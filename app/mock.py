"""离线自测用的模拟提供方（MockProvider）。

作用：在没有 API Key 的情况下，把「翻译流水线」整条链路真实跑一遍——
分批、术语表累积、JSON 解析、段落 id 漏返后的兜底重试、可中断续译、SSE 流式问答，
用的都是与 DeepSeek 完全相同的代码路径（`deepseek._extract_json` / `translate.*`）。

开启方式（二选一，二者都不会影响正常使用）：
    data/settings.json 里设 "provider": "mock"
    环境变量 EE_MOCK=1

注意：它**不翻译**，只是产出带〔模拟译文〕前缀的占位文本，用于验证对齐与流程，
绝不要拿它的输出去读论文。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterator

from .deepseek import _extract_json  # 复用真实客户端的解析逻辑

MOCK_TAG = "〔模拟译文〕"
MISSING_GLYPH = "⟦?⟧"


def _fill_placeholder(text: str) -> str:
    """把 ⟦?⟧ 还原成**带内容的**矩阵方括号。

    用来模拟"模型真的读懂了上下文"的理想输出。关键是括号里必须有东西：
    只给一对空括号（`\\left[\\;\\right]`）会被 `mathify.find_unrepaired` 判为未修复，
    那正好是我们要测的反例。
    """
    parts = text.split(MISSING_GLYPH)
    if len(parts) == 1:
        return text
    out = parts[0] + r"\left["
    last = len(parts) - 1
    for i, seg in enumerate(parts[1:], start=1):
        out += seg
        # 中间的空位视作矩阵换行，最后的空位补右括号
        out += r"\right]" if i == last else r" \\ "
    return out


class MockClient:
    """接口与 DeepSeekClient 完全一致，便于无缝替换。

    除了翻译，它还刻意模仿两种**真实模型会犯的错**，好让测试覆盖到对应的处理分支：
      * 收到含 `⟦?⟧`（无法辨认字形）的段落时，第一次会用 `⟨?⟩` 顶替 —— 这正是实测中
        模型的行为；只有收到加强指令（STRICT_REPAIR_HINT）后才给出真正的还原。
      * MOCK_BAD_REPAIR = True 时永远修不对，用于验证"确实修不好就如实标记"。
    """

    def __init__(self, settings: dict | None = None):
        self.settings = settings or {}
        self.model = "mock-translate"
        self.base_url = "mock://local"
        # 用于触发"漏返段落 id"的分支，验证 translate 的补漏重试
        self.calls = 0
        # True = 永远修不对（验证"修不好就如实标记"）；False = 收到严格指令后能修对
        self.MOCK_BAD_REPAIR = False

    # ---------------------------------------------------------------- 翻译
    TASK_MARKERS = ("translate_each_paragraph", "restore_original")

    def _payload_from_messages(self, messages: list[dict]) -> dict | None:
        """从消息里取出任务 JSON（翻译或"只重建左栏"）。

        用 JSONDecoder.raw_decode 而不是"找最后一个大括号"——公式里本来就有 {}，
        加上单段重译时 JSON 后面还会跟一段额外要求，必须让解析器自己判断边界。
        """
        for m in messages:
            content = m.get("content") or ""
            if not any(mk in content for mk in self.TASK_MARKERS):
                continue
            start = content.find("{")
            if start < 0:
                return None
            try:
                obj, _end = json.JSONDecoder().raw_decode(content[start:])
                return obj if isinstance(obj, dict) else None
            except Exception:
                return None
        return None

    def _fence(self, obj: Any) -> str:
        """故意输出 ```json 包裹的文本，用来验证解析器的容错。"""
        return "```json\n" + json.dumps(obj, ensure_ascii=False) + "\n```"

    def _protocol_json(self, messages: list[dict]) -> str | None:
        """第二轮（协议结构化）的模拟：把行号去掉、按行拆成步骤。

        刻意做得"规矩"——不增删内容、数字全留 —— 这样才走得过真实代码里的
        验收闸门（`protocol_keeps_content`），把这条链路也纳入自测。
        """
        if not any("排版助手" in (m.get("content") or "") for m in messages):
            return None
        # 注意：第二轮的用户消息**本身就是纯 JSON**（没有 translate_each_paragraph
        # 这类任务标记），所以不能走 _payload_from_messages（它按标记筛消息）。
        payload = None
        for m in reversed(messages):
            if m.get("role") != "user":
                continue
            content = m.get("content") or ""
            start = content.find("{")
            if start < 0:
                continue
            try:
                payload, _ = json.JSONDecoder().raw_decode(content[start:])
            except Exception:  # noqa: BLE001
                continue
            break
        if not isinstance(payload, dict) or "lines" not in payload:
            return None
        steps, setup = [], []
        for ln in payload.get("lines") or []:
            text = re.sub(r"^\s*\(?\d+[\.\)]?\s+", "", str(ln)).strip()
            if not text:
                continue
            if re.search(r"\b(Input|Output|输入|输出)\s*[:：]", text, re.I):
                setup.append(text)
            else:
                steps.append({"text": text, "subs": []})
        if not steps:
            return None
        return json.dumps({"protocol": {"title": str(payload.get("title") or ""),
                                        "setup": setup, "steps": steps}},
                          ensure_ascii=False)

    def _translate_json(self, messages: list[dict]) -> str:
        proto = self._protocol_json(messages)
        if proto is not None:
            return proto
        payload = self._payload_from_messages(messages)
        if not payload:
            return self._fence({"items": []})
        glossary = {g[0].lower(): g[1] for g in payload.get("glossary", []) if len(g) >= 2}
        want_rebuilt = bool(payload.get("need_rebuilt_original", True))
        items = []
        paras = payload.get("paragraphs", [])
        for i, p in enumerate(paras):
            # 第 3 次调用时故意丢掉中间一段，触发"部分缺失 → 单独重试"分支
            if self.calls == 3 and len(paras) >= 3 and i == 1:
                continue
            # 图：返回图题译文 + 图内文字 + （图内无文字时的）译注
            fig = p.get("figure")
            if isinstance(fig, dict) and fig.get("caption"):
                content = [MOCK_TAG + str(x) for x in (fig.get("content") or [])]
                note = "" if content else MOCK_TAG + "〔译注：原图为一幅示意图〕"
                items.append({"id": p.get("id"),
                              "figure": {"caption": MOCK_TAG + str(fig["caption"]),
                                         "content": content, "note": note}})
                continue
            # 伪代码：按行返回，行数保持一致；只给注释/说明行加标记（模拟"只翻注释"）
            alg = p.get("algorithm")
            if isinstance(alg, dict) and alg.get("lines"):
                lines = [str(x) for x in alg["lines"]]
                out_lines = []
                for ln in lines:
                    keep = ln.strip().startswith(("1", "2", "3", "4", "5", "6", "7", "8", "9"))
                    out_lines.append(ln if keep else (MOCK_TAG + ln))
                items.append({"id": p.get("id"), "algorithm": {"lines": out_lines}})
                continue
            # 表格：与真实模型一致，整表返回二维数组，行列数保持一致。
            # 表头也译（加前缀），数值/模型名原样保留。
            tbl = p.get("table")
            if isinstance(tbl, dict) and tbl.get("rows"):
                grid = tbl["rows"]
                head = int(tbl.get("head_rows") or 0)
                new_rows = []
                terms = []
                for r, row in enumerate(grid):
                    new_rows.append([
                        (MOCK_TAG + str(c)) if (r < head and str(c).strip()) else str(c)
                        for c in row
                    ])
                for c in (grid[0] if grid else []):
                    w = str(c).split(" ")[0]
                    if re.fullmatch(r"[A-Za-z][A-Za-z-]{4,}", w or ""):
                        terms.append({"en": w, "zh": "模拟术语"})
                rec_t = {"id": p.get("id"), "table": {"rows": new_rows},
                         "terms": terms[:4]}
                if p.get("caption"):
                    rec_t["caption"] = MOCK_TAG + str(p["caption"])
                items.append(rec_t)
                continue
            text = p.get("en", "")
            terms = []
            for w in re.findall(r"\b[A-Za-z][A-Za-z-]{6,}\b", text)[:2]:
                zh = glossary.get(w.lower())
                if not zh:
                    zh = "模拟术语"
                    glossary[w.lower()] = zh
                terms.append({"en": w, "zh": zh})
            rec = {
                "id": p.get("id"),
                "zh": MOCK_TAG + text,
                "terms": terms,
            }
            if want_rebuilt:
                # 与真实模型一致：每段都返回 en；无需改动的原样返回
                rec["en"] = self._rebuild(text) or text
            if MISSING_GLYPH in text:
                strict = any("上一轮的问题" in (m.get("content") or "") for m in messages)
                if self.MOCK_BAD_REPAIR or not strict:
                    # 模仿真实模型的偷懒：把占位符换成 `?`（第一次尝试）
                    fixed = text.replace(MISSING_GLYPH, r"\langle ? \rangle")
                else:
                    # 收到加强指令后给出真正的还原：成对的占位符当矩阵方括号，
                    # 且**括号里要有内容** —— 空括号会被 find_unrepaired 判为未修复。
                    fixed = _fill_placeholder(text)
                rec["en"] = fixed
                rec["zh"] = MOCK_TAG + fixed      # 译文里同样不再出现占位符
            items.append(rec)
        return self._fence({"items": items})

    def _restore_json(self, payload: dict) -> str:
        """「只重建左栏」：只返回 en，不返回 zh。

        与真实模型一致：**每段都要返回 en**，无需改动的原样返回（这样调用方才能
        区分"无需改动"与"响应缺失"）。
        """
        items = []
        for p in payload.get("paragraphs", []):
            src = p.get("en", "")
            items.append({"id": p.get("id"), "en": self._rebuild(src) or src})
        return self._fence({"items": items})

    @staticmethod
    def _rebuild(text: str) -> str:
        """模拟"原文重建"：按真实模型会做的方向做几处确定性规整。

        - 公式内部的空格归一（`$L _{x}$` -> `$L_{x}$`）
        - 去掉公式内外侧多余的空白（`$x$ )` -> `$x$)`）
        - 独占一段的行内公式提升为独立公式
        """
        def fix_math(m: re.Match) -> str:
            inner = re.sub(r"\s+", " ", m.group(1)).strip()
            inner = re.sub(r"\s+([_^])", r"\1", inner)
            inner = re.sub(r"([_^]\{[^}]*\})\s+", r"\1", inner)
            return "$" + inner + "$"

        fixed = re.sub(r"\$(?!\$)((?:[^$]|\\.)*?)\$", fix_math, text)
        fixed = re.sub(r"([)\]},;:])\s{2,}", r"\1 ", fixed)
        fixed = re.sub(r"[ \t]{2,}", " ", fixed).strip()
        if re.fullmatch(r"\$[^$\n]+\$", fixed):
            fixed = "$$" + fixed[1:-1] + "$$"
        return fixed if fixed != text else ""

    def _answer(self, messages: list[dict]) -> str:
        user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user = m.get("content") or ""
                break
        q = ""
        m = re.search(r"我的问题：(.*)", user, re.S)
        if m:
            q = m.group(1).strip().splitlines()[0][:120]
        ids = re.findall(r"【(p\d+)", user)
        return (
            "（这是模拟回答，用于验证流式链路）\n\n"
            f"收到的问题：{q}\n\n"
            f"上下文里包含段落：{'、'.join(ids) if ids else '（无）'}。\n\n"
            r"示例公式渲染：$\mathcal{A}(x) = \sum_{i=1}^{n} a_i x^i$，"
            r"以及行内 $\mathbb{Z}_q$ 与 $e(a,b)=e(b,a)$。"
        )

    def chat(self, messages: list[dict], model: str | None = None,
             temperature: float | None = None, json_mode: bool = False,
             max_tokens: int | None = None) -> str:
        self.calls += 1
        time.sleep(0.05)  # 模拟一点网络延迟
        # 第二轮（协议结构化）先判：它的请求里**没有** translate_each_paragraph 这类任务标记，
        # 走 _payload_from_messages 会返回 None、掉进兜底的问答分支（踩过：
        # 结果就是第二轮"悄悄没跑"）。
        proto = self._protocol_json(messages)
        if proto is not None:
            return proto
        payload = self._payload_from_messages(messages)
        if payload is not None:
            if payload.get("task") == "restore_original":
                return self._restore_json(payload)
            return self._translate_json(messages)
        return self._answer(messages)

    def chat_json(self, messages: list[dict], **kw) -> Any:
        return _extract_json(self.chat(messages, **kw))

    def stream(self, messages: list[dict], model: str | None = None,
               temperature: float | None = None, max_tokens: int | None = None) -> Iterator[str]:
        text = self._answer(messages)
        for i in range(0, len(text), 12):
            time.sleep(0.01)
            yield text[i:i + 12]

    def list_models(self) -> list[str]:
        return [self.model]

    def close(self) -> None:
        pass
