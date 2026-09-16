"""生成「离线效果样例」HTML。

用途：不配置 API Key 也能立刻看到左英右中对照的真实排版效果。
做法：取已有文档的抽取结果 + 一份人工/模型译文，渲染成自包含的 HTML，
     并把 MathJax 复制到同目录，保证断网也能正常渲染公式。

用法：
    python scripts/make_demo.py --doc 20260912-133603-plonk --first 24
    python scripts/make_demo.py --extracted a.json --translations b.json --out out/
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import render, store  # noqa: E402
from app.config import WEB_DIR  # noqa: E402

# Windows 控制台（含 CI runner）默认不是 UTF-8，不切的话下面打印中文/符号会
# UnicodeEncodeError 直接崩 —— 本地 Git Bash 是 UTF-8，所以只在 CI 上暴露。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DEFAULT_OUT = ROOT / "samples" / "demo-plonk"
DEFAULT_TRANSLATIONS = DEFAULT_OUT / "translations.json"


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 EasyEssay 离线样例 HTML")
    ap.add_argument("--doc", help="data/docs 下的文档 id（用已抽取+已翻译的文档）")
    ap.add_argument("--first", type=int, default=0, help="只取前 N 段")
    ap.add_argument("--extracted", help="直接指定 extracted.json")
    ap.add_argument("--translations", help="直接指定 translations.json")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    ap.add_argument("--title", default="", help="覆盖标题")
    ap.add_argument("--filename", default="", help="覆盖输出文件名（不含扩展名）")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.doc:
        extracted = store.load_extracted(args.doc)
        translations = store.load_translations(args.doc)
        meta = store.get_meta(args.doc)
        title = args.title or meta.get("title") or args.doc
        # 用人工翻译覆盖/补全（样例目录里的 translations.json 优先）
        if DEFAULT_TRANSLATIONS.exists():
            manual = json.loads(DEFAULT_TRANSLATIONS.read_text(encoding="utf-8"))
            translations = {**translations, **manual}
    else:
        extracted = json.loads(Path(args.extracted).read_text(encoding="utf-8"))
        translations = json.loads(Path(args.translations).read_text(encoding="utf-8"))
        meta = {}
        title = args.title or extracted.get("title") or "EasyEssay 样例"

    paragraphs = extracted.get("paragraphs", [])
    if args.first:
        paragraphs = paragraphs[: args.first]
    keep = {p["id"] for p in paragraphs}
    translations = {k: v for k, v in translations.items() if k in keep}

    doc = {
        # 保留真实文档 id：样例 HTML 在与本地服务同机时，「问 AI」可直接连回该文档
        "id": args.doc or "",
        "title": title,
        "paragraphs": paragraphs,
        "translations": translations,
        "meta": {
            "source_name": meta.get("source_name", "plonk.pdf"),
            "page_count": extracted.get("page_count"),
            "created_at": meta.get("created_at", ""),
            "settings": {"model": "（样例：人工翻译 · 未调用 API）"},
            "stats": {"paragraphs": len(paragraphs), "translated": len(translations)},
        },
    }

    safe = render.safe_filename(args.filename or title)
    html_path = out_dir / f"{safe}-中英对照示例.html"
    render.export_doc_to_file(doc, html_path)

    # 复制 MathJax，保证断网可渲染
    src = WEB_DIR / "vendor" / "mathjax" / "tex-svg.js"
    dst_dir = out_dir / "vendor" / "mathjax"
    if src.exists():
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst_dir / "tex-svg.js")

    # 冻结输入，便于复现
    (out_dir / "extracted.json").write_text(
        json.dumps(extracted, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"已生成：{html_path}")
    print(f"  段落 {len(paragraphs)} 段，其中已译 {len(translations)} 段")
    if src.exists():
        print(f"  MathJax 已复制到：{dst_dir / 'tex-svg.js'}")


if __name__ == "__main__":
    main()
