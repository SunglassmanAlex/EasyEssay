"""翻译流水线端到端自测（不需要 API Key）。

用 app/mock.py 的模拟提供方，把真实代码路径整条跑通并逐项断言：
  1. 逐段翻译：段落 id 一一对应、无遗漏、顺序一致
  2. 进度回调单调递增，最终为 1.0
  3. 术语表跨批累积并回灌（同一术语译法一致）
  4. 模型漏返段落 id 时能自动补漏（mock 故意在第 3 次调用丢掉一段）
  5. 断点续译：只翻缺失段落；force=true 才重译全部
  6. 可中途停止（status 变成 partial）
  7. 单段重译接口
  8. 流式问答（SSE）
  9. 导出 HTML → 交给 scripts/render_test.js 做无头渲染校验（应无「待翻译」单元格）

用法：python scripts/pipeline_test.py [--keep] [--pages 1-3]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.console import force_utf8  # noqa: E402
force_utf8()   # Windows 控制台默认不是 UTF-8，不切的话打印中文/✅ 会直接崩


import app.translate as T  # noqa: E402
from app import render, store  # noqa: E402
from app.extract import extract_pdf  # noqa: E402
from app.extract import _is_glyph_only_fragment, _merge_glyph_fragments  # noqa: E402
from app import mathify as M  # noqa: E402
from app.mock import MockClient  # noqa: E402
from app.translate import _norm_ws  # noqa: E402

def sample_pdf() -> Path:
    """测试用的 PDF：优先真实论文；仓库里没有（版权原因不分发）就用合成样张。

    合成样张由 scripts/make_sample_pdf.py 现场生成，包含标题/摘要/章节/公式/表格/参考文献，
    足以覆盖抽取器的各条路径，因此在新 clone 的仓库或 CI 上也能跑通自测。
    """
    real = ROOT / "samples" / "plonk.pdf"
    if real.exists():
        return real
    synthetic = ROOT / "samples" / "sample-paper.pdf"
    if not synthetic.exists():
        import subprocess
        subprocess.run([sys.executable, str(ROOT / "scripts" / "make_sample_pdf.py")],
                       check=True, cwd=str(ROOT))
    return synthetic


PDF = sample_pdf()
# 无头渲染自测需要 node 与 jsdom。默认用 PATH 里的 node；jsdom 用 EE_JSDOM 指定，
# 都没有就跳过这一项（不影响其它断言）。
NODE = Path(os.environ.get("EE_NODE", "node"))
JSDOM = Path(os.environ["EE_JSDOM"]) if os.environ.get("EE_JSDOM") else None

PASS, FAIL = [], []


def _has_node() -> bool:
    import shutil as _sh
    return bool(_sh.which(str(NODE)) or Path(str(NODE)).exists())



def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  ✅ " if ok else "  ❌ ") + name + (f"  —— {detail}" if detail else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", default="1-3")
    ap.add_argument("--keep", action="store_true", help="保留测试文档不删除")
    args = ap.parse_args()
    p_from, p_to = (int(x) for x in args.pages.split("-"))

    print("\n== 0. 准备测试文档 ==")
    tmp_dir = Path(tempfile.mkdtemp(prefix="easyessay-test-"))
    data = extract_pdf(PDF, p_from, p_to)
    paras = data["paragraphs"]
    ids = [p["id"] for p in paras]
    doc_id = store.create_doc("流水线自测 · PLONK", PDF, "plonk.pdf",
                              settings={"model": "mock-translate"})
    store.save_extracted(doc_id, data)
    print(f"  文档 {doc_id}：{len(paras)} 段，{len(data.get('ocr_pages') or [])} 页 OCR")

    settings = {
        "provider": "mock",
        "model": "mock-translate",
        "translate_batch_size": 6,
        "max_chars_per_batch": 2500,
        "target_lang": "简体中文",
    }

    # 全程使用同一个可计数的模拟客户端，用调用次数验证"只翻缺失段落"这类行为
    client = MockClient(settings)
    real_make = T.make_client
    T.make_client = lambda st: client
    probe = None          # 第 13 节的探针文档，供 finally 清理
    try:
        # ------------------------------------------------------------ 1
        print("\n== 1. 逐段翻译 ==")
        progress_log: list[float] = []
        res = T.translate_document(doc_id, settings,
                                   progress=lambda i: progress_log.append(i["progress"]))
        tr = store.load_translations(doc_id)
        check("所有段落都有译文", len(tr) == len(paras), f"{len(tr)}/{len(paras)}")
        check("段落 id 完全对应", set(tr) == set(ids))
        check("译文非空", all((tr[i].get("zh") or "").strip() for i in ids))
        check("确实分批了", res["batches"] > 1, f"batches={res['batches']}")
        check("首次运行 newly == 总段数", res["newly"] == len(paras), f"newly={res['newly']}")
        full_run_calls = client.calls
        print(f"  （首次全量翻译共 {full_run_calls} 次模型调用）")

        # ------------------------------------------------------------ 2
        print("\n== 2. 进度回调 ==")
        check("有进度回调", len(progress_log) >= res["batches"], f"{len(progress_log)} 次")
        check("进度单调不减", all(b >= a for a, b in zip(progress_log, progress_log[1:])))
        check("最终进度为 1.0", abs(store.get_meta(doc_id)["progress"] - 1.0) < 1e-6)

        # ------------------------------------------------------------ 3
        print("\n== 3. 术语表 ==")
        terms = [t for v in tr.values() for t in (v.get("terms") or [])]
        check("产出了术语", len(terms) > 0, f"{len(terms)} 条")
        en2zh: dict[str, set] = {}
        for t in terms:
            en2zh.setdefault(t["en"].lower(), set()).add(t["zh"])
        conflicts = {k: v for k, v in en2zh.items() if len(v) > 1}
        check("同一术语译法一致", not conflicts, f"冲突 {list(conflicts)[:3]}")
        raw_by_id = {p["id"]: p["text"] for p in paras}
        has_en = [k for k, v in tr.items() if v.get("en")]
        changed = [k for k in has_en if _norm_ws(tr[k]["en"]) != _norm_ws(raw_by_id[k])]
        check("每段都保存了 en（幂等所需）", len(has_en) == len(paras),
              f"{len(has_en)}/{len(paras)}")
        check("其中确有公式被重建", len(changed) > 0, f"{len(changed)} 段与原抽取不同")

        # ------------------------------------------------------------ 4
        print("\n== 4. 漏返段落 id 的补漏 ==")
        c2 = MockClient(settings)
        batch = [{"id": "px01", "kind": "text", "text": "Alpha commitment protocol"},
                 {"id": "px02", "kind": "text", "text": "Beta permutation argument"},
                 {"id": "px03", "kind": "text", "text": "Gamma arithmetization step"}]
        c2.calls = 2          # 下一次调用 calls==3，mock 会故意丢掉中间一段
        got, _ = T._translate_batch(c2, batch, settings, [])
        check("漏返的段落被补回", set(got) == {"px01", "px02", "px03"}, str(sorted(got)))
        check("确实触发了补漏调用", c2.calls > 3, f"calls={c2.calls}")

        # ------------------------------------------------------------ 5
        print("\n== 5. 断点续译 / 强制重译 ==")
        full = store.load_translations(doc_id)
        half = ids[: len(ids) // 2]
        store.save_translations(doc_id, {k: v for k, v in full.items() if k not in half})
        before = client.calls
        res2 = T.translate_document(doc_id, settings)
        delta = client.calls - before
        expect = math.ceil(len(half) / settings["translate_batch_size"])
        check("只翻缺失段落", res2["newly"] == len(half),
              f"newly={res2['newly']}，缺失 {len(half)} 段")
        check("调用次数与缺失段数相称", delta <= expect + 1,
              f"{delta} 次调用（预期 ≤ {expect + 1}，全量需 {full_run_calls} 次）")
        check("续译后补齐", len(store.load_translations(doc_id)) == len(paras))

        res3 = T.translate_document(doc_id, settings, force=True)
        check("force 重译全部", res3["newly"] == len(paras), f"newly={res3['newly']}")

        # ------------------------------------------------------------ 6
        print("\n== 6. 中途停止 ==")
        store.save_translations(doc_id, {})
        stop_after = {"n": 0}

        def should_stop() -> bool:
            stop_after["n"] += 1
            return stop_after["n"] > 2      # 第 3 批之前停下

        T.translate_document(doc_id, settings, progress=lambda i: None,
                             should_stop=should_stop)
        meta = store.get_meta(doc_id)
        done = len(store.load_translations(doc_id))
        check("停止后状态为 partial", meta["status"] == "partial", f"status={meta['status']}")
        check("已翻部分被保留", 0 < done < len(paras), f"{done}/{len(paras)}")

        # ------------------------------------------------------------ 7
        print("\n== 7. 单段重译 ==")
        target = ids[3]
        out = T.retranslate_paragraph(doc_id, target, "术语统一译作「承诺」", settings)
        check("返回了译文", bool(out.get("zh")))
        check("已写入 translated.json",
              store.load_translations(doc_id)[target]["zh"] == out["zh"])

        # ------------------------------------------------------------ 8
        print("\n== 8. 流式问答 ==")
        text = "".join(T.ask_stream(doc_id, "这段在讲什么？", ids[1], ids[1] + " 的片段",
                                    settings))
        check("收到流式内容", len(text) > 20, f"{len(text)} 字符")
        check("上下文段落被带进提示", ids[1] in text)

        # ------------------------------------------------------------ 9
        print("\n== 9. 补满译文 → 导出 → 无头渲染 ==")
        T.translate_document(doc_id, settings, force=True)
        meta = store.get_meta(doc_id)
        doc = {"id": doc_id, "title": meta["title"],
               "paragraphs": store.load_extracted(doc_id)["paragraphs"],
               "translations": store.load_translations(doc_id), "meta": meta}
        html_path = tmp_dir / "pipeline-export.html"
        render.export_doc_to_file(doc, html_path)
        print(f"  已导出 {html_path.name}（{html_path.stat().st_size} 字节）")

        if JSDOM and JSDOM.exists() and _has_node():
            env = dict(os.environ, EE_JSDOM=str(JSDOM).replace("\\", "/"))
            p = subprocess.run([str(NODE), str(ROOT / "scripts" / "render_test.js"), str(html_path)],
                               capture_output=True, text=True, encoding="utf-8", env=env)
            print("\n".join("  " + l for l in (p.stdout or "").strip().splitlines()))
            if p.stderr.strip():
                print("  stderr:", p.stderr.strip()[:300])
            check("无头渲染自测通过", p.returncode == 0)
        else:
            print("  （跳过：未找到 node / jsdom）")
        # ------------------------------------------------------------ 10
        print("\n== 10. 只重建左栏（不重译）==")
        tr_now = store.load_translations(doc_id)
        zh_before = {k: (v.get("zh") or "") for k, v in tr_now.items()}
        store.save_translations(doc_id, {k: {"zh": v.get("zh") or "",
                                            "terms": v.get("terms") or []}
                                        for k, v in tr_now.items()})
        check("模拟旧文档：en 已被清空",
              not any((v or {}).get("en") for v in store.load_translations(doc_id).values()))
        r2 = T.restore_document(doc_id, settings)
        tr_after = store.load_translations(doc_id)
        check("重建无失败项", not r2["failed"], str(r2["failed"][:3]))
        check("重建了若干段", r2["newly"] > 0, f"改动 {r2['newly']} 段，未改动 {r2['unchanged']}")
        check("译文一字未动",
              all((tr_after[k].get("zh") or "") == zh_before[k] for k in zh_before))
        check("重启复跑不会重复重建（幂等）",
              T.restore_document(doc_id, settings).get("skipped") is True)
        # ------------------------------------------------------------ 12
        print("\n== 12. 无法辨认字形（PDF 字体表坏了）的处理 ==")
        # 背景：CMEX10 等数学字体的字形常被映射成控制字符/私有区码位，
        # 早期实现「静默丢弃」，导致公式少了括号、模型拿到残缺公式 → 翻译出错。
        # 现在必须替换成显式占位符，交给模型按上下文还原。
        mg = M.MISSING_GLYPH
        check("控制字符被替换为占位符",
              M.plain_text_escape("a\x10b") == f"a{mg}b", repr(M.plain_text_escape("a\x10b")))
        check("私有区字符被替换为占位符",
              M.plain_text_escape("\uf8ee") == mg, repr(M.plain_text_escape("\uf8ee")))
        check("替换符 U+FFFD 被替换", M.plain_text_escape("\ufffd") == mg)
        check("数学片段里同样处理",
              "\x10" not in M.text_to_latex("\x10Y") and mg in M.text_to_latex("\x10Y"),
              repr(M.text_to_latex("\x10Y")))
        check("占位符可计数", M.count_missing_glyphs(f"x{mg}y{mg}") == 2)
        check("正常字符不受影响", M.plain_text_escape("normal text") == "normal text")
        # 端到端：整篇抽取结果里不允许残留控制字符/私有区
        doc = extract_pdf(PDF, 1, 2)
        joined = "\n".join(p["text"] for p in doc["paragraphs"])
        residue = [c for c in joined
                   if (ord(c) < 0x20 and c not in "\n\t") or 0xE000 <= ord(c) <= 0xF8FF
                   or ord(c) == 0xFFFD]
        check("整篇抽取无残留乱码码位", not residue, str(residue[:6]))
        check("抽取结果带字形问题字段",
              isinstance(doc.get("glyph_issues"), int) and "glyph_issue_ids" in doc,
              f"glyph_issues={doc.get('glyph_issues')}")

        # 实测发现：一条独立公式会被 PDF 拆成多块，其中一块的可见字符可能**只剩 ⟦?⟧**
        # （大括号写成上下两截时就是这样，见用户论文 p0085/p0086）。这种碎片单独
        # 存在毫无意义 —— 模型只能给出空括号。必须在抽取期并回上一条公式。
        eq = {"id": "p1", "page": 9, "page_end": 9, "kind": "equation",
              "text": "x$a$ ⟦?⟧⟦?⟧", "math_ratio": 0.9, "bbox": [368.9, 186.8, 400.3, 220.7]}
        frag = {"id": "p2", "page": 9, "page_end": 9, "kind": "text",
                "text": "⟦?⟧⟦?⟧", "math_ratio": 0.0, "bbox": [368.9, 204.4, 375.5, 220.7]}
        outer = {"id": "p3", "page": 9, "page_end": 9, "kind": "text",
                 "text": "⟦?⟧⟦?⟧", "math_ratio": 0.0, "bbox": [500.0, 500.0, 520.0, 520.0]}
        check("纯占位符短段被识别为碎片", _is_glyph_only_fragment(frag))
        check("含正文的段不算碎片", not _is_glyph_only_fragment(eq))
        got = _merge_glyph_fragments([dict(eq), dict(frag)])
        check("碎片被并回上一条公式", len(got) == 1 and got[0]["id"] == "p1", str([g["id"] for g in got]))
        check("合并后占位符一个不丢",
              got[0]["text"].count(M.MISSING_GLYPH) == 4, repr(got[0]["text"]))
        check("记录合并来源便于排查", got[0].get("merged_fragments") == ["p2"])
        # 版面无重叠、且上一条不是公式时不许乱并（防止把独立小段吃掉）
        got2 = _merge_glyph_fragments([{"id": "t1", "page": 9, "page_end": 9, "kind": "text",
                                        "text": "a normal sentence.", "math_ratio": 0.0,
                                        "bbox": [10.0, 10.0, 200.0, 20.0]}, dict(outer)])
        check("不该并的碎片不并（无重叠且非公式）", len(got2) == 2, str([g["id"] for g in got2]))

        # ------------------------------------------------------------ 13
        print("\n== 13. 模型「用 ? 或空括号顶替占位符」的发现与重试 ==")
        # 实测模型会把 ⟦?⟧ 改写成 ⟨?⟩ —— 看起来像修好了，其实是在藏问题。
        # 更隐蔽的一种：被要求"不许出现问号"之后换成空括号 `\left[\;\right]`，
        # 语法合法、渲染成一对空框，内容照样丢了。两者都必须被判为未修复。
        check("`?` 顶替被判为未修复", bool(M.find_unrepaired(r"$\langle ? \rangle$")))
        check("空括号被判为未修复",
              bool(M.find_unrepaired(r"$$\left[\;\right]\left[\;\right]$$")),
              "空括号是最容易被漏掉的一种「伪装修复」")
        # 第三种伪装：一对空的矩阵单元格。被要求"不许空括号"之后模型就换这个。
        check("空矩阵单元格被判为未修复",
              bool(M.find_unrepaired(r"$$\left[\begin{matrix} \\ \end{matrix}\right]$$")))
        check("空 array（带列格式）同样被判为未修复",
              bool(M.find_unrepaired(r"$$\left[\begin{array}{c} \\ \end{array}\right]$$")))
        check("正常公式不误报",
              not M.find_unrepaired(r"$$\left[\begin{matrix}a \\ b\end{matrix}\right]$$")
              and not M.find_unrepaired(r"$a + b = c$")
              and not M.find_unrepaired(r"$$\|x\|_2^2$$")
              and not M.find_unrepaired(r"$$\sum_{j=1}^{N} x_j^{(0)} \cdot T[j]$$"))
        # 关键的反向保护：源文本里本来就没有占位符时，译文里的 `?` 是**原文自带**的
        # （实测论文里有 `$?=$` 这种真问号），不能判成"模型藏了问题"，
        # 否则这些段会被「修复公式段」按钮永久选中、反复重译。
        check("源里无占位符时，原文自带的 ? 不算异常",
              not M.find_unrepaired(r"$?=$", had_placeholder=False),
              "误报会让按钮永远不收敛")
        check("同一段若源里有占位符，则 ? 仍算异常",
              bool(M.find_unrepaired(r"$?=$", had_placeholder=True)))
        # mock 刻意模仿这个行为：第一次给 ?，收到加强指令后才真正还原。
        probe = store.create_doc("占位符重试测试", PDF, "p.pdf")
        pdata = extract_pdf(PDF, 1, 1)
        pdata["paragraphs"][0]["text"] = "⟦?⟧Y $\\cdot \\cdot \\cdot$ ⟦?⟧Y"
        pdata["paragraphs"][0]["kind"] = "equation"
        pid0 = pdata["paragraphs"][0]["id"]
        store.save_extracted(probe, pdata)
        client.MOCK_BAD_REPAIR = False
        T.translate_document(probe, settings, force=True)
        rec = (store.load_translations(probe).get(pid0) or {})
        blob = (rec.get("en") or "") + (rec.get("zh") or "")
        check("第一次被 ? 顶替 → 自动重试并修好", "?" not in blob and "⟦?⟧" not in blob,
              (rec.get("en") or "")[:80])
        check("重试后给的是有内容的括号（不是空括号）",
              not M.find_unrepaired(rec.get("en") or ""),
              (rec.get("en") or "")[:80])
        check("修好后不标记为待确认", rec.get("unrepaired") is not True)

        client.MOCK_BAD_REPAIR = True
        store.save_translations(probe, {})
        T.translate_document(probe, settings, force=True)
        rec2 = (store.load_translations(probe).get(pid0) or {})
        check("确实修不好时如实标记 unrepaired", rec2.get("unrepaired") is True,
              f"unrepaired={rec2.get('unrepaired')} en={(rec2.get('en') or '')[:40]!r}")
        check("标记的段落仍保留模型输出（不丢译文）", bool(rec2.get("zh")))
        client.MOCK_BAD_REPAIR = False

        # ------------------------------------------------------------ 14
        print("\n== 14. 按字体字形名确定性还原大符号 ==")
        # 坏掉的 ToUnicode 把 CMEX10 的字形变成控制字符/私有区，但字体自己的
        # /Encoding 里有可读的 TeX 字形名。据此可以**不靠模型猜**就还原
        # 大括号、求和号、大 ⊕（见 app/glyphnames.py）。
        from app import glyphnames as G
        check("能解析 Type1 的 /Encoding 表",
              G._parse_encoding(
                  b"/Encoding 256 array\n0 1 255 {1 index exch /.notdef put} for\n"
                  b"dup 16 /parenleftBig put\ndup 77 /circleplusdisplay put\n"
                  b"readonly def") == {16: "parenleftBig", 77: "circleplusdisplay"})
        # 子集前缀必须规范化：字体表里是 VFYNJW+CMEX10，span 里常常只有 CMEX10
        check("字体名去掉子集前缀", G._norm_font("VFYNJW+CMEX10") == "cmex10"
              and G._norm_font("CMEX10") == "cmex10"
              and G._norm_font("AB1234+CMEX10") == "cmex10")
        encs = {"cmex10": {0x10: "parenleftBig", 0x11: "parenrightBig",
                           0x4D: "circleplusdisplay", 0x50: "summationtext",
                           0x32: "bracketlefttp"}}
        rv = G.make_resolver("ABCDEF+CMEX10", encs)
        check("大括号按字形名还原", rv("\x10") == r"\big(" and rv("\x11") == r"\big)")
        check("大 ⊕ 按字形名还原", rv("M") == r"\bigoplus")
        check("求和号按字形名还原", rv("P") == r"\sum")
        # 关键的安全性：拼接片段（一个大 [ 被拆成上/中/下三块）故意不映射，
        # 逐块映射会输出 `[[[`，比占位符更糟。
        check("拼接片段不映射（否则会输出 [[[）", rv("\uf8ee") is None and rv("\x32") is None)
        check("认不出的字符返回 None，交回原逻辑", rv("x") is None and rv("\u2200") is None)
        check("非 CMEX 字体不参与解析",
              G.make_resolver("ABCDEF+CMR10", {"cmr10": {0x10: "parenleftBig"}}) is None)
        # 端到端：整篇抽取仍不许残留乱码码位（解析器不能把新字符漏进去）
        doc2 = extract_pdf(PDF, 1, 2)
        joined2 = "\n".join(p["text"] for p in doc2["paragraphs"])
        bad2 = [c for c in joined2
                if (ord(c) < 0x20 and c not in "\n\t") or 0xE000 <= ord(c) <= 0xF8FF
                or ord(c) == 0xFFFD]
        check("接上字形名解析后仍无残留乱码码位", not bad2, str(bad2[:6]))
    finally:
        # 注意：清理必须放 finally，而**测试节必须留在 try 里** ——
        # 第 12/13 节依赖 T.make_client 被替换成计数用 mock，一旦写到 finally
        # 后面，桩已被 real_make 覆盖，mock 的行为（含 MOCK_BAD_REPAIR）全部失效。
        T.make_client = real_make
        shutil.rmtree(tmp_dir, ignore_errors=True)
        for d in (probe, doc_id):
            if not d:
                continue
            if d == doc_id and args.keep:
                print(f"\n  保留测试文档 {doc_id}（data/docs/{doc_id}）")
                continue
            store.delete_doc(d)
            print(f"\n  已清理测试文档 {d}")

    print(f"\n== 结果：{len(PASS)} 项通过，{len(FAIL)} 项失败 ==")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        sys.exit(1)


if __name__ == "__main__":
    main()
