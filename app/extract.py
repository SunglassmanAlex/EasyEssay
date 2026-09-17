"""PDF -> 段落级结构化文本。

流程：PyMuPDF 取 block/line/span -> 过滤页眉页脚 -> 双栏重排 -> 合并段落
     -> 上下标与公式还原（mathify）-> 段落分类（标题/正文/公式/图表题注/参考文献）

输出段落对象：
    {"id": "p0001", "page": 1, "kind": "text", "text": "...$x^{2}$...", "math_ratio": 0.1}
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import glyphnames, mathify, tables
# 直接导入这个函数：函数体里有局部变量也叫 tables（表块列表），
# 会遮蔽模块名（踩过）
from .tables import looks_caption, looks_like_figure_label

try:  # PyMuPDF 新版本推荐 import pymupdf；旧版本只有 fitz
    import pymupdf as fitz
except Exception:  # pragma: no cover
    try:
        import fitz  # type: ignore
    except Exception:
        fitz = None

try:
    import pdfplumber
except Exception:  # pragma: no cover
    pdfplumber = None


# ------------------------------------------------------------------ 工具函数

def _rect_span(rect: Any) -> tuple[float, float, float, float]:
    return (rect.x0, rect.y0, rect.x1, rect.y1)


# 当前这次抽取的"连字符词表"。放在模块级：`_join_lines` 的调用点分散在几个
# 函数里（段落合并、表格题注、兜底路径），逐个传参不现实。
_HYPHEN_VOCAB: set[str] = set()
_WORD_VOCAB: set[str] = set()

# 常见学术连词（前缀部分 → 允许保留连字符）。
# ⚠️ 这条只是**兜底**：主体判据是"文档内行中间出现过的连字符词"。
# 白名单里的词只会**增加**连字符，永远不会把 `ac-cess` 这种断词保留下来，
# 所以零风险（宁可有连字符，也不要把算法名/术语拼错）。
_KNOWN_COMPOUNDS = {
    "state-of", "of-the", "trade-off", "tree-top", "multi-index", "multi-tier",
    "non-trivial", "non-colluding", "semi-honest", "end-to-end", "top-k",
    "knowledge-intensive", "resource-efficient", "memory-efficient",
    "cost-effective", "time-consuming", "real-world", "fine-grained",
    "co-design", "sub-linear", "sub-string", "cross-layer", "self-similar",
    "well-defined", "well-known", "high-dimensional", "low-latency",
    "client-side", "server-side", "read-only", "write-only", "two-party",
    "single-server", "multi-party", "public-key", "private-key",
    "hash-based", "graph-based", "tree-based", "index-based", "oram-based",
    "indistinguishability-based", "simulation-based", "game-based",
    "billion-scale", "million-scale", "near-neighbor", "nearest-neighbor",
    "round-trip", "point-lookup", "load-balancing", "batching-based",
}


def collect_hyphen_vocab(doc: Any, p_from: int, p_to: int) -> set[str]:
    """收集**全文里出现在行中间的连字符词**（这些确定是真连词）。

    判据的关键是"位置"：同一个词，出现在**行中间**说明它本来就带连字符；
    出现在**行末**则可能是排版断词。行中间的出现就是"确证"。
    """
    vocab: set[str] = set()
    for pno in range(max(0, p_from - 1), min(doc.page_count, p_to)):
        try:
            txt = doc[pno].get_text()
        except Exception:  # noqa: BLE001
            continue
        for m in re.finditer(r"([A-Za-z]{2,})-([A-Za-z]{2,})", txt or ""):
            # 连字符后面紧接着换行 = 行末断词，不算确证
            if m.end() < len(txt) and txt[m.end()] in "\r\n":
                continue
            vocab.add((m.group(1) + "-" + m.group(2)).lower())
    global _HYPHEN_VOCAB, _WORD_VOCAB
    _HYPHEN_VOCAB = vocab
    _WORD_VOCAB = collect_word_vocab(doc, p_from, p_to)
    return vocab


def collect_word_vocab(doc: Any, p_from: int, p_to: int) -> set[str]:
    """收集文档里的**独立单词**（出现 ≥2 次）。

    用途：行末连字符的第二条判据 —— 连字符两侧都是本文里出现过的独立单词时，
    它多半是**真连词**（`trade-off`、`tree-top`、`knowledge-intensive`），
    而不是排版断词（`ac-cess` 里的 `cess` 不是独立单词）。
    只取"行末连字符"的候选词往往只在全文出现一次、词表里没有证据，
    所以才需要这条兜底判据（实测还剩 7 处就是这么漏的）。
    """
    from collections import Counter
    cnt: Counter = Counter()
    for pno in range(max(0, p_from - 1), min(doc.page_count, p_to)):
        try:
            txt = doc[pno].get_text()
        except Exception:  # noqa: BLE001
            continue
        for w in re.findall(r"[A-Za-z]{2,}", txt or ""):
            cnt[w.lower()] += 1
    return {w for w, c in cnt.items() if c >= 2 and len(w) >= 2}


def _join_lines(line_texts: list[str], hyphen_vocab: set[str] | None = None) -> str:
    """合并同一段落内的多行：处理跨行连字符，行间补空格。

    ⚠️ 行末连字符有**两种**，必须分开处理（规格 §9-2）：
    · 排版断词（`ac-\ncess`）→ 去掉连字符合成 `access`；
    · 真连词（`state-of-\nthe-art`、`multi-\nindex`）→ **保留连字符**。
    区分办法就是用文档自己的词表（`collect_hyphen_vocab`）：
    候选词若在"行中间出现过"的词表里，说明它本来就有连字符。
    """
    parts: list[str] = []
    for t in line_texts:
        t = t.strip()
        if not t:
            continue
        parts.append(t)
    if not parts:
        return ""
    vocab = hyphen_vocab if hyphen_vocab is not None else _HYPHEN_VOCAB
    out = parts[0]
    for nxt in parts[1:]:
        # 上一行是否以「字母-」结尾，且下一行以小写字母开头
        m = re.search(r"([A-Za-z]{2,})-$", out)
        n = re.match(r"^([a-z][A-Za-z]*)", nxt)
        if m and n:
            candidate = (m.group(1) + "-" + n.group(1)).lower()
            # 判据二：**左侧本身就是本文里的独立单词** → 真连词。
            # 为什么看左侧就够：排版断词的左侧是"被切断的词片"（`ac-`、`infor-`、
            # `per-`），它不会是个独立单词；而真连词的左侧总是完整单词
            # （`trade-off`、`tree-top`、`knowledge-intensive`、`non-trivial`）。
            # 实测把这条放进来后，丢连字符从 7 处降到 0~1 处。
            left, right = m.group(1), n.group(1)
            # 判据二（主判据）：**拼起来的形式在本文里确实是个单词** → 排版断词，合并。
            # 反过来，拼起来不成词（`treetop`、`tradeoff`、`ofthe`）→ 保留连字符。
            #
            # ⚠️ 这条是试了三版才定下的，前两版都被实测否掉：
            # · "左侧是单词"→ 参考文献的大写断词被误判（`Ad-vances`）：多出 106 个假连字符；
            # · "两侧都是单词"→ `algo-rithm`、`band-width`、`as-signed` 仍被误判：多出 36 个。
            # **假连字符是把词写错（algo-rithm），比少一个连字符严重得多**，
            # 所以宁可保守：只在有"拼起来成词"的正面证据时才合并。
            # 判据三：常见学术连词白名单（只**补**连字符，不会把词写错）。
            # 为什么需要它：有些真连词在全文里只出现一次、且恰好断在行末，
            # 词表拿不到任何正面证据（实测剩 7 处这一类）。
            in_whitelist = candidate in _KNOWN_COMPOUNDS or (
                (left + "-" + right).lower() in _KNOWN_COMPOUNDS)
            if candidate in vocab or in_whitelist:
                out = out + nxt                 # 真连词：保留连字符
            else:
                out = out[:-1] + nxt            # 排版断词：去掉连字符
        else:
            out = out + " " + nxt
    return re.sub(r"\s{2,}", " ", out).strip()


def _normalize_furniture(text: str) -> str:
    t = re.sub(r"\d+", "#", text.strip().lower())
    t = re.sub(r"\s+", " ", t)
    return t


FURNITURE_RE = re.compile(
    r"^(arxiv:|https?://|www\.|doi:|©|copyright|\d{1,4}$|\d+\s*\|\s*page|"
    r"proceedings of|downloaded from)",
    re.I,
)


# --------------------------------------------------------------- 页面结构

def _inside(inner: tuple, outer: tuple, pad: float = 3.0) -> bool:
    """内框是否基本落在外框里（用于剔除表格区域内的重复文本块）。"""
    return (inner[0] >= outer[0] - pad and inner[1] >= outer[1] - pad
            and inner[2] <= outer[2] + pad and inner[3] <= outer[3] + pad)


def _rows_to_markdown(rows: list | None) -> str:
    """把表格二维单元格转成 Markdown 表格（保留数值、行序、列义）。"""
    cleaned: list[list[str]] = []
    for r in rows or []:
        cells = []
        for c in r or []:
            v = re.sub(r"\s+", " ", str(c or "")).strip().replace("|", "\\|")
            cells.append(v)
        cleaned.append(cells)
    cleaned = [r for r in cleaned if any(c for c in r)]
    if len(cleaned) < 2:
        return ""
    width = max(len(r) for r in cleaned)
    cleaned = [r + [""] * (width - len(r)) for r in cleaned]
    keep = [i for i in range(width) if any(r[i] for r in cleaned)]
    if len(keep) < 2:
        return ""
    cleaned = [[r[i] for i in keep] for r in cleaned]
    head, body = cleaned[0], cleaned[1:]
    lines = ["| " + " | ".join(head) + " |",
             "|" + "|".join([" --- "] * len(head)) + "|"]
    for r in body:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def _renumber(paragraphs: list[dict]) -> list[dict]:
    """把段落 id 重编成连续序号（p0001…）。

    ⚠️ 为什么必须重编：id 是抽取时按顺序发的，而抽取末尾还会
    **合并字形碎片**（`_merge_glyph_fragments` 把被切断的公式并回上一条）
    并丢掉页眉页脚 —— 被并掉/丢掉的 id 就成了空洞。
    空洞会让"缺段"看起来像漏抽，也让交付自检的"编号不得跳号"永远过不了
    （实测缺 15/38/95/107）。
    重编号后 id 连续；重抽流程会按内容锚点把译文搬过来。
    """
    for i, p in enumerate(paragraphs, 1):
        p["id"] = f"p{i:04d}"
    # 收尾统一修「缺参数的 LaTeX 命令」（`$\sqrt$ N` 这种）。
    # ⚠️ 放在这里而不是每个构造点：文本来源有五六个（正文 / 表格单元格 /
    # 伪代码行 / 图内文字 / 兜底路径），逐点加注定会漏（刚踩过：只包了一处，
    # 重抽后仍有 3 处裸 `\sqrt` 漏网）。单点收尾最省心。
    for p in paragraphs:
        _repair_math_in_place(p)
    return paragraphs


def _repair_math_in_place(para: dict) -> None:
    """把段落里所有文本字段的"缺参数命令"补全（就地改）。"""
    def fix(v):
        if not isinstance(v, str):
            return v
        # 顺序：先补缺参数的命令，再把公式里的裸 < > 转义
        return mathify.escape_math_angles(mathify.repair_bare_commands(v))

    if isinstance(para.get("text"), str):
        para["text"] = fix(para["text"])
    for key in ("lines", "content", "labels"):
        arr = para.get(key)
        if isinstance(arr, list):
            para[key] = [fix(x) for x in arr]
    for holder in ("algorithm", "figure", "table"):
        obj = para.get(holder)
        if not isinstance(obj, dict):
            continue
        for key in ("lines", "content", "labels", "caption"):
            if isinstance(obj.get(key), str):
                obj[key] = fix(obj[key])
            elif isinstance(obj.get(key), list):
                obj[key] = [fix(x) for x in obj[key]]
        for row in obj.get("rows") or []:
            for cell in row:
                if isinstance(cell, dict) and isinstance(cell.get("text"), str):
                    cell["text"] = fix(cell["text"])


def _caption_like(block: dict) -> bool:
    """块看起来是不是题注（`Table 1:` / `Figure 2:`）。

    ⚠️ 优先读 `text`：段落合并之后块里**已经没有 `lines` 了**，
    早先只读 `lines` 导致题注永远匹配不上、题注会重复出现一次（踩过）。
    """
    text = block.get("text")
    if not text:
        text = " ".join(sp["text"] for ln in block.get("lines", []) for sp in ln["spans"]).strip()
    return looks_caption(text or "")


def _page_tables(page, size: float = 10.0, captions: list[dict] | None = None) -> list[dict]:
    """取出本页的**结构化块**：表格（网格）与伪代码（按行）。

    用 `app.tables` 自己按几何重建，**不用 PyMuPDF 的 find_tables**：
    论文里的表格几乎都是 booktabs 风格（只有横线），`lines` 策略一个都找不到；
    `text` 策略又会把整页正文吞进去。详见 app/tables.py 的模块说明。

    `captions` 传入本页的题注块（用于区分 `Table N` 与 `Figure N`）——
    这是把"图的坐标刻度""被切碎的正文"从表格候选里摘掉的关键判据。
    """
    try:
        found = tables.extract_tables(page, size, captions)
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for t in found:
        if t.get("kind") == "figure":
            fig = t.get("figure") or {}
            if not fig.get("caption"):
                continue
            out.append({"bbox": tuple(float(v) for v in t["bbox"]), "kind": "figure",
                        "md": fig.get("caption", ""), "figure": fig,
                        "caption": fig.get("caption", ""),
                        "caption_bbox": t.get("caption_bbox"),
                        "note": t.get("note", "")})
            continue
        if t.get("kind") == "algorithm":
            lines = (t.get("algorithm") or {}).get("lines") or []
            if len(lines) < 3:
                continue
            out.append({"bbox": tuple(float(v) for v in t["bbox"]), "kind": "algorithm",
                        "md": "\n".join(lines), "algorithm": {"lines": lines},
                        "note": t.get("note", "")})
            continue
        grid = t.get("table") or {}
        md = tables.grid_to_markdown(grid)
        if not md:
            continue
        out.append({"bbox": tuple(float(v) for v in t["bbox"]), "kind": "table",
                    "md": md, "table": grid, "note": t.get("note", ""),
                    # 表题跟着表走（渲染成 .tcap），题注段落本身会被吸收掉
                    "caption": t.get("caption", ""),
                    "caption_bbox": t.get("caption_bbox")})
    return out


def _snap_split(text: str, idx: int) -> int:
    """把切分点挪到最近的、且不在 $...$ 内部的空格处（避免把公式切成两半）。"""
    if idx <= 0 or idx >= len(text):
        return idx
    best: int | None = None
    in_math = False
    for i, ch in enumerate(text):
        if ch == "$":
            in_math = not in_math
        elif ch == " " and not in_math:
            if best is None or abs(i - idx) < abs(best - idx):
                best = i
    return best if best is not None else idx


def _merge_cross_page(paragraphs: list[dict]) -> list[dict]:
    """把被页边界切断的正文段合并成一段，并记录 page_end 与 page_break_at。

    判据从严：两段都是正文、跨页、前段没有句末标点、后段以小写或左括号开头。
    page_break_at 记录"换页点"在合并后文本中的位置，供渲染时原位插入换页标记。
    """
    out: list[dict] = []
    for p in paragraphs:
        p.setdefault("page_end", p["page"])
        if out:
            prev = out[-1]
            tail = re.sub(r"\$+$", "", prev["text"].rstrip()).rstrip()
            ends_open = bool(tail) and tail[-1] not in ".?!:;。？！：；…"
            starts_lower = bool(re.match(r"^[a-z(\u201c\[]", p["text"]))
            if (prev["kind"] == "text" and p["kind"] == "text" and ends_open
                    and starts_lower and p["page"] == prev["page_end"] + 1):
                head = prev["text"].rstrip()
                merged = head + " " + p["text"].lstrip()
                prev["page_break_at"] = _snap_split(merged, len(head))
                prev["text"] = merged
                prev["page_end"] = p["page"]
                prev["continued"] = True
                prev["math_ratio"] = mathify.math_coverage(merged)
                continue
        out.append(p)
    return out


def _is_glyph_only_fragment(p: dict) -> bool:
    """这一段是不是"整段只剩无法辨认字形"的碎片。

    实测场景：一条独立公式被 PDF 拆成多个块，其中某块的可见字符只剩下 ⟦?⟧
    （大括号写成上下两截时就是这样）。这种段**单独存在毫无意义**，无论给模型
    多大自由度都只能瞎猜 —— 实测输出是 `\\left[\\right]` 这种空括号。
    """
    text = p.get("text") or ""
    if mathify.MISSING_GLYPH not in text:
        return False
    rest = text.replace(mathify.MISSING_GLYPH, "")
    # 去掉空白、标点与纯运算符，看还剩不剩"真正的内容"
    rest = re.sub(r"[\s.,;:!?()\[\]{}<>|/\\$~^_+\-=*]+", "", rest)
    return not rest and len(text.strip()) <= 12


def _recount_glyph_issues(paragraphs: list[dict]) -> None:
    """按当前文本重算每段的字形问题数。

    必须做：段落的 `glyph_issues` 是建段时记下的，碎片合并后文本变了，
    旧计数就对不上了（表现为文档级总数少算，用户看到的"待修"数字也会偏小）。
    """
    for p in paragraphs:
        n = mathify.count_missing_glyphs(p.get("text") or "")
        if n:
            p["glyph_issues"] = n
        else:
            p.pop("glyph_issues", None)


def _bbox_overlaps(a, b) -> bool:
    """两个 bbox 是否有明显重叠（用于判断碎片是否属于同一条公式）。"""
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    if ix <= 0 or iy <= 0:
        return False
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return smaller > 0 and (ix * iy) / smaller >= 0.5


def _merge_glyph_fragments(paragraphs: list[dict]) -> list[dict]:
    """把"整段只有 ⟦?⟧"的碎片并回上一段。

    为什么必须合并而不是丢给模型猜：碎片和上一段本来就是同一条公式的两半
    （大括号被拆成上/下两截，各写一半）。并起来模型看到的才是完整公式。
    """
    out: list[dict] = []
    for p in paragraphs:
        if out and _is_glyph_only_fragment(p):
            prev = out[-1]
            # 只在"上一段是公式"或"两者版面重叠"时才合并 —— 说明它们本属一体
            if prev.get("kind") == "equation" or _bbox_overlaps(prev.get("bbox"), p.get("bbox")):
                prev["text"] = prev["text"].rstrip() + " " + (p["text"] or "").strip()
                prev["page_end"] = max(prev.get("page_end", prev["page"]), p["page"])
                prev["math_ratio"] = mathify.math_coverage(prev["text"])
                prev.setdefault("merged_fragments", []).append(p["id"])
                continue
        out.append(p)
    return out


def _page_blocks(page) -> list[dict]:
    """返回页面上所有文本块：{bbox, lines:[{bbox, spans}]}"""
    raw = page.get_text("dict")
    blocks = []
    for blk in raw.get("blocks", []):
        if blk.get("type") != 0:
            continue
        lines = []
        for ln in blk.get("lines", []):
            spans = []
            for sp in ln.get("spans", []):
                if not sp.get("text"):
                    continue
                spans.append({
                    "text": sp["text"],
                    "font": sp.get("font", ""),
                    "size": float(sp.get("size", 10.0)),
                    "origin": (float(sp["origin"][0]), float(sp["origin"][1])),
                    "bbox": tuple(float(v) for v in sp["bbox"]),
                })
            if spans:
                lines.append({"bbox": tuple(float(v) for v in ln["bbox"]), "spans": spans})
        if lines:
            blocks.append({"bbox": tuple(float(v) for v in blk["bbox"]), "lines": lines})
    return blocks


def _block_dominant_size(block: dict) -> float:
    sizes: list[float] = []
    for ln in block["lines"]:
        for sp in ln["spans"]:
            sizes.append(round(sp["size"], 1))
    if not sizes:
        return 10.0
    counts: dict[float, int] = {}
    for s in sizes:
        counts[s] = counts.get(s, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _block_markdown(block: dict, base_size: float,
                    encodings: dict | None = None) -> tuple[str, float]:
    """把 block 内所有行合并成一段 markdown，返回 (text, 公式覆盖度)。

    注意这里的第二个值是「落在 $...$ 内的字符占比」（math_coverage），
    不是"数学字符的比例"。判断"这段该不该按独立公式排版"必须用前者：
    像 `l_x(X) = c_x (X^n-1)/(X-x)` 这种 LaTeX 源码里几乎全是 ASCII 字母，
    按字符比例算会得出 0.04 这种荒谬结果，导致独立公式识别不出来。
    """
    parts = []
    for ln in block["lines"]:
        parts.append(mathify.spans_to_markdown(ln["spans"], base_size, encodings))
    text = _join_lines(parts)
    return text, mathify.math_coverage(text)


def _is_bold(block: dict) -> bool:
    names = " ".join(sp["font"] for ln in block["lines"] for sp in ln["spans"])
    return bool(re.search(r"(bold|black|heavy|semibold|bx|CMB)", names, re.I))


def _detect_two_column(blocks: list[dict], page_width: float) -> bool:
    if len(blocks) < 4:
        return False
    mid = page_width / 2
    tol = page_width * 0.04
    left = [b for b in blocks if b["bbox"][2] <= mid + tol]
    right = [b for b in blocks if b["bbox"][0] >= mid - tol]
    spanning = [b for b in blocks
                if b["bbox"][0] < mid - tol and b["bbox"][2] > mid + tol]
    if len(left) < 2 or len(right) < 2:
        return False
    if len(spanning) > max(2, len(blocks) // 3):
        return False
    # 左右栏必须有明显的纵向重叠（真两栏），否则可能是单栏但缩进奇怪
    l_top = min(b["bbox"][1] for b in left)
    r_top = min(b["bbox"][1] for b in right)
    l_bot = max(b["bbox"][3] for b in left)
    r_bot = max(b["bbox"][3] for b in right)
    overlap = min(l_bot, r_bot) - max(l_top, r_top)
    return overlap > (l_bot - l_top) * 0.4 and overlap > (r_bot - r_top) * 0.4


def _order_blocks(blocks: list[dict], page_width: float) -> list[dict]:
    """按阅读顺序排列块：跨栏块（标题/摘要）-> 左栏 -> 右栏 -> 底部跨栏块。"""
    if not _detect_two_column(blocks, page_width):
        return sorted(blocks, key=lambda b: (round(b["bbox"][1], 1), b["bbox"][0]))
    mid = page_width / 2
    tol = page_width * 0.04
    spanning = [b for b in blocks if b["bbox"][0] < mid - tol and b["bbox"][2] > mid + tol]
    left = [b for b in blocks if b["bbox"][2] <= mid + tol]
    right = [b for b in blocks if b["bbox"][0] >= mid - tol]
    spanning.sort(key=lambda b: b["bbox"][1])
    left.sort(key=lambda b: b["bbox"][1])
    right.sort(key=lambda b: b["bbox"][1])
    if not left:
        return spanning + right
    first_col_top = min(b["bbox"][1] for b in left + right)
    top_span = [b for b in spanning if b["bbox"][1] <= first_col_top + 2]
    bottom_span = [b for b in spanning if b["bbox"][1] > first_col_top + 2]
    return top_span + left + right + bottom_span


# ------------------------------------------------------------- 段落分类

SECTION_RE = re.compile(r"^(\d+(\.\d+)*\.?|[A-Z]\.)\s+\S")
CAPTION_RE = re.compile(r"^(figure|fig\.|table|algorithm|listing|scheme|eq\.)\s*\d+", re.I)
REF_HEAD_RE = re.compile(r"^(references|bibliography|参考文献)\s*$", re.I)
ABSTRACT_RE = re.compile(r"^(abstract|摘要)\b", re.I)


def _classify(text: str, size: float, body_size: float, bold: bool,
              math_ratio: float, page: int, state: dict) -> tuple[str, int]:
    """返回 (kind, level)。"""
    if state.get("in_refs") and not REF_HEAD_RE.match(text.strip()):
        return "reference", 0
    if REF_HEAD_RE.match(text.strip()):
        state["in_refs"] = True
        return "heading", 1
    if mathify.is_display_equation(text, math_ratio):
        return "equation", 0
    if ABSTRACT_RE.match(text.strip()):
        return "abstract", 0
    stripped = text.strip()
    # 去掉行尾的上/下标记号（脚注引用等），避免影响"是否以句号结尾"的判断
    tail = re.sub(r"\$[\^_]\{[^}]*\}\$", "", stripped).rstrip()
    if CAPTION_RE.match(stripped) and size <= body_size * 1.02:
        return "caption", 0
    short = len(stripped) < 90
    is_candidate = short and (size >= body_size * 1.14 or (bold and size >= body_size * 0.98))
    if is_candidate:
        if size >= body_size * 1.6 and page == 1:
            return "title", 0
        # 章节标题：编号 + 大写开头 + 不以句号结尾（避免把编号列表当成标题）
        if SECTION_RE.match(stripped) and not tail.endswith((".", "。", ":", ",")):
            parts = stripped.split()[0]
            return "heading", 1 if "." not in parts else 2
        if size >= body_size * 1.15:
            return "heading", 1
        if bold and short and not tail.endswith((".", "。", ":")):
            return "heading", 2
    if short and SECTION_RE.match(stripped) and not tail.endswith((".", "。", ":")):
        parts = stripped.split()[0]
        return "heading", 1 if "." not in parts else 2
    return "text", 0


# ----------------------------------------------------------------- 主流程

def _body_size(doc) -> float:
    sizes: dict[float, int] = {}
    for page in doc[: min(6, doc.page_count)]:
        for blk in _page_blocks(page):
            for ln in blk["lines"]:
                for sp in ln["spans"]:
                    s = round(sp["size"], 1)
                    sizes[s] = sizes.get(s, 0) + len(sp["text"])
    if not sizes:
        return 10.0
    return max(sizes.items(), key=lambda kv: kv[1])[0]


def extract_pdf(path: str | Path, page_from: int | None = None,
                page_to: int | None = None, use_ocr: bool = True,
                use_tables: bool = True) -> dict:
    """抽取 PDF，返回 {title, paragraphs, page_count, ocr_pages}。

    use_tables=True 时用 PyMuPDF 的表格识别把表格抽成 Markdown 表格（前后端各渲染一份）。
    """
    if fitz is None:
        raise RuntimeError("未安装 PyMuPDF，请先 pip install PyMuPDF")

    doc = fitz.open(str(path))
    body = _body_size(doc)
    total = doc.page_count
    p_from = max(1, page_from or 1)
    p_to = min(total, page_to or total)
    # 把 CMEX 系列字体的"码位 -> 字形名"表读出来：这些字体里的码位常常被坏掉的
    # ToUnicode 抹成控制字符/私有区，但字体自己的 /Encoding 里有可读的 TeX 字形名
    # （parenleftBig / summationtext / circleplusdisplay…），据此可以**确定性地**
    # 还原大括号、求和号之类，不必让模型猜（见 app/glyphnames.py）。
    encodings = glyphnames.collect_encodings(doc)

    # 先收集"行中间出现过的连字符词"（判定行末连字符是断词还是真连词的依据）
    hyphen_vocab = collect_hyphen_vocab(doc, p_from, p_to)   # 同时写入模块级缓存

    # 第一遍：收集各页块，识别重复页眉页脚
    pages: list[dict] = []
    furniture: dict[str, int] = {}
    for pno in range(p_from - 1, p_to):
        page = doc[pno]
        rect = page.rect
        blocks = _page_blocks(page)
        h, top_limit, bot_limit = rect.height, rect.y0 + rect.height * 0.062, rect.y1 - rect.height * 0.055
        for b in blocks:
            y0, y1 = b["bbox"][1], b["bbox"][3]
            if y1 <= top_limit or y0 >= bot_limit:
                txt = " ".join(sp["text"] for ln in b["lines"] for sp in ln["spans"]).strip()
                if txt:
                    furniture[_normalize_furniture(txt)] = furniture.get(_normalize_furniture(txt), 0) + 1
        pages.append({"page": page, "rect": rect, "blocks": blocks})

    npages = max(1, len(pages))
    repeated = {k for k, v in furniture.items() if v >= max(2, int(npages * 0.3))}

    paragraphs: list[dict] = []
    state: dict = {"in_refs": False}
    ocr_pages: list[int] = []
    idx = 0

    for item in pages:
        page, rect, blocks = item["page"], item["rect"], item["blocks"]
        pno = page.number + 1
        h = rect.height
        top_limit, bot_limit = rect.y0 + h * 0.062, rect.y1 - h * 0.055

        kept = []
        # 题注要先收集：表格/图的区分就靠它（Table N vs Figure N）
        caps = []
        for b in blocks:
            cap_txt = " ".join(sp["text"] for ln in b["lines"] for sp in ln["spans"]).strip()
            if re.match(r"^\s*(Table|Figure|Fig\.?|表|图)\s*\d", cap_txt, re.I):
                caps.append({"bbox": b["bbox"], "text": cap_txt})
        tables = _page_tables(page, body, caps) if use_tables else []
        for b in blocks:
            y0, y1 = b["bbox"][1], b["bbox"][3]
            txt = " ".join(sp["text"] for ln in b["lines"] for sp in ln["spans"]).strip()
            in_zone = y1 <= top_limit or y0 >= bot_limit
            if in_zone:
                norm = _normalize_furniture(txt)
                if norm in repeated or FURNITURE_RE.match(txt):
                    continue
                if re.fullmatch(r"\d{1,4}", txt):
                    continue
            # 页码：孤立的纯数字/罗马数字小块（宽度很小），无论在哪都丢掉
            if re.fullmatch(r"[\dIVXivx]{1,4}", txt) and len(b["lines"]) == 1 \
                    and (b["bbox"][2] - b["bbox"][0]) < rect.width * 0.12:
                continue
            # 落在表格区域内的文本块会被表格本身覆盖，丢掉避免抽两遍
            if any(_inside(b["bbox"], tb["bbox"]) for tb in tables):
                continue
            kept.append(b)

        page_text_len = sum(len(sp["text"]) for b in kept for ln in b["lines"] for sp in ln["spans"])

        # 扫描页：几乎没有文本 -> OCR
        if page_text_len < 40 and use_ocr:
            try:
                from . import ocr

                text = ocr.ocr_page(page, dpi=200)
                if text.strip():
                    ocr_pages.append(pno)
                    for chunk in _split_ocr_text(text):
                        idx += 1
                        paragraphs.append({
                            "id": f"p{idx:04d}", "page": pno, "kind": "text",
        "text": mathify.repair_bare_commands(mathify.plain_text_escape(chunk)),
                        })
                    continue
            except Exception:
                pass

        if not kept and not tables:
            continue

        combined = list(kept) + [
            {"bbox": tb["bbox"], "is_table": True, "text": tb["md"],
             "kind": tb["kind"], "table": tb.get("table"),
             "algorithm": tb.get("algorithm"), "figure": tb.get("figure"),
             "caption": tb.get("caption", ""), "lines": []}
            for tb in tables
        ]
        ordered = _order_blocks(combined, rect.width)
        merged: list[dict] = []
        for b in ordered:
            if b.get("is_table"):
                merged.append({"bbox": b["bbox"], "text": b["text"], "is_table": True,
                               "kind": b.get("kind", "table"),
                               "table": b.get("table"), "algorithm": b.get("algorithm"),
                               "figure": b.get("figure"), "caption": b.get("caption", ""),
                               "math_ratio": mathify.math_coverage(b["text"]),
                               "size": body, "bold": False})
                continue
            text, math_ratio = _block_markdown(b, body, encodings)
            if not text:
                continue
            rec = {
                "bbox": b["bbox"], "text": text, "math_ratio": math_ratio,
                "size": _block_dominant_size(b), "bold": _is_bold(b),
            }
            # 段落合并：上一块未以句末标点结束且本块以小写/介词开头，且纵向间距小
            if merged:
                prev = merged[-1]
                gap = rec["bbox"][1] - prev["bbox"][3]
                prev_tail = prev["text"].rstrip()
                starts_lower = bool(re.match(r"^[a-z(]", rec["text"]))
                no_end = not re.search(r"[.:;!?]\s*$", prev_tail)
                same_size = abs(rec["size"] - prev["size"]) < 0.6
                not_special = prev["kind_guess"] == "text" if "kind_guess" in prev else True
                if (same_size and not_special and gap < 1.6 * rec["size"]
                        and (starts_lower or (no_end and gap < 0.9 * rec["size"]))):
                    merged[-1]["text"] = _join_lines([prev["text"], rec["text"]])
                    merged[-1]["bbox"] = (prev["bbox"][0], prev["bbox"][1],
                                          max(prev["bbox"][2], rec["bbox"][2]), rec["bbox"][3])
                    merged[-1]["math_ratio"] = mathify.math_coverage(merged[-1]["text"])
                    continue
            mrec = dict(rec)
            mrec["kind_guess"] = "equation" if mathify.is_display_equation(text, math_ratio) else "text"
            merged.append(mrec)

        for tb in tables:
            if tb.get("kind") == "figure":
                tb["md"] = (tb.get("figure") or {}).get("caption", "")

        # 被表格/图带走的题注：按 bbox 记住，生成段落时跳过（题注文本会渲染在
        # 表格的 .tcap 或图的 .figcap 里，由翻译整块处理，不再单独成段）
        absorbed_captions: set = set()
        absorbed_labels: set = set()
        for tb in tables:
            cap = tb.get("caption")
            if not cap:
                continue
            best = None
            for b in merged:
                if b.get("is_table"):
                    continue
                if _caption_like(b) and (best is None or abs(b["bbox"][1] - tb["bbox"][1])
                                         < abs(best["bbox"][1] - tb["bbox"][1])):
                    best = b
            if best is not None:
                absorbed_captions.add(best["bbox"])
        # —— 图例吸收 ——
        # ⚠️ 图的**图例/子图标题常常画在图框之外**（甚至横向错开：实测图框 x≈84–160、
        # 图例 x≈162–472），按"图框范围"取文字根本取不到，于是图例会被当成
        # 正文段落抽出来、模型还会认真翻译它（规格 §5 明确说图内文字不该当正文翻）。
        # 这里按"图的纵向区带 + 标签特征"吸收：区带内的短块、无句号句子结构的，
        # 判为图的一部分（标题因为有编号/字号特征，会被 _classify 判成 heading，不受影响）。
        for tb in tables:
            if tb.get("kind") != "figure":
                continue
            fig = tb.get("figure") or {}
            # ⚠️ 锚点用 bbox 的**底**（它含图题），不是顶：
            # 图的 bbox 顶是"第一个子图的框"，而图例往往在那之上
            # （实测图框顶 y=82、图例 y=72 —— 锚错了就差 10pt 全漏）
            base = tb["bbox"][3]
            band_lo, band_hi = base - 430.0, base + 2.0
            for b in merged:
                if b.get("is_table") or b.get("bbox") in absorbed_captions:
                    continue
                by = (b["bbox"][1] + b["bbox"][3]) / 2
                if not (band_lo <= by <= band_hi):
                    continue
                # ⚠️ 读 `text` 而不是 `lines`：合并后的块里**没有 `lines` 了**
                # （踩过两次：题注吸收一次、这里一次）
                txt = (b.get("text") or "").strip()
                if not txt or not looks_like_figure_label(txt):
                    continue
                # **先判"数字占主导"**：图表的刻度碎片常被判成标题，而它们
                # 又恰好以数字开头（`0.55 0.7 0.19 efn:[1,128] …`）——
                # 若先走"编号开头的标题不吸收"那条守卫，它们会被整批放过（踩过）。
                # 真标题（`3.1 Threat Model & Security Guarantees`）数字占比很低。
                toks = txt.split()
                numeric = sum(1 for t in toks
                              if re.fullmatch(r"[\d.,:%\-\[\]()]+", t or ""))
                numeric_dominant = bool(toks) and numeric / len(toks) >= 0.5
                if not numeric_dominant:
                    # 标题特征（编号开头 / 短且加粗）不吸收 —— 那是章节标题
                    if re.match(r"^\s*\d+(\.\d+)*\s+\S", txt) or _caption_like(b):
                        continue
                    # 题注（含上一张图的）是边界，不吸收
                    if looks_caption(txt):
                        continue
                    kind_guess, _lvl = _classify(txt, float(b.get("size") or body),
                                                 body, bool(b.get("bold")),
                                                 float(b.get("math_ratio") or 0),
                                                 pno, {"in_refs": False})
                    if kind_guess in ("heading", "title", "caption",
                                      "reference", "abstract"):
                        continue
                # ⚠️ 放进 **labels** 而不是 content：坐标轴刻度/图例不是"可读内容"，
                # 塞进 content 会让"纯图形的图"看起来"有文字"，模型就不写译注了
                # （测试当场抓到：合成页的纯图形图被判成"有内容"）。
                # labels 既不进正文流、也不参与"要不要写译注"的判断，只在图上弱化展示。
                fig.setdefault("labels", [])
                if txt not in fig["labels"]:
                    fig["labels"].append(txt)
                absorbed_labels.add(b["bbox"])

        for b in merged:
            idx += 1
            # 题注已被表格/图吸收（渲染成 .tcap / .figcap）→ 不要再单独成段，
            # 否则同一句题注会出现两次；图例同理（并进了 figure.content）
            if b.get("bbox") in absorbed_captions or b.get("bbox") in absorbed_labels:
                continue
            if b.get("is_table"):
                # 伪代码与表格都算"结构化块"，但**处理方式完全不同**：
                # 伪代码按行保留（行号 + 缩进表达层级），绝不能网格化；
                # 表格则要网格化才能表达跨列与列对齐。
                if b.get("kind") == "figure" and b.get("figure"):
                    fig = b["figure"]
                    paragraphs.append({
                        "id": f"p{idx:04d}", "page": pno, "page_end": pno,
                        "kind": "figure", "level": 0,
                        "text": fig.get("caption", ""),
                        "math_ratio": round(b["math_ratio"], 3),
                        "bbox": [round(v, 1) for v in b["bbox"]],
                        "figure": fig,
                    })
                    continue
                is_alg = b.get("kind") == "algorithm" and b.get("algorithm")
                paragraphs.append({
                    "id": f"p{idx:04d}", "page": pno, "page_end": pno,
                    "kind": "algorithm" if is_alg else "table", "level": 0,
                    "text": b["text"],
                    "math_ratio": round(b["math_ratio"], 3),
                    "bbox": [round(v, 1) for v in b["bbox"]],
                    # 结构化网格：{columns, head_rows, rows:[[{text,span}|None]]}。
                    # 表格必须**整块**翻译（拆开翻会丢掉列对齐与表头对应），
                    # 渲染也靠它出真正的 <table>（markdown 表达不了跨列的表头）。
                    **({"table": b["table"]} if (b.get("table") and not is_alg) else {}),
                    **({"algorithm": b["algorithm"]} if is_alg else {}),
                    **({"caption": b["caption"]} if b.get("caption") else {}),
                })
                continue
            kind, level = _classify(b["text"], b["size"], body, b["bold"],
                                    b["math_ratio"], pno, state)
            n_missing = mathify.count_missing_glyphs(b["text"])
            paragraphs.append({
                "id": f"p{idx:04d}", "page": pno, "page_end": pno,
                "kind": kind, "level": level,
                "text": b["text"], "math_ratio": round(b["math_ratio"], 3),
                "bbox": [round(v, 1) for v in b["bbox"]],
                # 这段里有几处"PDF 字体表坏了、认不出"的字形（渲染为 ⟦?⟧，等模型还原）
                **({"glyph_issues": n_missing} if n_missing else {}),
            })

    paragraphs = _merge_cross_page(paragraphs)
    # 再把"整段只剩 ⟦?⟧"的公式碎片并回上一条（必须放在跨页合并之后：
    # 先处理页边界，碎片才不会被误当成跨页段的另一半）
    paragraphs = _merge_glyph_fragments(paragraphs)
    _recount_glyph_issues(paragraphs)

    title = ""
    for p in paragraphs:
        if p["kind"] == "title":
            title = p["text"]
            break
    if not title:
        for p in paragraphs:
            if p["kind"] in ("text", "heading") and len(p["text"]) > 12:
                title = p["text"][:120]
                break
    title = re.sub(r"\$[^$]*\$", "", title).strip() or Path(path).stem

    doc.close()
    # 合并字形碎片会吃掉一些 id → 这里统一重编号，保证不跳号
    paragraphs = _renumber(paragraphs)
    glyph_issues = sum(p.get("glyph_issues", 0) for p in paragraphs)
    return {
        "title": title,
        "paragraphs": paragraphs,
        "page_count": total,
        "body_size": round(body, 2),
        "ocr_pages": ocr_pages,
        "pages_extracted": p_to - p_from + 1,
        # 汇总"需要模型还原的字形数"，用于给用户提示与"只修这些段"的入口
        "glyph_issues": glyph_issues,
        "glyph_issue_ids": [p["id"] for p in paragraphs if p.get("glyph_issues")],
    }


def _split_ocr_text(text: str, max_len: int = 900) -> list[str]:
    """把 OCR 文本按行聚合为段落。"""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    out: list[str] = []
    for b in blocks:
        if len(b) <= max_len:
            out.append(re.sub(r"(?<![.!?:;])\n", " ", b))
        else:
            for i in range(0, len(b), max_len):
                out.append(b[i:i + max_len])
    return out


def extract_with_pdfplumber(path: str | Path) -> dict:
    """pdfplumber 兜底：PyMuPDF 抽取结果异常（例如整篇 0 段）时使用。"""
    if pdfplumber is None:
        raise RuntimeError("未安装 pdfplumber")
    paragraphs: list[dict] = []
    idx = 0
    with pdfplumber.open(str(path)) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            words = page.extract_words(use_text_flow=True) or []
            lines: dict[float, list[str]] = {}
            for w in words:
                key = round(w["top"] / 3.0)
                lines.setdefault(key, []).append(w["text"])
            buf: list[str] = []
            for key in sorted(lines):
                buf.append(" ".join(lines[key]))
            text = _join_lines(buf, hyphen_vocab)
            for chunk in [c.strip() for c in re.split(r"\s{2,}", text) if c.strip()]:
                idx += 1
                paragraphs.append({
                    "id": f"p{idx:04d}", "page": pno, "kind": "text",
                    "text": mathify.repair_bare_commands(mathify.plain_text_escape(chunk)),
                    "math_ratio": 0.0,
                })
    paragraphs = _renumber(paragraphs)
    return {"title": Path(path).stem, "paragraphs": paragraphs,
            "page_count": len(set(p["page"] for p in paragraphs)) or 1,
            "body_size": 10.0, "ocr_pages": [], "fallback": True}
