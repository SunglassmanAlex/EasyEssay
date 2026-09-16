"""交付验收自检：把规格第 10 节的验收清单变成可执行断言。

用法：
    python scripts/conformance_check.py <导出.html> [--doc <doc_id>] [--pdf <source.pdf>]

为什么单独一个脚本：这 8 条**每一条都可以确定性地判定**，
不该靠"看起来还行"来交付。放在这里，改渲染/改抽取后跑一遍就知道有没有退化。

设计取舍（重要）：
* **数据层与渲染层分开查**。`$` 转义、术语标记这类问题只在 DOM 里现形；
  表格数值、页码这类问题在数据里查更快更准。两边都要查。
* 有问题**逐条打印证据**（哪个 id / 哪个字符串），否则修的时候还得再找一遍。
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


# ---------------------------------------------------------------- 数据层

def _iter_cell_texts(grid: dict):
    for row in grid.get("rows") or []:
        for cell in row:
            if isinstance(cell, dict):
                yield cell.get("text") or ""


def check_tables(doc_id: str) -> None:
    """§3/§10：表数量、列数、数据在 en / zh 两侧都在、数值不被翻译。"""
    from app import store
    ex = store.load_extracted(doc_id)
    tr = store.load_translations(doc_id)
    tables = [p for p in ex["paragraphs"] if p.get("kind") == "table"]
    check("表格数量不为 0", len(tables) > 0, f"{len(tables)} 张")
    bad_cols, no_zh, num_lost = [], [], []
    for p in tables:
        grid = p.get("table") or {}
        zd = (tr.get(p["id"]) or {}).get("table") or {}
        if not zd:
            no_zh.append(p["id"])
            continue
        if zd.get("columns") != grid.get("columns"):
            bad_cols.append(f"{p['id']}(en {grid.get('columns')} vs zh {zd.get('columns')})")
        # 数值必须逐字一致（表格数据不翻译）
        nums_en = set(re.findall(r"\d[\d,.]*", " ".join(_iter_cell_texts(grid))))
        nums_zh = set(re.findall(r"\d[\d,.]*", " ".join(_iter_cell_texts(zd))))
        lost = {n for n in nums_en if n not in nums_zh}
        if lost:
            num_lost.append(f"{p['id']}:{sorted(lost)[:4]}")
    check("每张表都有中文版", not no_zh, f"缺 {no_zh}" if no_zh else f"{len(tables)} 张")
    check("中英表列数一致", not bad_cols, "; ".join(bad_cols[:3]))
    check("表格数值逐字保留（不清零/不翻译）", not num_lost, "; ".join(num_lost[:3]))


def check_terms(doc_id: str) -> None:
    """§7：术语表非空；且**译文里真的有标记**（否则前端无法高亮）。"""
    from app import store
    tr = store.load_translations(doc_id)
    n_terms = sum(len((r or {}).get("terms") or []) for r in tr.values())
    gl = store.load_glossary(doc_id)
    check("存在全局术语表（术语高亮的唯一真源）", len(gl) > 0, f"{len(gl)} 条")
    check("译文里带术语数组", n_terms > 0, f"{n_terms} 条（逐段）")


def check_pages(doc_id: str) -> None:
    """§8：每个 block 有页码；页栏标签单调；每一页都有 block。"""
    from app import store
    ex = store.load_extracted(doc_id)
    meta = store.get_meta(doc_id)
    ps = ex["paragraphs"]
    missing = [p["id"] for p in ps if not p.get("page")]
    check("每个 block 都记录原文页码", not missing, f"缺 {len(missing)} 个")
    pages = sorted({int(p.get("page") or 0) for p in ps})
    total = int(meta.get("page_count") or 0)
    gaps = [n for n in range(1, total + 1) if n not in pages] if total else []
    check("原文每一页都有对应 block", not gaps, f"无 block 的页：{gaps[:8]}" if gaps else f"{total} 页全覆盖")
    ids = [p["id"] for p in ps]
    seq = [int(m.group(1)) for i in ids if (m := re.match(r"p(\d+)$", i))]
    holes = [n for n in range(1, max(seq) + 1) if n not in seq] if seq else []
    check("block 编号不跳号", not holes, f"缺号 {holes[:8]}" if holes else f"{len(seq)} 个连续")


def check_page_labels(doc_id: str) -> None:
    """§8/§10：页栏标签必须单调递增（浮动图表按原印刷顺序排的结果）。"""
    from app import store
    ex = store.load_extracted(doc_id)
    pages = [int(p.get("page") or 0) for p in ex["paragraphs"]]
    back = [(i, pages[i - 1], pages[i]) for i in range(1, len(pages)) if pages[i] < pages[i - 1]]
    check("页栏标签单调递增（无回跳）", not back,
          f"{len(back)} 处回跳，例如第{back[0][0]}段 {back[0][1]}→{back[0][2]}" if back
          else f"共 {len(pages)} 段，{min(pages)}→{max(pages)}")


def check_fidelity(doc_id: str) -> None:
    """§10 保真度：英文侧压缩后应与原文一致（子串匹配）。

    压缩 = 只留字母数字 + 转小写。原文取 PDF 全文（PyMuPDF）。
    未命中的多半是「公式被转成 LaTeX」「连字符断词还原」「图表插断」这类
    **可接受**的差异；真正要抓的是"整段不在原文里"（= 抽取时丢词/串行）。
    """
    from app import store
    src = store.source_path(doc_id)
    if not src:
        print("  · 找不到源文件，跳过保真度检查")
        return
    try:
        import pymupdf
        doc = pymupdf.open(src)
        raw = "".join(pg.get_text() for pg in doc)
        doc.close()
    except Exception as e:  # noqa: BLE001
        print(f"  · 读取原文失败（{str(e)[:40]}），跳过保真度检查")
        return

    def squash(t: str) -> str:
        """归一化：解 HTML 实体、去掉 LaTeX 命令，再只留字母数字小写。

        ⚠️ 不归一化会误报：`&` 被转义成 `&amp;`（压缩后多出 "amp"）、
        公式被转成 LaTeX（`$	heta$`）—— 这些是**正确行为**，不是抽取错误。
        第一版要求"整段匹配"，误报率 18%（实测未命中的全是这两类）。
        """
        t = html.unescape(t or "")
        t = re.sub(r"\[a-zA-Z]+", "", t)      # LaTeX 命令
        return re.sub(r"[^a-z0-9]", "", t.lower())

    hay = squash(raw)
    ex = store.load_extracted(doc_id)
    kinds = ("text", "heading", "abstract", "caption", "note")
    miss, total = [], 0
    for p in ex["paragraphs"]:
        if p.get("kind") not in kinds:
            continue
        needle = squash(p.get("text") or "")
        if len(needle) < 40:          # 太短的片段（标题词）噪声大
            continue
        total += 1
        # 按**前缀**匹配：正文里夹着公式/符号时，整段不会逐字相同，
        # 但开头 40 字必须能在原文里找到（找不到才是真丢词/串行）
        if needle[:40] not in hay:
            miss.append(p["id"])
    rate = 1 - len(miss) / max(1, total)
    check("保真度：英文侧开头能在原文里找到", rate >= 0.95,
          f"命中 {total - len(miss)}/{total} = {rate*100:.0f}%"
          + (f"，可疑 {miss[:5]}" if miss else ""))


def check_coverage(doc_id: str) -> None:
    """§10：status 不为 partial；100% 段落有 zh；zh 内不得残留整句英文。"""
    from app import store
    ex = store.load_extracted(doc_id)
    tr = store.load_translations(doc_id)
    meta = store.get_meta(doc_id)
    ps = ex["paragraphs"]
    done = [p["id"] for p in ps if (tr.get(p["id"]) or {}).get("zh")]
    rate = len(done) / max(1, len(ps))
    check("100% 段落有译文", len(done) == len(ps), f"{len(done)}/{len(ps)} = {rate*100:.0f}%")
    check("status 不为 partial", meta.get("status") != "partial", str(meta.get("status")))
    # zh 里残留"整句英文"。
    # ⚠️ 规格明确把"专有名词 / 引文标题"排除在外，所以判据必须区分：
    #   · 连续**小写开头**的英文词 → 是正文句子漏译 → 违规
    #   · 连续首字母大写（Matei Zaharia / Social Security Number）→ 专有名词 → 允许
    # 一开始只按"4 个以上英文词"一刀切，报出 78 处，绝大多数是作者名与数据集名，
    # 属于**假失败** —— 判据太粗会让真正的漏译淹没在噪声里。
    residue, proper = [], 0
    # 只查**正文类**块。规格自己写了两条例外，必须照办，否则判据就是在冤枉正确行为：
    #   · §5「图内文字不是正文：坐标轴刻度、流程框内的词、图例，都不要当段落翻译」
    #   · §9「参考文献的作者列表/URL/年份要整条保留」
    # 不排除的话，图的图例（"Plaintext-HNSW slow …"）与文献标题会被当成漏译报出来
    # （实测 55 处"违规"里绝大多数是这两类）。
    prose_kinds = {"text", "heading", "abstract", "note"}
    for p in ps:
        if p.get("kind") not in prose_kinds:
            continue
        zh = (tr.get(p["id"]) or {}).get("zh") or ""
        for m in re.finditer(r"(?:[A-Za-z][A-Za-z'\-]*\s+){3,}[A-Za-z][A-Za-z'\-]*", zh):
            phrase = m.group(0).strip()
            if len(phrase) <= 24:
                continue
            words = phrase.split()
            lower_start = sum(1 for w in words if w[:1].islower())
            if lower_start >= 2:          # 有实义小写词 → 是句子，不是专名
                residue.append(f"{p['id']}:{phrase[:44]}")
            else:
                proper += 1
            break
    check("译文里没有残留的整句英文（专有名词除外）", not residue,
          f"违规 {len(residue)} 处：{residue[:3]}" if residue
          else f"0 处；另有 {proper} 处专有名词（作者名/数据集名，规格允许）")


# ---------------------------------------------------------------- 渲染层

def rendered_dom(html_path: Path) -> str | None:
    """用真实浏览器取渲染后的 DOM。

    ⚠️ 必须用 DOM 而不是源码：导出文件里**内联了渲染脚本与样式**，
    源码里 JS 字符串中的 `$`、`'<div'` 会被当成"未转义公式""标签不配平"（踩过，
    一上来就报了 6 个假失败）。
    """
    import subprocess
    exe = None
    for cand in (r"C:/Program Files/Google/Chrome/Application/chrome.exe",
                 r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"):
        if Path(cand).exists():
            exe = cand
            break
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "--headless=new", "--disable-gpu", "--no-sandbox",
                            "--virtual-time-budget=12000", "--dump-dom",
                            html_path.resolve().as_uri()],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    except Exception:  # noqa: BLE001
        return None
    return r.stdout or None


def strip_inline(html: str) -> str:
    """退路：去掉内联 <script>/<style>，只留页面结构。"""
    html = re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style\b.*?</style>", "", html, flags=re.S | re.I)
    return html


def check_html(html: str, doc_id: str | None) -> None:
    """§6/§8/§10：标签配平、$ 转义、分页标记两栏、术语标记与表格内无标记。"""
    n_div = len(re.findall(r"<div\b", html))
    n_div_end = len(re.findall(r"</div>", html))
    check("HTML 标签配平（<div> == </div>）", n_div == n_div_end, f"{n_div} vs {n_div_end}")
    for tag in ("table", "tr", "td", "th", "ol", "li"):
        a, b = len(re.findall(rf"<{tag}\b", html)), len(re.findall(rf"</{tag}>", html))
        if a != b:
            check(f"标签配平 <{tag}>", False, f"{a} vs {b}")

    # $ 转义：排除 \$ 后 $ 必须成对
    stripped = html.replace("\\$", "")
    n_dollar = stripped.count("$")
    check("排除 \\$ 后 $ 计数为偶数（公式成对）", n_dollar % 2 == 0, f"{n_dollar} 个")

    # 公式区间的裸 < / >（写进 HTML 会被当标签）
    bare = []
    for m in re.finditer(r"\$([^$\n]{0,200})\$", html):
        seg = m.group(1)
        if "<" in seg and "&lt;" not in seg:
            bare.append(seg[:40])
        if ">" in seg and "&gt;" not in seg:
            bare.append(seg[:40])
    check("$…$ 内没有裸 < / >", not bare, f"{len(bare)} 处：{bare[:2]}")

    # 页面标记行必须左右两栏都在
    pbreak = re.findall(r'<div class="[^"]*\bpbreak\b[^"]*"[^>]*>(.*?)</div>\s*</div>', html, re.S)
    if pbreak:
        one_sided = [b[:40] for b in pbreak if b.count('class="en"') + b.count('class="zh"') < 2]
        check("分页标记行保留左右两栏", not one_sided, f"{len(one_sided)} 行只有一栏")
    else:
        # 没有分页标记行：不判失败，但如实说明（本项目的导出只用段落开头的页栏）
        print("  · 本导出没有分页标记行（只有段落页栏 p.N）")

    # 术语标记不能出现在表格里
    tbl_html = re.findall(r"<table\b.*?</table>", html, re.S)
    in_table = sum(t.count('class="term"') for t in tbl_html)
    check("表格内不做术语高亮", in_table == 0, f"表内 {in_table} 处")


def main() -> int:
    ap = argparse.ArgumentParser(description="交付验收自检（规格第 10 节）")
    ap.add_argument("html", help="导出的对照 HTML")
    ap.add_argument("--doc", help="doc_id（用于查数据层）")
    ap.add_argument("--data-dir", default="",
                    help="文档所在的数据目录（exe 版是 dist/data；默认用项目的 data/）")
    args = ap.parse_args()

    if args.data_dir:
        # 文档可能在别的数据目录里（exe 版在 dist/data），要先把 store 指过去，
        # 否则数据层全查 0 —— 工具查错了目录，比没查更误导（踩过）
        import app.config as C
        d = Path(args.data_dir).resolve()
        C.DATA_DIR, C.DOCS_DIR, C.UPLOAD_DIR = d, d / "docs", d / "uploads"
        from app import store as _store
        _store.DATA_DIR, _store.DOCS_DIR = d, d / "docs"

    path = Path(args.html)
    if not path.exists():
        print(f"文件不存在：{path}")
        return 2
    dom = rendered_dom(path)
    if dom:
        html = strip_inline(dom)
        print("（渲染层在真实浏览器的 DOM 上检查）")
    else:
        html = strip_inline(path.read_text(encoding="utf-8"))
        print("（未找到浏览器，退回源码检查 —— 已去掉内联脚本/样式）")

    print("== 渲染层 ==")
    check_html(html, args.doc)

    if args.doc:
        print("\n== 数据层 ==")
        check_tables(args.doc)
        check_pages(args.doc)
        check_page_labels(args.doc)
        check_fidelity(args.doc)
        check_terms(args.doc)
        check_coverage(args.doc)

    print(f"\n== 结果：{len(PASS)} 项通过，{len(FAIL)} 项失败 ==")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
