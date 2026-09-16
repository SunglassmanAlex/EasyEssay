"""对一个**已存在的文档**跑翻译（续译 / 补齐到 100%）。

为什么要这个脚本：网页里的「继续翻译」按钮在 exe 窗口里，而
"重抽之后补齐译文""导出前把覆盖率做到 100%"这类事情适合在命令行做，
也便于放进自动化（例如打包前跑一遍验收自检）。

用法：
    python scripts/translate_doc.py --doc <doc_id>                    # 续译（只补缺的）
    python scripts/translate_doc.py --doc <doc_id> --data-dir dist/data
    python scripts/translate_doc.py --doc <doc_id> --force            # 全部重译

注意：会真实调用大模型 API（花调用者的额度）。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="对一个已有文档跑翻译")
    ap.add_argument("--doc", required=True, help="doc_id")
    ap.add_argument("--data-dir", default="", help="数据目录（exe 版是 dist/data）")
    ap.add_argument("--force", action="store_true", help="全部重译（默认只补缺的）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.data_dir:
        import app.config as C
        d = Path(args.data_dir).resolve()
        C.DATA_DIR, C.DOCS_DIR, C.UPLOAD_DIR = d, d / "docs", d / "uploads"
        from app import store as _s
        _s.DATA_DIR, _s.DOCS_DIR = d, d / "docs"

    from app import store, translate

    meta = store.get_meta(args.doc)
    if not meta:
        print(f"文档不存在：{args.doc}")
        return 2

    settings = {"provider": "deepseek"}
    t0 = time.time()
    last = [0.0]

    def on_progress(info: dict) -> None:
        if args.quiet or time.time() - last[0] < 5:
            return
        last[0] = time.time()
        pct = info["progress"] * 100
        print(f"  第 {info['batch']}/{info['batches']} 批 · "
              f"已译 {info['translated']}/{info['paragraphs']} 段（{pct:.0f}%）"
              f" · 用时 {time.time() - t0:.0f}s", flush=True)

    print(f"文档：{meta.get('title')}")
    print(f"段落：{(meta.get('stats') or {}).get('paragraphs')}"
          f" · 已译 {(meta.get('stats') or {}).get('translated')}")
    res = translate.translate_document(args.doc, settings, force=args.force,
                                       progress=on_progress)
    print(f"\n完成：新译 {res['newly']} 段，累计 {res['translated']}/{res['total']} 段"
          f"（{res['translated'] / max(1, res['total']) * 100:.0f}%），"
          f"用时 {time.time() - t0:.0f}s")
    if res.get("failed"):
        print(f"失败 {len(res['failed'])} 段：{res['failed'][:6]}")
    terms = res.get("terms") or {}
    if terms:
        print(f"术语：冲突 {terms.get('conflicts', 0)} 个，统一 {terms.get('replaced', 0)} 处")
    meta = store.get_meta(args.doc)
    print(f"status={meta.get('status')} · {meta.get('message')}")
    return 0 if meta.get("status") == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
