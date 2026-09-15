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

import app.translate as T  # noqa: E402
from app import render, store  # noqa: E402
from app.extract import extract_pdf  # noqa: E402
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
    finally:
        T.make_client = real_make
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if not args.keep:
            store.delete_doc(doc_id)
            print(f"\n  已清理测试文档 {doc_id}")
        else:
            print(f"\n  保留测试文档 {doc_id}（data/docs/{doc_id}）")

    print(f"\n== 结果：{len(PASS)} 项通过，{len(FAIL)} 项失败 ==")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        sys.exit(1)


if __name__ == "__main__":
    main()
