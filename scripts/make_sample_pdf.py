"""生成**合成**测试样张 PDF（无版权问题，供全新 clone 的仓库/CI 使用）。

为什么需要：samples/ 里的真实论文 PDF 不随仓库分发（版权），
但自测需要一份 PDF 作为输入。本脚本用 PyMuPDF 现场画一份两页的"伪论文"：
标题、作者行、摘要、章节标题、含 $...$ 的公式行、一个小表格、参考文献 ——
覆盖抽取器关心的大部分结构（层级、公式、表格、跨栏）。

用法：
    python scripts/make_sample_pdf.py            # 生成 samples/sample-paper.pdf
    python scripts/make_sample_pdf.py --force    # 覆盖已有文件
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Windows 控制台（含 CI runner）默认不是 UTF-8，不切的话下面打印中文/符号会
# UnicodeEncodeError 直接崩 —— 本地 Git Bash 是 UTF-8，所以只在 CI 上暴露。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "samples" / "sample-paper.pdf"

BODY = """We study the problem of delegating computation to untrusted servers while keeping both privacy and verifiability. Prior work shows that a structured reference string can be constructed in a universal and updatable fashion, which removes one of the main obstacles in deployment.
Our construction follows the permutation argument of Bayer and Groth, but evaluates the argument over a multiplicative subgroup rather than over coefficients of monomials.
As a result, both the permutation argument and the arithmetization step become substantially simpler, and the prover running time drops accordingly.
We measure the end-to-end cost on circuits of increasing size and report the group exponentiations required by each phase.
The verifier run time remains polylogarithmic in the circuit size, while the proof length stays logarithmic.
We also discuss the preprocessing phase and the generation of the structured reference string."""

BODY2 = """A related convenience is that multiplicative subgroups interact well with Lagrange bases. Suppose H is a multiplicative subgroup of order n and x lies in H. The polynomial that vanishes on the rest of the subgroup and takes value one at x has a very sparse representation.
To see why this matters, consider the cost of checking polynomial identities: with a sparse representation the check reduces to a handful of group operations instead of a full interpolation.
We then combine this with the grand product argument and show that the resulting protocol is complete and knowledge sound under the discrete logarithm assumption in the random oracle model."""

# 公式行用 PyMuPDF 内置的 Symbol 字体写：抽取器会把它识别为"数学字体片段"，
# 于是整段变成 $...$ —— 这样样张能真正走通公式识别与"重建原文"这两条路径。
FORMULA = "l_x(X) = c_x (X^n - 1) / (X - x)"
TABLE = [["Scheme", "Prover (G1 exp)", "Proof size", "Succinct"],
         ["Groth16", "3n + m", "3 G1", "yes"],
         ["Sonic", "18n", "4 G1", "no"],
         ["This work", "2n", "2 G1", "yes"]]


def build(out: Path) -> Path:
    import pymupdf

    doc = pymupdf.open()
    for page_index in range(2):
        page = doc.new_page(width=595, height=842)   # A4
        y = 70.0
        if page_index == 0:
            page.insert_text((72, y), "Delegating Computation with Universal Setup", fontsize=17, fontname="hebo")
            y += 26
            page.insert_text((72, y), "A. Author, B. Author, C. Author", fontsize=11)
            y += 16
            page.insert_text((72, y), "Institute of Cryptography, Example University", fontsize=10)
            y += 26
            page.insert_text((72, y), "Abstract.", fontsize=11, fontname="hebo")
            y += 16
            y = _para(page, y, BODY)
            y += 20
            page.insert_text((72, y), "1  Introduction", fontsize=13, fontname="hebo")
            y += 22
            y = _para(page, y, BODY2)
            y += 18
            page.insert_text((72, y), FORMULA, fontsize=11, fontname="symb")
        else:
            page.insert_text((72, y), "2  Efficiency Analysis", fontsize=13, fontname="hebo")
            y += 22
            y = _para(page, y, BODY)
            y += 24
            y = _table(page, y)
            y += 24
            page.insert_text((72, y), "References", fontsize=13, fontname="hebo")
            y += 20
            for i, ref in enumerate([
                "[1] S. Bayer and J. Groth. Efficient zero-knowledge argument for correctness of a shuffle, 2012.",
                "[2] J. Groth. On the size of pairing-based non-interactive arguments, 2016.",
                "[3] M. Maller, S. Bowe, M. Kohlweiss and S. Meiklejohn. Sonic, 2019.",
            ], start=1):
                page.insert_text((72, y), ref, fontsize=9)
                y += 17
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    doc.close()
    return out


def _para(page, y: float, text: str, width: float = 450.0, size: float = 10.5) -> float:
    """按宽度折行写段落，返回新的 y。"""
    import pymupdf

    words, line = text.split(), ""
    for w in words:
        probe = (line + " " + w).strip()
        if pymupdf.get_text_length(probe, fontname="helv", fontsize=size) > width:
            page.insert_text((72, y), line, fontsize=size)
            y += 15
            line = w
        else:
            line = probe
    if line:
        page.insert_text((72, y), line, fontsize=size)
        y += 15
    return y


def _table(page, y: float) -> float:
    """画一个带框线的小表格（抽取器用线条识别表格）。"""
    import pymupdf

    x0, col_w, row_h = 72.0, 110.0, 17.0
    for r, row in enumerate(TABLE):
        for c, cell in enumerate(row):
            cx = x0 + c * col_w
            page.draw_rect(pymupdf.Rect(cx, y + r * row_h, cx + col_w, y + (r + 1) * row_h),
                           color=(0, 0, 0), width=0.6)
            page.insert_text((cx + 4, y + r * row_h + 12), cell, fontsize=8.5)
    return y + len(TABLE) * row_h


def main() -> int:
    ap = argparse.ArgumentParser(description="生成合成测试样张 PDF")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"已存在，跳过：{out}（加 --force 覆盖）")
        return 0
    p = build(out)
    print(f"已生成合成样张：{p}（{p.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
