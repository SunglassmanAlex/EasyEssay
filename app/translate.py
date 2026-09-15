"""逐段翻译流水线 + 选中片段问答。

翻译策略：
- 按字数/段数切批，一批一次请求，返回 JSON（段落 id 一一对应），从机制上保证左右对齐；
- 术语表跨批累积并回灌给模型，保证全文术语译法一致；
- 单批失败自动二分重试，仍失败的段落单独重试；
- 每批完成即落盘，可中断、可续翻（只翻缺失段落）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterator

from . import mathify, store
from .deepseek import DeepSeekClient, DeepSeekError, make_client

OUTPUT_SPEC = r"""

【输出格式（必须严格遵守）】
只输出一个 JSON 对象，不要任何解释、不要 Markdown 代码块，形如：
{"items":[{"id":"p0001","en":"重建后的英文原文（公式为标准 LaTeX）","zh":"译文正文","terms":[{"en":"commitment","zh":"承诺"}]}]}
规则：
- items 与输入段落一一对应，id 必须原样返回，顺序一致，不得遗漏、合并或增补；
- en 是**重建后的英文原文**：文字内容与原文完全一致，只把数学内容规范化成标准 LaTeX
  （行内 $...$，独立 $$...$$）；若该段没有公式或本来就很规范，en 原样返回；
- zh 是译文正文；其中的数学内容必须保持 $...$（行内）或 $$...$$（独立）的 LaTeX 写法；
- 输入里的 `⟦?⟧` 是 PDF 无法辨认的字形（多为大型括号/矩阵分隔符/分式线）：
  按上下文还原成最可能的 LaTeX，**绝不在 zh 里保留 `⟦?⟧`**；
- 若该段是纯公式（$$...$$），en 与 zh 都原样返回该公式，不要添加解释；
- terms 只列该段的关键术语（最多 4 个，en/zh 对应），没有则给空数组 []。"""

# 模型把占位符改写成 `?` 时，用于第二次尝试的加强指令
STRICT_REPAIR_HINT = r"""

【重要·上一轮的问题】你把无法辨认的字形占位符 ⟦?⟧ 改写成了 `?`，这不行：
`?` 不是公式的一部分，看起来像"修好了"其实是在藏问题。这次请务必：
- 不要出现任何 `?`；
- 结合上下文给出**最可能的字面推断**。按实测统计，⟦?⟧ 绝大多数是：
  矩阵/向量的括号 `[ ]`、`( )`（例如竖排向量用 \left(\begin{matrix}…\end{matrix}\right)），
  集合的花括号 `{ }`，范数 `\| \|`，内积/期望的 `\langle \rangle`，或分式横线。
  同一行成对出现的两个 ⟦?⟧ 基本就是矩阵方括号；
- 实在无法确定时，选择上下文里最常用的那一种即可，但**不要留 `?`、不要留 ⟦?⟧**。"""


# 关闭「原文重建」时的输出协议（只翻译）
OUTPUT_SPEC_NO_RESTORE = r"""

【输出格式（必须严格遵守）】
只输出一个 JSON 对象，不要任何解释、不要 Markdown 代码块，形如：
{"items":[{"id":"p0001","zh":"译文正文","terms":[{"en":"commitment","zh":"承诺"}]}]}
规则：
- items 与输入段落一一对应，id 必须原样返回，顺序一致，不得遗漏、合并或增补；
- zh 是译文正文；其中的数学内容必须保持 $...$（行内）或 $$...$$（独立）的 LaTeX 写法；
- 输入里的 `⟦?⟧` 是 PDF 无法辨认的字形（多为大型括号/矩阵分隔符/分式线）：
  按上下文还原成最可能的 LaTeX，**绝不在 zh 里保留 `⟦?⟧`**；
- 若该段是纯公式（$$...$$），zh 原样返回该公式，不要添加解释；
- terms 只列该段的关键术语（最多 4 个，en/zh 对应），没有则给空数组 []。"""

# 「只重建左栏、不重译」用的输出协议：输出更短，省钱
OUTPUT_SPEC_RESTORE = r"""

