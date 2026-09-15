"""EasyEssay 命令行入口：PDF → 中英对照 HTML，不开浏览器也能用。

用法示例：
    # 转前 10 页，输出到 out/
    python -m app.cli paper.pdf --pages 1-10 -o out/paper.html

    # 只抽取不翻译（先看版面与公式还原效果）
    python -m app.cli paper.pdf --pages 1-3 --no-translate -o out/raw.html

    # 自定义系统提示词 + 指定模型
    python -m app.cli paper.pdf --model deepseek-chat \
        --system-prompt "把论文翻译成中文，术语首次出现标注英文原词"

    # 离线自测（不需要 API Key，产出模拟译文）
    python -m app.cli paper.pdf --mock -o out/mock.html

文档默认会留在文档库里（可用网页继续阅读/追问），加 --temp 则在导出后删除。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import render, store, translate
from .config import load_settings
from .extract import extract_pdf, extract_with_pdfplumber


def _parse_pages(text: str) -> tuple[int | None, int | None]:
    if not text:
        return None, None
    if "-" in text:
        a, b = text.split("-", 1)
        return int(a), int(b)
    n = int(text)
    return n, n


def build_site_cli(argv: list[str] | None = None) -> int:
    from .site import build_site

    ap = argparse.ArgumentParser(
        prog="python -m app.cli site",
        description="把文档库里所有论文生成为静态「论文阅读库」站点（托管到域名/静态空间用）")
    ap.add_argument("--out", default="site", help="输出目录，默认 ./site")
    ap.add_argument("--title", default="论文阅读库", help="站点标题")
    ap.add_argument("--sub", default="", help="站点副标题")
    ap.add_argument("--only", default="", help="只导出指定文档 id（逗号分隔）")
    ap.add_argument("--domain", default="",
                    help="自定义域名，写进 CNAME 供 GitHub Pages 绑定（如 example.com）")
    ap.add_argument("--all", action="store_true",
                    help="连同其它账号的文档一起导出（⚠ 会把朋友的私人论文公开，默认不导出）")
    args = ap.parse_args(argv)

    only = [x.strip() for x in args.only.split(",") if x.strip()] or None
    r = build_site(args.out, only, args.title, args.sub, domain=args.domain,
                   include_all=args.all)
    print(f"已生成站点：{r['index']}")
    print(f"  论文 {r['papers']} 篇，目录：{r['out']}")
    if r.get("skipped_other_users"):
        print(f"  已跳过 {r['skipped_other_users']} 篇属于其它账号的文档（不会公开）")
    print("  直接把整个目录上传到静态空间（或指向域名的根目录）即可访问。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="EasyEssay —— 把 PDF 论文转成左英右中对照 HTML")
    ap.add_argument("pdf", nargs="?", help="PDF 文件路径；用 `site` 生成静态阅读库")
    ap.add_argument("--site", action="store_true", help="生成静态阅读库站点（等价于 python -m app.cli site）")
    ap.add_argument("-o", "--out", help="输出 HTML 路径（默认与 PDF 同目录）")
    ap.add_argument("--pages", default="", help="页码范围，如 1-10 或 5")
    ap.add_argument("--title", default="", help="文档标题（默认自动识别）")
    ap.add_argument("--model", default="", help="模型名（默认取设置）")
    ap.add_argument("--system-prompt", default="", help="覆盖翻译系统提示词")
    ap.add_argument("--target-lang", default="", help="目标语言，默认简体中文")
    ap.add_argument("--batch", type=int, default=0, help="每批段落数")
    ap.add_argument("--no-translate", action="store_true", help="只抽取，不翻译")
    ap.add_argument("--restore-only", action="store_true",
                    help="只重建左栏公式，不翻译（已有译文时用它修公式，花费远低于重译）")
    ap.add_argument("--no-formula-fix", action="store_true", help="关闭公式修复提示")
    ap.add_argument("--mock", action="store_true", help="离线模拟（无需 API Key，产出占位译文）")
    ap.add_argument("--temp", action="store_true", help="导出后删除文档库里的记录")
    ap.add_argument("--quiet", action="store_true", help="不打印逐批进度")
    args, unknown = ap.parse_known_args(argv)

    # `python -m app.cli site ...` 走站点生成
    if args.pdf == "site" or args.site:
        return build_site_cli(unknown + ([] if not args.pdf or args.pdf == "site" else [args.pdf]))

    if not args.pdf:
        ap.print_help()
        return 2

    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"找不到文件：{pdf}", file=sys.stderr)
        return 2

    p_from, p_to = _parse_pages(args.pages)
    settings: dict = {}
    if args.mock:
        settings["provider"] = "mock"
    if args.model:
        settings["model"] = args.model
    if args.system_prompt:
        settings["system_prompt"] = args.system_prompt
    if args.target_lang:
        settings["target_lang"] = args.target_lang
    if args.batch:
        settings["translate_batch_size"] = args.batch
    if args.no_formula_fix:
        settings["fix_formula"] = False

    # ---------------------------------------------------------------- 抽取
    print(f"[1/3] 解析 {pdf.name} ……")
    data = extract_pdf(pdf, p_from, p_to, use_ocr=True)
    if not data.get("paragraphs"):
        print("      PyMuPDF 未取到文本，改用 pdfplumber 兜底 ……")
        try:
            data = extract_with_pdfplumber(pdf)
        except Exception as e:  # noqa: BLE001
            print(f"      兜底也失败：{e}", file=sys.stderr)
    paras = data.get("paragraphs", [])
    if not paras:
        print("      没有抽取到任何段落（扫描件请先安装 requirements-ocr.txt）", file=sys.stderr)
        return 3
    kinds: dict[str, int] = {}
    for p in paras:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    formulas = sum(1 for p in paras if "$" in p["text"])
    print(f"      共 {len(paras)} 段（" + "，".join(f"{k}×{v}" for k, v in kinds.items()) +
          f"），其中 {formulas} 段含公式")
    if data.get("ocr_pages"):
        print(f"      其中 {len(data['ocr_pages'])} 页走了 OCR：{data['ocr_pages'][:10]}")

    doc_id = store.create_doc(args.title or data.get("title") or pdf.stem,
                              pdf, pdf.name, settings=settings)
    store.save_extracted(doc_id, data)
    print(f"      文档 id：{doc_id}")

    # ---------------------------------------------------------------- 翻译
    if args.no_translate:
        print("[2/3] 跳过翻译（--no-translate）")
    elif args.restore_only:
        print("[2/3] 只重建左栏公式（不翻译）……")
        eff = load_settings()
        eff.update(settings)
        if (eff.get("provider") or "").lower() != "mock" and not eff.get("api_key"):
            print("      未配置 API Key。", file=sys.stderr)
            return 4

        def on_restore(info: dict) -> None:
            if not args.quiet:
                print(f"      批次 {info['batch']}/{info['batches']}"
                      f"（已处理 {info['completed']}/{info['total']} 段）")

        r = translate.restore_document(doc_id, settings, progress=on_restore)
        if r.get("skipped"):
            print("      左栏无需重建")
        else:
            print(f"      完成：重建 {r['newly']} 段，未改动 {r['unchanged']} 段"
                  + (f"，失败 {len(r['failed'])} 段" if r["failed"] else ""))
    else:
        print("[2/3] 逐段翻译 ……")
        eff = load_settings()
        eff.update(settings)
        if (eff.get("provider") or "").lower() != "mock" and not eff.get("api_key"):
            print("      未配置 API Key。用 --mock 可离线跑通流程，"
                  "或在网页「设置」/ .env 里填写。", file=sys.stderr)
            return 4

        def on_progress(info: dict) -> None:
            if not args.quiet:
                pct = info["progress"] * 100
                print(f"      批次 {info['batch']}/{info['batches']} "
                      f"（{info['translated']}/{info['paragraphs']} 段，{pct:.0f}%）")

        res = translate.translate_document(doc_id, settings, progress=on_progress)
        if res.get("skipped"):
            print("      无需翻译")
        else:
            print(f"      完成：新译 {res['newly']} 段，累计 {res['translated']}/{res['total']} 段"
                  + (f"，失败 {len(res['failed'])} 段" if res["failed"] else ""))

    # ---------------------------------------------------------------- 导出
    print("[3/3] 导出对照 HTML ……")
    meta = store.get_meta(doc_id)
    doc = {"id": doc_id, "title": meta.get("title"),
           "paragraphs": store.load_extracted(doc_id).get("paragraphs", []),
           "translations": store.load_translations(doc_id), "meta": meta}
    out = Path(args.out) if args.out else pdf.with_name(
        f"{pdf.stem}-中英对照.html")
    if out.is_dir():
        out = out / (render.safe_filename(meta.get("title") or pdf.stem) + "-中英对照.html")
    render.export_doc_to_file(doc, out)
    print(f"      → {out.resolve()}")

    if args.temp:
        store.delete_doc(doc_id)
        print(f"      已删除文档库记录 {doc_id}")
    else:
        print(f"      文档已保留，可在网页里继续读：/reader?doc={doc_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