【任务】把每段英文原文**重建**成规范排版的文本：文字内容一字不改，只把数学内容
（上下标、分式、求和号、\vec/\mathrm/\langle 等语义命令、被拉平或粘连的公式）
按数学含义还原成标准 LaTeX（行内 $...$，独立 $$...$$）。

【输出格式（必须严格遵守）】
只输出一个 JSON 对象，不要任何解释、不要 Markdown 代码块，形如：
{"items":[{"id":"p0001","en":"重建后的英文原文"}]}
规则：
- items 与输入段落一一对应，id 必须原样返回，顺序一致，不得遗漏；
- 不要输出 zh 字段，不要翻译，不要增删或改写任何文字；
- 若某段本来就很规范，en 原样返回即可。"""


def _chunk(paras: list[dict], batch_size: int, max_chars: int) -> list[list[dict]]:
    batches: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0
    for p in paras:
        length = len(p.get("text", ""))
        if cur and (len(cur) >= batch_size or cur_chars + length > max_chars):
            batches.append(cur)
            cur, cur_chars = [], 0
        cur.append(p)
        cur_chars += length
    if cur:
        batches.append(cur)
    return batches


_WS_RE = re.compile(r"\s+")


def _norm_ws(s: str) -> str:
    return _WS_RE.sub(" ", (s or "")).strip()


def _build_messages(batch: list[dict], system_prompt: str, target_lang: str,
                    glossary: list[list[str]], restore_original: bool) -> list[dict]:
    spec = OUTPUT_SPEC if restore_original else OUTPUT_SPEC_NO_RESTORE
    sys = system_prompt.rstrip()
    # 若用户自定义提示词里没提「任务 A」，补一句，避免模型不返回 en
    if restore_original and "任务 A" not in sys:
        sys += "\n\n另外：每个 item 还要返回 en —— 把该段英文原文重建为规范排版的文本" \
               "（文字不变，只把数学内容写成标准 LaTeX）。"
    if not restore_original:
        sys = re.sub(r"【任务 A】[\s\S]*?(?=【任务 B】)", "", sys)
    sys += spec
    payload: dict[str, Any] = {
        "target_language": target_lang,
        "task": "translate_each_paragraph",
        "need_rebuilt_original": bool(restore_original),
        "paragraphs": [
            {"id": p["id"], "kind": p.get("kind", "text"), "en": p["text"]}
            for p in batch
        ],
    }
    if restore_original:
        payload["note"] = ("输入里的 en 是从 PDF 抽出的原文，数学内容可能是碎的；"
                           "请按数学含义重建为规范 LaTeX 后返回。")
    if glossary:
        payload["glossary"] = glossary
        payload["glossary_note"] = "以下术语必须沿用既有译法（英文 -> 中文）"
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": "请翻译以下段落并按要求返回 JSON：\n"
                                    + json.dumps(payload, ensure_ascii=False)},
    ]


def _norm_item(item: dict) -> tuple[str, str, list[dict], str]:
    """返回 (id, zh, terms, en_rebuilt)。en_rebuilt 为空串表示模型认为原文无需改动。"""
    pid = str(item.get("id") or item.get("paragraph_id") or "").strip()
    zh = item.get("zh") or item.get("translation") or item.get("text") or ""
    en = item.get("en") or item.get("en_fixed") or item.get("original") or ""
    terms = item.get("terms") or []
    clean_terms = []
    if isinstance(terms, list):
        for t in terms:
            if isinstance(t, dict) and t.get("en"):
                clean_terms.append({"en": str(t["en"]).strip(),
                                    "zh": str(t.get("zh") or "").strip()})
    return pid, str(zh).strip(), clean_terms, str(en).strip()


def _translate_batch(client: DeepSeekClient, batch: list[dict], settings: dict,
                     glossary: list[list[str]]) -> tuple[dict, list[dict]]:
    """翻译一批，返回 ({id: {"zh":..,"terms":..,"en":..}}, 新术语)。失败自动二分。"""
    if not batch:
        return {}, []
    restore = bool(settings.get("restore_original", True))
    messages = _build_messages(
        batch, settings.get("system_prompt", ""), settings.get("target_lang", "简体中文"),
        glossary, restore,
    )
    try:
        data = client.chat_json(
            messages,
            model=settings.get("model"),
            temperature=float(settings.get("temperature", 1.0) or 1.0),
        )
    except DeepSeekError:
        if len(batch) == 1:
            raise
        mid = len(batch) // 2
        a, ta = _translate_batch(client, batch[:mid], settings, glossary)
        b, tb = _translate_batch(client, batch[mid:], settings, glossary + ta)
        return {**a, **b}, ta + tb

    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        items = []
    raw_by_id = {p["id"]: p["text"] for p in batch}
    out: dict[str, Any] = {}
    new_terms: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        pid, zh, terms, en = _norm_item(it)
        if not pid or not zh:
            continue
        rec: dict[str, Any] = {"zh": zh, "terms": terms}
        # 统一保存 en（即使与原文相同）：这样 translated.json 自描述、重建任务幂等，
        # 前端也只对「与原文不同」的段落打「公式已重建」标记。
        if restore and en:
            rec["en"] = en
        out[pid] = rec
        new_terms.extend(terms)

    # 校验：模型有没有用 `?` 顶替占位符、或干脆照抄占位符
    #
    # 注意 `_no_repair_retry`：重试时的子调用必须跳过这段校验，否则单段重试会
    # 再次触发"校验→重试→校验"，模型永远修不好时就会无限递归（第一版就是这么挂的）。
    if not settings.get("_no_repair_retry"):
        bad = [pid for pid, rec in out.items()
               if mathify.find_unrepaired((rec.get("en") or "") + (rec.get("zh") or ""))]
        for pid in bad:
            para = next((p for p in batch if p["id"] == pid), None)
            if not para:
                continue
            try:
                strict = dict(settings)
                strict["_no_repair_retry"] = True
                strict["system_prompt"] = (settings.get("system_prompt", "") or "") + STRICT_REPAIR_HINT
                sub, _t = _translate_batch(client, [para], strict, glossary)
                if sub and not mathify.find_unrepaired(
                        (sub[pid].get("en") or "") + (sub[pid].get("zh") or "")):
                    out[pid] = sub[pid]
                    continue
            except DeepSeekError:
                pass
            # 重试仍不行：如实标记"待人工确认"，不要让用户以为修好了
            out[pid]["unrepaired"] = True

    missing = [p for p in batch if p["id"] not in out]
    if missing:
        if len(missing) == len(batch):
            if len(batch) > 1:
                mid = len(batch) // 2
                a, ta = _translate_batch(client, batch[:mid], settings, glossary)
                b, tb = _translate_batch(client, batch[mid:], settings, glossary + ta)
                return {**a, **b}, new_terms + ta + tb
            raise DeepSeekError("批次翻译返回为空")
        # 只有部分缺失：逐个补
        for p in missing:
            try:
                sub, _t = _translate_batch(client, [p], settings, glossary)
                out.update(sub)
            except DeepSeekError:
                continue
    return out, new_terms


def _update_glossary(glossary: list[list[str]], terms: list[dict], limit: int = 80) -> None:
    known = {g[0].lower() for g in glossary}
    for t in terms:
        en, zh = t.get("en", "").strip(), t.get("zh", "").strip()
        if not en or not zh or en.lower() in known:
            continue
        if len(en) > 40:
            continue
        glossary.append([en, zh])
        known.add(en.lower())
    del glossary[limit:]


def translate_document(
    doc_id: str,
    settings: dict | None = None,
    only_ids: list[str] | None = None,
    force: bool = False,
    progress: Callable[[dict], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """执行翻译，返回统计结果。"""
    from .config import load_settings

    st = dict(load_settings())
    if settings:
        st.update({k: v for k, v in settings.items() if v is not None})

    extracted = store.load_extracted(doc_id)
    paras = extracted.get("paragraphs", [])
    if not paras:
        raise RuntimeError("该文档没有可翻译的段落（抽取结果为空）")
    done = store.load_translations(doc_id)

    def _has_zh(pid: str) -> bool:
        return bool(((done.get(pid) or {}).get("zh") or "").strip())

    if only_ids:
        todo = [p for p in paras if p["id"] in set(only_ids)]
    elif force:
        todo = list(paras)
    else:
        # 注意：记录存在但 zh 为空（例如重抽后合并段落丢失了半段译文）也要重译
        todo = [p for p in paras if p["id"] not in done or not _has_zh(p["id"])]
    if not todo:
        store.update_meta(doc_id, {"status": "ready", "progress": 1.0,
                                   "message": "无需翻译的段落"})
        return {"newly": 0, "translated": len(done), "total": len(paras), "skipped": True}

    batches = _chunk(todo, int(st.get("translate_batch_size", 8) or 8),
                     int(st.get("max_chars_per_batch", 3500) or 3500))
    glossary: list[list[str]] = []
    # 用已有译文里的术语预填术语表
    for v in done.values():
        _update_glossary(glossary, (v or {}).get("terms") or [])

    client = make_client(st)
    total = len(todo)
    completed = 0
    failed: list[str] = []
    store.update_meta(doc_id, {"status": "translating", "progress": len(done) / max(1, len(paras)),
                               "message": f"待翻译 {total} 段", "error": ""})
    try:
        for bi, batch in enumerate(batches, 1):
            if should_stop and should_stop():
                store.update_meta(doc_id, {"status": "partial",
                                           "message": "已手动停止"})
                break
            try:
                mapping, terms = _translate_batch(client, batch, st, glossary)
                _update_glossary(glossary, terms)
                store.merge_translations(doc_id, mapping)
                completed += len(mapping)
                failed.extend(p["id"] for p in batch if p["id"] not in mapping)
            except Exception as e:  # noqa: BLE001
                failed.extend(p["id"] for p in batch)
                store.update_meta(doc_id, {"error": str(e)[:300],
                                           "message": f"第 {bi}/{len(batches)} 批失败：{str(e)[:120]}"})
            translated = len(store.load_translations(doc_id))
            info = {
                "batch": bi, "batches": len(batches), "completed": completed,
                "total": total, "translated": translated,
                "paragraphs": len(paras),
                "progress": translated / max(1, len(paras)),
            }
            store.update_meta(doc_id, {
                "progress": info["progress"],
                "message": f"已翻译 {translated}/{len(paras)} 段",
                "stats": {"paragraphs": len(paras), "translated": translated},
            })
            if progress:
                progress(info)
    finally:
        client.close()

    final_meta = store.get_meta(doc_id)
    total_translated = sum(1 for v in store.load_translations(doc_id).values()
                           if (v or {}).get("zh"))
    status = "ready" if total_translated >= len(paras) else ("partial" if total_translated else "error")
    if failed and total_translated:
        status = "ready" if total_translated >= len(paras) else "partial"
    store.update_meta(doc_id, {
        "status": status, "progress": total_translated / max(1, len(paras)),
        "stats": {"paragraphs": len(paras), "translated": total_translated},
        "message": f"完成：{total_translated}/{len(paras)} 段" + (f"，{len(failed)} 段失败" if failed else ""),
    })
    # newly：本次真正新翻译的段数；translated：翻译后累计已译段数；total：文档总段数
    return {"newly": completed, "translated": total_translated, "total": len(paras),
            "failed": sorted(set(failed)), "batches": len(batches),
            "previous_status": final_meta.get("status")}


# ---------------------------------------------------------------- 问答

def build_ask_context(doc_id: str, para_id: str, selection: str = "",
                      span: int = 2) -> str:
    extracted = store.load_extracted(doc_id)
    paras = extracted.get("paragraphs", [])
    translations = store.load_translations(doc_id)
    index = {p["id"]: i for i, p in enumerate(paras)}
    i = index.get(para_id)
    lines: list[str] = []
    if i is None:
        if selection:
            lines.append(f"【读者选中的片段】{selection}")
        return "\n".join(lines)
    lo, hi = max(0, i - span), min(len(paras), i + span + 1)
    for j in range(lo, hi):
        p = paras[j]
        tag = " <<< 读者当前所在的段落" if j == i else ""
        lines.append(f"【{p['id']}｜第{p['page']}页｜{p['kind']}】{tag}")
        tr = translations.get(p["id"]) or {}
        # 优先用「重建原文」喂给模型：公式是规范 LaTeX，比 PDF 直抽的碎片更好推理
        lines.append(f"EN: {tr.get('en') or p['text']}")
        if tr.get("zh"):
            lines.append(f"ZH: {tr['zh']}")
    if selection:
        lines.append("")
        lines.append(f"【读者选中的片段】{selection}")
    return "\n".join(lines)


def ask_stream(doc_id: str, question: str, para_id: str = "", selection: str = "",
               settings: dict | None = None, history: list[dict] | None = None) -> Iterator[str]:
    """流式回答关于某段/某片段的提问。"""
    from .config import load_settings

    st = dict(load_settings())
    if settings:
        st.update({k: v for k, v in settings.items() if v is not None})
    client = make_client(st)
    try:
        context = build_ask_context(doc_id, para_id, selection,
                                    int(st.get("context_paragraphs", 2) or 2))
        sys_prompt = st.get("ask_system_prompt", "") or ""
        messages: list[dict] = [{"role": "system", "content": sys_prompt}]
        for h in (history or [])[-6:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                messages.append({"role": h["role"], "content": str(h["content"])[:4000]})
        user = (f"以下是论文上下文（左列为英文原文，右列为已有中文译文）：\n\n{context}\n\n"
                f"我的问题：{question}\n\n"
                "请基于以上上下文作答，必要时引用段落编号。")
        messages.append({"role": "user", "content": user})
        model = st.get("ask_model") or st.get("model")
        yield from client.stream(messages, model=model,
                                 temperature=float(st.get("temperature", 1.0) or 1.0))
    finally:
        client.close()


# ------------------------------------------------- 只重建左栏（不重译）

def _restore_batch(client: DeepSeekClient, batch: list[dict], settings: dict) -> dict:
    """让模型只返回重建后的原文（en），不翻译——输出短、便宜。

    返回值包含**所有**被处理的段落 id：值为重建文本，值为空串表示"原文已规范、无需改动"。
    未被返回的 id 才算失败（响应缺失）。
    """
    if not batch:
        return {}
    messages = [
        {"role": "system", "content": (settings.get("system_prompt", "") or "").split("【任务 B】")[0]
         .rstrip() + OUTPUT_SPEC_RESTORE},
        {"role": "user", "content": "请重建以下段落的原文并按要求返回 JSON：\n"
         + json.dumps({"task": "restore_original",
                       "paragraphs": [{"id": p["id"], "en": p["text"]} for p in batch]},
                      ensure_ascii=False)},
    ]
    try:
        data = client.chat_json(messages, model=settings.get("model"),
                                temperature=float(settings.get("temperature", 1.0) or 1.0))
    except DeepSeekError:
        if len(batch) == 1:
            raise
        mid = len(batch) // 2
        return {**_restore_batch(client, batch[:mid], settings),
                **_restore_batch(client, batch[mid:], settings)}
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        items = []
    raw_by_id = {p["id"]: p["text"] for p in batch}
    out: dict[str, str] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        pid = str(it.get("id") or "").strip()
        en = str(it.get("en") or it.get("text") or "").strip()
        if pid in raw_by_id:
            out[pid] = en or raw_by_id[pid]
    missing = [p for p in batch if p["id"] not in out]
    if missing and len(missing) == len(batch) and len(batch) > 1:
        mid = len(batch) // 2
        return {**_restore_batch(client, batch[:mid], settings),
                **_restore_batch(client, batch[mid:], settings)}
    return out


def restore_document(doc_id: str, settings: dict | None = None,
                     only_ids: list[str] | None = None, force: bool = False,
                     progress: Callable[[dict], None] | None = None,
                     should_stop: Callable[[], bool] | None = None) -> dict:
    """只重建左栏原文，保留已有译文。用于"译文已翻好、只想修公式"的场景。"""
    from .config import load_settings

    st = dict(load_settings())
    if settings:
        st.update({k: v for k, v in settings.items() if v is not None})
    extracted = store.load_extracted(doc_id)
    paras = extracted.get("paragraphs", [])
    if not paras:
        raise RuntimeError("该文档没有可重建的段落")
    done = store.load_translations(doc_id)

    if only_ids:
        todo = [p for p in paras if p["id"] in set(only_ids)]
    elif force:
        todo = list(paras)
    else:
        # 已有 en 的不再重建（除非 force）
        todo = [p for p in paras if not (done.get(p["id"]) or {}).get("en")]
    if not todo:
        store.update_meta(doc_id, {"message": "左栏无需重建"})
        return {"newly": 0, "total": len(paras), "skipped": True}

    batches = _chunk(todo, int(st.get("translate_batch_size", 8) or 8),
                     int(st.get("max_chars_per_batch", 3500) or 3500))
    client = make_client(st)
    completed = 0
    unchanged = 0
    failed: list[str] = []
    store.update_meta(doc_id, {"status": "restoring",
                               "message": f"重建左栏：待处理 {len(todo)} 段"})
    try:
        for bi, batch in enumerate(batches, 1):
            if should_stop and should_stop():
                store.update_meta(doc_id, {"status": "partial", "message": "已手动停止"})
                break
            try:
                got = _restore_batch(client, batch, st)
                raw_map = {p["id"]: p["text"] for p in batch}
                patch = {}
                for pid, en in got.items():
                    cur = dict(store.load_translations(doc_id).get(pid) or {})
                    cur["en"] = en
                    cur.setdefault("zh", "")
                    cur.setdefault("terms", [])
                    patch[pid] = cur
                    if _norm_ws(en) == _norm_ws(raw_map.get(pid, "")):
                        unchanged += 1
                if patch:
                    store.merge_translations(doc_id, patch)
                    completed += len(patch)
                failed.extend(p["id"] for p in batch if p["id"] not in got)
            except Exception as e:  # noqa: BLE001
                failed.extend(p["id"] for p in batch)
                store.update_meta(doc_id, {"error": str(e)[:300]})
            if progress:
                progress({"batch": bi, "batches": len(batches),
                          "completed": completed, "total": len(todo)})
            store.update_meta(doc_id, {"message": f"重建左栏 {completed}/{len(todo)} 段"})
    finally:
        client.close()

        total_en = sum(1 for v in store.load_translations(doc_id).values() if (v or {}).get("en"))
        tr_total = sum(1 for v in store.load_translations(doc_id).values() if (v or {}).get("zh"))
        status = "ready" if tr_total >= len(paras) else ("partial" if tr_total else "extracted")
        store.update_meta(doc_id, {"status": status,
                                   "message": f"左栏已重建 {completed - unchanged} 段"
                                              + (f"（{unchanged} 段原文已规范）" if unchanged else "")
                                              + (f"，{len(failed)} 段失败" if failed else "")})
        return {"newly": completed - unchanged, "unchanged": unchanged,
                "covered": total_en, "failed": sorted(set(failed)), "total": len(paras)}


def retranslate_paragraph(doc_id: str, para_id: str, instruction: str = "",
                          settings: dict | None = None) -> dict:
    from .config import load_settings

    st = dict(load_settings())
    if settings:
        st.update({k: v for k, v in settings.items() if v is not None})
    extracted = store.load_extracted(doc_id)
    para = next((p for p in extracted.get("paragraphs", []) if p["id"] == para_id), None)
    if not para:
        raise RuntimeError(f"未找到段落 {para_id}")
    client = make_client(st)
    try:
        extra = f"\n\n本段额外要求（优先满足）：{instruction}" if instruction else ""
        messages = _build_messages([para], st.get("system_prompt", ""),
                                   st.get("target_lang", "简体中文"), [],
                                   bool(st.get("restore_original", True)))
        messages[1]["content"] += extra
        data = client.chat_json(messages, model=st.get("model"),
                                temperature=float(st.get("temperature", 1.0) or 1.0))
        items = data.get("items") if isinstance(data, dict) else data
        if not items:
            raise DeepSeekError("重译返回为空")
        pid, zh, terms, en = _norm_item(items[0])
        rec: dict[str, Any] = {"zh": zh, "terms": terms}
        if en and _norm_ws(en) != _norm_ws(para["text"]):
            rec["en"] = en
        store.merge_translations(doc_id, {para_id: rec})
        return {"id": para_id, **rec}
    finally:
        client.close()
