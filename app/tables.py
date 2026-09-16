"""表格抽取：把 booktabs 风格（只有横线）的表格重建成二维网格。

## 为什么需要单独一个模块

LaTeX 论文里的表格绝大多数是 **booktabs 风格：只有顶/中/底三条横线，一根竖线都没有**。
PyMuPDF 的 `find_tables(strategy="lines")` 要求"闭合单元格"，对这种表**完全找不到**
（实测用户论文第 13–14 页三个表，`lines` 策略返回 0 个表）；`strategy="text"` 又太贪 ——
它按整页空白对齐分组，会把整页正文吞成一个 48 行 × 9 列的"表"。

结果是表格单元格被按阅读顺序拍平成一整段（`Model Name Time Comm. Rounds ...`），
屏幕上就是一堆读不下去的数字。所以这里自己按几何重建。

## 五个关键判据（都是实测出来的，改之前先看数据）

1. **表格区域靠横线定位**：booktabs 的 toprule/midrule/bottomrule **等宽**，
   把 x 范围相同的横线聚成一组即可；表头跨列用的 `\\cmidrule` 只有部分宽度，会自然落单。
2. **列槽必须只取自表体行**：表头有跨列单元格，拿它定槽会把 5 列算成 8 列（踩过）。
3. **词→列靠"重叠最大"，不能靠"词间空隙"**。实测 Table 1 里：
   `'Model'@204..233.6` 与 `'Name'@237..264` 间隙 3.5pt（同一格），
   而 `'Name'@264.3` 与 `'(Lower'@267.1` 间隙只有 2.8pt（**不同格**）——
   跨列间隙比同格词距还小，空隙阈值法必然出错。
4. **跨列靠"组头吸收"**：`'Perplexity'@277..325` 横跨列 1–2（span=2），
   它下一行的 `'(Lower is'`、`'better)'` 分别落在列 1、列 2 —— 位置上看是两个格，
   语义上属于同一个组头。所以要用 span≥2 的组头去"吸收"落在它范围内的子格。
5. **行距并逻辑行**：一行文字可能占多个物理行（窄列里标题换行），
   行距 ≤ 0.8×字号 视为同一逻辑行（实测同一行 0.55×，相邻行 1.1×）。

## 表格 vs 伪代码 vs 图：怎么区分（2026-09-16 补）

初版只看"两条等宽横线夹内容"，结果把三类东西都当成了表格（用户实测反馈）：

| 误判 | 真相 | 判据 |
|---|---|---|
| `Algorithm 1: SEARCH(...)` | **伪代码**（`algorithm` 环境上下各一条 `\\hrule`） | 见 `pseudocode_score` |
| 一段安全证明正文 | **正文**（被列槽切成 12 列） | 挂的是 `Figure` 题注 → 丢弃 |
| `efn:[1,128]` `0.04 0.02` | **图的坐标轴刻度与图例** | 无 Table 题注 / `Figure` 题注 |

**最可靠的判据是题注**：论文里真表格一定配 `Table N:`，图配 `Figure N:`。
实测一次就把 7 个误判全部摘掉、7 个真表格全部保留（见 `_caption_kind`）。
不确定的一律**丢弃**（不是硬塞进表格）—— 丢弃后该区域的正文不再被抑制，
会作为普通段落抽出来，这正是我们想要的。

伪代码有**自己的处理**（`algorithm_lines`）：按物理行保留行号与缩进，
绝不网格化 —— 伪代码靠"一行一条语句 + 缩进层级"表达结构，拆成单元格就没法读。

## 已知边界（如实说明）

* `\\multirow`（跨行单元格）不支持：单元格竖直居中，会并进相邻行，
  实测表现为 `SIGMA 32`（正确是 `32 | SIGMA`）。仍是一张可读的表，只是该格归属有偏差。
* 完全没有横线的"纯空白对齐"表格识别不到（没有可靠的行边界）。
"""
from __future__ import annotations

import math
import re
from typing import Any

_ROW_GAP_RATIO = 0.8        # 行距小于 0.8×字号 → 同一逻辑行的续行
_MIN_RULE_WIDTH = 30.0      # 太短的线段不算横线
_MIN_RULE_COUNT = 2         # 至少两条横线才可能是表格
_MIN_TABLE_HEIGHT = 15.0
_MIN_CORRIDOR = 3.0      # 走廊宽度小于这个值不算列边界（词间距本身就有 2–3pt）
_MAX_FIG_GAP = 55.0      # 图内元素之间的最大空白；超过就认为越过了图边界
_MAX_FIG_GAP = 55.0      # 图内元素之间的最大空白；超过就认为越过了图边界

# 伪代码关键字（用于把 algorithm 环境从表格候选里摘出来）
_PSEUDO_KEYWORDS = ("for", "foreach", "while", "if", "then", "do", "return",
                    "elif", "else", "←", "<-")

# 题注首词：`Table 1:` / `Figure 2.`（也认中文"表/图"）。
# ⚠️ 编号后面**必须**跟 `:` 或 `.` —— 否则正文里的
#    "Fig. 2 shows the architecture ..." 会被当成图题（实测 Compass 第 5、7 页各中一次）。
_TABLE_CAPTION_RE = re.compile(r"^\s*(Table|TABLE|表)\s*\d+\s*[:：.]", re.I)
_FIGURE_CAPTION_RE = re.compile(r"^\s*(Figure|Fig\.?|FIGURE|图)\s*\d+\s*[:：.]", re.I)


# --------------------------------------------------------------- 几何基础

def _rules(page: Any) -> list[tuple[float, float, float]]:
    """取出页面上的横线：(y, x0, x1)。"""
    out: list[tuple[float, float, float]] = []
    try:
        drawings = page.get_drawings()
    except Exception:  # noqa: BLE001
        return out
    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        if r.height < 2.0 and r.width >= _MIN_RULE_WIDTH:
            out.append((round(float(r.y0), 1), round(float(r.x0), 1), round(float(r.x1), 1)))
    return sorted(out)


def _has_text_between(page: Any, y_a: float, y_b: float, x0: float, x1: float) -> bool:
    """两条横线之间有没有**正文**（用来判断它们是否属于同一个块）。

    ⚠️ 题注行不算正文：表格的 `Table N:` 题注常常正好落在两块之间，
    如果把题注当成"内容"，两张相邻的表就永远切不开、会被并成一块
    （踩过：自造样张里 Table 1 的题注挡在伪代码块与 Table 2 之间）。
    """
    if y_b - y_a < 8.0:
        return True          # 挨得很近，算同一块
    try:
        words = page.get_text("words")
    except Exception:  # noqa: BLE001
        return True
    inside = [w for w in words
              if x0 - 4 <= w[0] <= x1 + 4 and y_a + 1.0 < w[1] < y_b - 1.0]
    if not inside:
        return False
    # 按 y 聚成行，整行都是题注的忽略掉
    lines: list[list] = []
    for w in sorted(inside, key=lambda w: (round(w[1], 1), w[0])):
        if lines and abs(w[1] - lines[-1][0][1]) <= 3.0:
            lines[-1].append(w)
        else:
            lines.append([w])
    for ln in lines:
        text = " ".join(w[4] for w in ln).strip()
        if not looks_caption(text):
            return True      # 有非题注的正文 → 是同一块的内容
    return False             # 夹着的全是题注 → 两块，切开


def _split_regions(page: Any, x0: float, x1: float, ys: list[float]) -> list[dict]:
    """同一 x 范围的横线：**两条横线之间没有文字就切开** —— 它们是两个块。

    为什么必须切：同一页里"伪代码块"和"表格"经常左右边界完全相同
    （都在版心宽度内、都从同一个左边距起），只按 x 范围分组会把它们并成一个
    跨越大半页的"区域"，然后伪代码判据一命中，**后面的表格就被整个吞掉**
    （踩过：合成样张里伪代码 + 表格相邻，表格直接消失）。
    """
    out: list[dict] = []
    cur = [ys[0]]
    for y in ys[1:]:
        if _has_text_between(page, cur[-1], y, x0, x1):
            cur.append(y)
        else:
            if len(cur) >= _MIN_RULE_COUNT and cur[-1] - cur[0] >= _MIN_TABLE_HEIGHT:
                out.append({"x0": x0, "x1": x1, "ys": cur})
            cur = [y]
    if len(cur) >= _MIN_RULE_COUNT and cur[-1] - cur[0] >= _MIN_TABLE_HEIGHT:
        out.append({"x0": x0, "x1": x1, "ys": cur})
    return out


def table_regions(page: Any) -> list[dict]:
    """按 x 范围把横线分组 → 表格区域；组内再按"中间有没有文字"细分。

    只看 x 范围完全一致的横线 —— booktabs 的 top/mid/bottom rule 等宽，
    而 `\\cmidrule`（表头跨列用的部分横线）宽度不同，会自然落单。
    """
    groups: dict[tuple[float, float], list[float]] = {}
    for y, x0, x1 in _rules(page):
        groups.setdefault((x0, x1), []).append(y)
    out: list[dict] = []
    for (x0, x1), ys in groups.items():
        out.extend(_split_regions(page, x0, x1, sorted(ys)))
    out.sort(key=lambda g: g["ys"][0])
    return out


def _words_in(page: Any, reg: dict) -> list[dict]:
    x0, x1 = reg["x0"] - 6, reg["x1"] + 6
    top, bot = reg["ys"][0] - 3, reg["ys"][-1] + 9
    out: list[dict] = []
    try:
        words = page.get_text("words")
    except Exception:  # noqa: BLE001
        return out
    for w in words:
        if x0 <= w[0] <= x1 and top <= w[1] <= bot:
            out.append({"x0": float(w[0]), "x1": float(w[2]), "y": float(w[1]),
                        "t": str(w[4])})
    return out


def _physical_lines(words: list[dict], size: float) -> list[dict]:
    ws = sorted(words, key=lambda w: (round(w["y"], 1), w["x0"]))
    lines: list[dict] = []
    cur: list[dict] = []
    cy: float | None = None
    tol = max(2.0, size * 0.35)
    for w in ws:
        if cy is None or abs(w["y"] - cy) > tol:
            if cur:
                lines.append({"y": cur[0]["y"], "words": cur})
            cur, cy = [w], w["y"]
        else:
            cur.append(w)
    if cur:
        lines.append({"y": cur[0]["y"], "words": cur})
    for ln in lines:
        ln["words"].sort(key=lambda w: w["x0"])
    return lines


# --------------------------------------------------------------- 列与单元格

def _slots(lines: list[dict]) -> list[tuple[float, float]]:
    """列槽 = 用**纵向走廊**切出来的区间：每行在那个 x 段都空着，才算是列边界。

    ⚠️ 早先的做法是"拿词数最多的那一行，按相邻词的空档中点切列"。它在数值表上没问题，
    但在**含长文本列**的表上会彻底崩：Compass 第 5 页的符号表（真实只有 2 列
    `Symbol | Description`）里，Description 那一行有 12 个词，于是被切成 10 列 ——
    描述文字被拆成 `degree bound of | traversed | nodes | in HNSW | search`
    （用户实测截图反馈："你是识别不出来这个表格有几列吗？"）。

    走廊法的好处：**文本列里的词虽然多，但每个词的 x 位置逐行不同**，
    不存在"每行都空"的走廊，所以不会被切成碎片；而真正的列边界
    （符号列与描述列之间空出 353..365）在每一行都空着，会被稳定识别。
    """
    if not lines:
        return []
    # 每行的"空档"区间（相邻两词之间）
    row_gaps: list[list[tuple[float, float]]] = []
    for ln in lines:
        ws = ln["words"]
        if len(ws) < 2:
            row_gaps.append([])
            continue
        row_gaps.append([(ws[i]["x1"], ws[i + 1]["x0"])
                         for i in range(len(ws) - 1)
                         if ws[i + 1]["x0"] > ws[i]["x1"]])
    n = len(lines)
    # 允许极少数行"挡住"走廊（例如某行文字特别长），但绝大多数行必须让开
    need = max(1, math.ceil(n * 0.9))

    lo = min((w["x0"] for ln in lines for w in ln["words"]), default=0.0)
    hi = max((w["x1"] for ln in lines for w in ln["words"]), default=0.0)
    if hi <= lo:
        return []

    # 以 1pt 为步长扫描：这个位置被多少行"空着"
    step = 1.0
    corridors: list[tuple[float, float]] = []
    open_from: float | None = None
    x = lo
    while x <= hi + step:
        free = sum(1 for gaps in row_gaps
                   if any(a - 0.5 <= x <= b + 0.5 for a, b in gaps))
        if free >= need:
            if open_from is None:
                open_from = x
        else:
            if open_from is not None:
                corridors.append((open_from, x))
                open_from = None
        x += step
    if open_from is not None:
        corridors.append((open_from, hi + step))

    # 太窄的不算走廊（词间距本身就有 2–3pt）
    bounds = [(a + b) / 2 for a, b in corridors if b - a >= _MIN_CORRIDOR]
    if len(bounds) < 1:
        # 走廊法切不出列（多见于"某一列文字折行、占满整宽"的表，如 Compass 的 Table 3），
        # 退回"最宽行"的切法 —— 它在**数值表**上是可靠的。
        return _slots_by_widest_row(lines)
    out: list[tuple[float, float]] = []
    start = lo - 8
    for b in bounds:
        out.append((start, b))
        start = b
    out.append((start, hi + 8))
    return out


def _slots_by_widest_row(lines: list[dict]) -> list[tuple[float, float]]:
    """回退方案：拿词数最多的那一行，按相邻词的空档中点切列。

    ⚠️ 只能用在数值/短单元格的表上：含长文本列时会把描述文字拆成一堆假列
    （Compass 第 5 页符号表就是这么被切成 10 列的）。
    """
    if not lines:
        return []
    ws = max(lines, key=lambda ln: len(ln["words"]))["words"]
    if not ws:
        return []
    if len(ws) == 1:
        return [(ws[0]["x0"] - 8, ws[0]["x1"] + 8)]
    edges = [(ws[i]["x1"] + ws[i + 1]["x0"]) / 2 for i in range(len(ws) - 1)]
    bounds = [ws[0]["x0"] - 8] + edges + [ws[-1]["x1"] + 8]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def _slots_of_word(w: dict, slots: list[tuple[float, float]]) -> tuple[int, int]:
    """词覆盖到的槽范围 (start, end)。

    判据是"**所有**实质重叠的槽"，不是"重叠最大的那个槽" —— 跨列组头
    （`Perplexity` 宽 47.9，与列 1 重叠 26.9、与列 2 重叠 21）必须判成跨两列，
    取最大值只会得到一列，跨列信息就丢了（踩过）。
    阈值取 `max(1.5, 0.25×词宽)`：太小的擦边重叠不算，避免把普通词误判成跨列。
    """
    width = w["x1"] - w["x0"]
    thr = max(1.5, 0.25 * width)
    covered = [i for i, (a, b) in enumerate(slots)
               if min(w["x1"], b) - max(w["x0"], a) >= thr]
    if not covered:
        c = (w["x0"] + w["x1"]) / 2
        i = min(range(len(slots)),
                key=lambda k: abs((slots[k][0] + slots[k][1]) / 2 - c))
        return i, i
    return covered[0], covered[-1]


def _cells_by_slots(line: dict, slots: list[tuple[float, float]]) -> list[dict]:
    """一行的词按列槽聚成单元格。span 由词覆盖到的槽范围决定。"""
    raw: list[dict] = []
    for w in line["words"]:
        start, end = _slots_of_word(w, slots)
        if raw and raw[-1]["end"] >= start:          # 与上一格同槽或左跨过来
            raw[-1]["t"] = (raw[-1]["t"] + " " + w["t"]).strip()
            raw[-1]["end"] = max(raw[-1]["end"], end)
        else:
            raw.append({"t": w["t"], "start": start, "end": end})
    return [{"text": c["t"], "start": c["start"], "span": c["end"] - c["start"] + 1}
            for c in raw]


def _group_rows(lines: list[dict], size: float) -> list[list[dict]]:
    """按行距把物理行并成逻辑行。"""
    out: list[list[dict]] = []
    for ln in lines:
        if out and (ln["y"] - out[-1][-1]["y"]) < _ROW_GAP_RATIO * size:
            out[-1].append(ln)
        else:
            out.append([ln])
    return out


def _ranges_overlap(a: dict, b: dict) -> bool:
    return not (a["start"] + a["span"] - 1 < b["start"] or b["start"] + b["span"] - 1 < a["start"])


def _merge_row(lines: list[dict], slots: list[tuple[float, float]]) -> list[dict]:
    """一个逻辑行 → 单元格列表（多个物理行的同槽内容拼接）。"""
    merged: list[dict] = []
    for ln in lines:
        for c in _cells_by_slots(ln, slots):
            hit = next((m for m in merged if _ranges_overlap(m, c)), None)
            if hit:
                hit["text"] = (hit["text"] + " " + c["text"]).strip()
                lo = min(hit["start"], c["start"])
                hi = max(hit["start"] + hit["span"], c["start"] + c["span"])
                hit["start"], hit["span"] = lo, hi - lo
            else:
                merged.append(dict(c))
    return sorted(merged, key=lambda c: c["start"])


def _build_header(header_lines: list[dict], slots: list[tuple[float, float]],
                  size: float) -> list[list[dict]]:
    """表头 → 若干行。

    分两种情况：
      * **没有跨列组头**（普通单层表头，可能换行）：所有表头行按列槽合并成一行，
        `Time` + `(s)` → `Time (s)`。实测 Table 2 走的这条路。
      * **有跨列组头**（`Perplexity` 横跨列 1–2）：先把**范围相同**的组头合成一个
        （`Accuracy` 与下一行的 `(Larger is better)` 本来就是同一个表头单元），
        再把落在组头范围内的窄格吸收进去（`(Lower is` / `better)` → `Perplexity (Lower is better)`）；
        不含任何组头的行（如 `Float | Fixed | Float | Fixed`）单独成行。
        实测 Table 1 走的这条路，得到标准的两级表头。
    """
    if not header_lines:
        return []
    per_line = [_cells_by_slots(ln, slots) for ln in header_lines]
    groups = [c for cells in per_line for c in cells if c["span"] >= 2]
    if not groups:
        return [_merge_row(header_lines, slots)]

    # 1) 先分类：整行都是单列格 → 这是"子表头行"（`Float | Fixed | Float | Fixed`），
    #    它自己就是一行，**绝不能**被组头吸收（否则得到
    #    `Perplexity (Lower is better) Float Fixed` 这种怪物）。
    #    含跨列格的行 → 属于组头块，供下面的合并与吸收使用。
    plain_rows: list[list[dict]] = [sorted(cells, key=lambda c: c["start"])
                                    for cells in per_line
                                    if all(c["span"] < 2 for c in cells)]
    group_lines: list[list[dict]] = [cells for cells in per_line
                                     if any(c["span"] >= 2 for c in cells)]

    # 2) 范围完全相同的组头合并成一个（文本按行序拼接）：
    #    `Accuracy` 与下一行的 `(Larger is better)` 本来就是同一个表头单元。
    merged_groups: list[dict] = []
    for cells in group_lines:
        for c in cells:
            if c["span"] < 2:
                continue
            same = next((g for g in merged_groups
                         if g["start"] == c["start"] and g["span"] == c["span"]), None)
            if same:
                same["text"] = (same["text"] + " " + c["text"]).strip()
            else:
                merged_groups.append(dict(c))

    # 3) 把组头块里的窄格吸收进覆盖它的组头：
    #    `(Lower is` / `better)` → `Perplexity (Lower is better)`
    for g in merged_groups:
        lo, hi = g["start"], g["start"] + g["span"] - 1
        extra: list[str] = []
        for cells in group_lines:
            for c in cells:
                if c["span"] >= 2:
                    continue
                if c["start"] >= lo and c["start"] + c["span"] - 1 <= hi \
                        and c["text"] not in g["text"]:
                    extra.append(c["text"])
        if extra:
            g["text"] = (g["text"] + " " + " ".join(extra)).strip()

    # 4) 组头块里不落在任何组头范围内的窄格（如 `Model Name`）自成单元格
    others: list[dict] = []
    for cells in group_lines:
        for c in cells:
            if c["span"] < 2 and not any(_ranges_overlap(g, c) for g in merged_groups):
                others.append(dict(c))

    header_row = sorted(merged_groups + others, key=lambda c: c["start"])
    if not plain_rows and not header_row:
        return [_merge_row(header_lines, slots)]
    return [header_row] + plain_rows


# --------------------------------------------------------------- 输出

def _rows_to_grid(rows: list[list[dict]], columns: int) -> list[list[Any]]:
    """把 {(start, span)} 行补成固定列数的网格；被 span 覆盖的位置为 None。"""
    grid: list[list[Any]] = []
    for row in rows:
        line: list[Any] = [None] * columns
        for c in row:
            if c["start"] >= columns:
                continue
            span = max(1, min(c["span"], columns - c["start"]))
            line[c["start"]] = {"text": c["text"], "span": span}
            for k in range(c["start"] + 1, c["start"] + span):
                line[k] = None
        grid.append(line)
    return grid


def _has_text(row: list[Any]) -> bool:
    return any(isinstance(c, dict) and (c.get("text") or "").strip() for c in row)


def grid_to_markdown(table: dict) -> str:
    """网格 → Markdown 表格（搜索、纯文本导出、"原始抽取"视图用）。"""
    rows = [r for r in (table.get("rows") or []) if _has_text(r)]
    if len(rows) < 2:
        return ""
    lines: list[str] = []
    head = int(table.get("head_rows") or 0)
    for i, row in enumerate(rows):
        cells = [re.sub(r"\s+", " ", (c or {}).get("text", "")).replace("|", "\\|")
                 if isinstance(c, dict) else "" for c in row]
        lines.append("| " + " | ".join(cells) + " |")
        if i == head - 1:
            lines.append("|" + "---|" * len(cells))
    return "\n".join(lines)


def grid_to_text(table: dict) -> str:
    """网格 → 制表符分隔的纯文本（塞进提示词时用，比 JSON 省 token 且更直观）。"""
    out = []
    for row in table.get("rows") or []:
        cells = [(c or {}).get("text", "") if isinstance(c, dict) else "" for c in row]
        out.append(" \t ".join(cells))
    return "\n".join(out)


def _ruled_tables(page: Any, taken: list[dict],
                  captions: list[dict] | None = None) -> list[dict]:
    """兜底：**带框线**的表格交给 PyMuPDF 的 `lines` 策略。

    上面那套"等宽横线"只认 booktabs（顶/中/底三条横线）。但表格也可能是**每个单元格
    都画了框**的（Word 导出、Excel 截图转 PDF 常见）—— 那种表的横线被列切开，
    每条只有一列宽，x 范围各不相同，按"等宽分组"就漏了（合成样张正是这种）。
    对这种表 `find_tables(strategy="lines")` 反而很好用，所以两条路都留着。

    与已识别的区域重叠时跳过，避免同一张表抽两遍。

    ⚠️ 这条路也必须过题注判据：**图的坐标轴 + 网格线本身就会围出一堆闭合单元格**，
    `lines` 策略会把整张折线图当表格（实测 Compass 的 Figure 7 就这么漏过来的）。
    所以挂 `Figure N` 题注的一律不要。
    """
    try:
        finder = page.find_tables(strategy="lines")
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for t in getattr(finder, "tables", None) or []:
        try:
            raw = t.extract()
        except Exception:  # noqa: BLE001
            continue
        bbox = tuple(float(v) for v in t.bbox)
        if any(_rects_overlap(bbox, other["bbox"]) for other in taken) or \
                any(_rects_overlap(bbox, other["bbox"]) for other in out):
            continue
        if captions:
            cap_reg = {"x0": bbox[0], "x1": bbox[2], "ys": [bbox[1], bbox[3]]}
            if _caption_kind(captions, cap_reg) == "figure":
                continue        # 坐标轴网格围出的"假表格"
        rows: list[list[Any]] = []
        for r in raw or []:
            cells = [{"text": re.sub(r"\s+", " ", str(c or "")).strip(), "span": 1}
                     for c in (r or [])]
            if any(c["text"] for c in cells):
                rows.append(cells)
        if len(rows) < 2:
            continue
        columns = max(len(r) for r in rows)
        rows = [r + [{"text": "", "span": 1}] * (columns - len(r)) for r in rows]
        out.append({"bbox": bbox, "kind": "table", "note": "框线表格",
                    "table": {"columns": columns, "head_rows": 1, "rows": rows}})
    return out


def _rects_overlap(a: tuple, b: tuple) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def pseudocode_score(region: dict, page: Any, size: float = 10.0) -> tuple[int, list[str]]:
    """判断这块"横线夹着的东西"是不是**伪代码/算法**（返回分值 + 依据）。

    为什么必须单独判：LaTeX 的 `algorithm` 环境上下各一条 `\\hrule`，
    看起来和 booktabs 表格一模一样（都是"两条等宽横线夹内容"）。
    实测用户论文里的 `Algorithm 1: SEARCH(...)` 就这么被当成了 5 列表格，
    行号、缩进全被拆成单元格 —— 用户原话"这个是伪代码，你为什么要把它放表格里？"

    判据（实测分离度很好：伪代码块得分 8，其它候选最高 2）：
      * 开头出现 `Algorithm` 标题
      * 多行以行号开头（`1`、`2`、`…`）
      * 出现伪代码关键字（for/foreach/while/if/then/do/return/…）
      * 同时有 `Input:` 与 `Output:`
    """
    lines = _physical_lines(_words_in(page, region), size)
    texts = [" ".join(w["t"] for w in ln["words"]) for ln in lines]
    joined = " ".join(texts).lower()
    score, why = 0, []
    if "algorithm" in joined[:80]:
        score += 3
        why.append("有 Algorithm 标题")
    numbered = sum(1 for t in texts if t.strip()[:3].strip().isdigit())
    if numbered >= 3:
        score += 2
        why.append(f"{numbered} 行以行号开头")
    hits = [k for k in _PSEUDO_KEYWORDS if k in joined]
    if len(hits) >= 3:
        score += 2
        why.append(f"{len(hits)} 个伪代码关键字")
    if "input:" in joined and "output:" in joined:
        score += 1
        why.append("有 Input/Output")
    return score, why


def algorithm_lines(page: Any, region: dict, size: float = 10.0) -> list[str]:
    """把伪代码块按**物理行**取出来，保留行号与缩进。

    表格的网格化在这里是有害的：伪代码靠"一行一条语句 + 缩进层级"表达结构，
    拆成单元格就没法读了。所以这里只按行拼接，并按左边界换算成前导空格。
    """
    lines = _physical_lines(_words_in(page, region), size)
    if not lines:
        return []
    left = min(ln["words"][0]["x0"] for ln in lines if ln["words"])
    char_w = max(2.0, size * 0.5)     # 估一个字符宽，用来把缩进换算成空格数
    out: list[str] = []
    for ln in lines:
        if not ln["words"]:
            continue
        indent = int(max(0.0, (ln["words"][0]["x0"] - left)) / char_w + 0.5)
        out.append(" " * indent + " ".join(w["t"] for w in ln["words"]))
    return out


def looks_caption(text: str) -> bool:
    """这段文字像不像题注。

    ⚠️ 先剥掉开头的一串数字/点：PDF 里题注经常和上一行的刻度数字粘在一起
    （实测抽出来是 `0.12Figure 1: Latency breakdown.`），
    直接用 `match` 会因为开头是 `0.12` 而漏掉整个题注。
    """
    t = re.sub(r"^[\d\s.,%)\-]+", "", text or "").strip()
    return bool(_TABLE_CAPTION_RE.match(t) or _FIGURE_CAPTION_RE.match(t))


def looks_caption(text: str) -> bool:
    """这段文字像不像题注。

    ⚠️ 先剥掉开头的一串数字/点：PDF 里题注经常和上一行的刻度数字粘在一起
    （实测抽出来是 `0.12Figure 1: Latency breakdown.`），
    直接用 `match` 会因为开头是 `0.12` 而漏掉整个题注。
    """
    t = re.sub(r"^[\d\s.,%)\-]+", "", text or "").strip()
    return bool(_TABLE_CAPTION_RE.match(t) or _FIGURE_CAPTION_RE.match(t))


def _norm_ws(s: str) -> str:
    """题注/单元格文本里的连续空格（PyMuPDF 常见）压成单个。"""
    return re.sub(r"\s+", " ", s or "").strip()


def _caption_near(captions: list[dict] | None, region: dict) -> dict | None:
    """返回区域上下 40pt 内那个题注本身（含文本与 bbox）。

    题注是**最可靠的判据**（`Table N:` vs `Figure N:`），同时也直接拿来当
    表格标题 / 图题用 —— 所以这里返回题注对象而不只是类型。
    """
    if not captions:
        return None
    top, bot = region["ys"][0], region["ys"][-1]
    x0, x1 = region["x0"], region["x1"]
    best: dict | None = None
    for cap in captions:
        cy = cap["bbox"][1]
        if not ((top - 40 <= cy <= top + 4) or (bot - 4 <= cy <= bot + 40)):
            continue
        if cap["bbox"][2] < x0 or cap["bbox"][0] > x1:
            continue
        if best is None or cy > best["bbox"][1]:
            best = cap
    return best


def figure_area(page: Any, caption: dict, captions: list[dict] | None = None,
                max_up: float = 330.0) -> dict:
    """圈出图题上方那块图所占的纵向范围（用来收集"图里的文字"）。

    图里的文字（坐标轴刻度、图例、示意图里的字）不能当正文段落去翻 —— 它们属于图。

    ⚠️ 关键是**别越过上方另一张图**：早先简单地"从图题往上取 330pt"，
    结果把上一张图的刻度也圈进来，纯图形的那张于是"看起来有文字"、
    模型就不会写译注了（踩过）。这里改成从图题往上逐块走，
    遇到明显的空白（> _MAX_FIG_GAP）就停 —— 空白就是图与图/图与正文的界。
    """
    cap_top = caption["bbox"][1]
    x0, x1 = caption["bbox"][0], caption["bbox"][2]
    try:
        boxes: list[tuple] = []
        for d in page.get_drawings():
            r = d.get("rect")
            if r is not None:
                boxes.append((r.x0, r.y0, r.x1, r.y1))
        for img in page.get_image_info():
            b = img.get("bbox")
            if b:
                boxes.append(tuple(b))
    except Exception:  # noqa: BLE001
        boxes = []
    # 边界一：上面若还有**另一条题注**，它就是上一张图的结束 —— 到此为止。
    # 这比"按空白宽度猜"可靠得多（实测两张图只隔 50pt 时，
    # 单纯用空白阈值会把上一张图的刻度一起圈进来）。
    ceiling = cap_top - max_up
    for other in captions or []:
        if other is caption:
            continue
        oy = other["bbox"][3]
        if ceiling < oy < cap_top:
            ceiling = max(ceiling, oy)
    near = [b for b in boxes
            if b[3] <= cap_top + 2 and b[2] >= x0 and b[0] <= x1
            and b[1] >= ceiling]
    near.sort(key=lambda b: b[3], reverse=True)      # 从最靠近图题的往上走
    lo = cap_top
    keep: list[tuple] = []
    for bx0, by0, bx1, by1 in near:
        if lo - by1 > _MAX_FIG_GAP:
            break            # 空白太宽 → 上面那一块不是同一张图
        lo = min(lo, by0)
        keep.append((bx0, by0, bx1, by1))
    # 横向也要用**图自己的范围**：题注往往比图窄（缩进过），
    # 只按题注的 x 取词会把坐标轴刻度挡在外面（踩过：整张图变成"无文字"）。
    if keep:
        x0 = min([x0] + [b[0] for b in keep])
        x1 = max([x1] + [b[2] for b in keep])
    return {"x0": x0, "x1": x1, "top": max(lo, ceiling), "bottom": cap_top - 2}


def figure_text(page: Any, area: dict, size: float = 10.0) -> list[str]:
    """取图范围内的文字行（避开图题本身）。图是纯图形时返回空列表。"""
    words = []
    try:
        for w in page.get_text("words"):
            if (area["x0"] - 6 <= w[0] <= area["x1"] + 6
                    and area["top"] <= w[1] <= area["bottom"] - 4):
                words.append({"x0": float(w[0]), "x1": float(w[2]),
                              "y": float(w[1]), "t": str(w[4])})
    except Exception:  # noqa: BLE001
        return []
    if len(words) < 3:
        return []
    lines: list[str] = []
    for ln in _physical_lines(words, size):
        text = " ".join(w["t"] for w in ln["words"]).strip()
        if text:
            lines.append(text)
    return lines[:40]


def looks_like_figure_label(text: str) -> bool:
    """这段文字像不像"图上的标签"（图例、子图标题、坐标轴说明）。

    判据：短、没有句号分隔的句子、没有明显的句子结构。
    典型的图例长这样：`Compass Compass w/o Lazy Eviction Compass w/ vanilla Ring ORAM`。
    """
    t = " ".join((text or "").split())
    if not t or len(t) > 180:
        return False
    # 有"句号 + 空格 + 大写"就是正文句子，不是标签
    return not re.search(r"[.!?]\s+[A-Z]", t)


def _extend_up_for_labels(page: Any, area: dict,
                          captions: list[dict] | None = None,
                          max_up: float = 80.0) -> float:
    """把图的范围向上扩到包住紧邻的标签块，返回新的 top。

    ⚠️ 为什么必须扩：图的**图例常常画在图框上方**，不在图框内。
    只按图框取文字的话，图例会被当成正文段落抽出来 —— 于是
    `Compass Compass w/o Lazy Eviction …` 这种图例进了正文，
    模型还会"认真翻译"它（规格 §5 明确说图内文字不该当正文翻译）。
    """
    top = float(area["top"])
    x0, x1 = float(area["x0"]), float(area["x1"])
    # 上界：**上面若还有另一条题注，它就是上一张图的结束** —— 到此为止。
    # ⚠️ 不设这个上界，标签扩展会一路吞掉上一张图的刻度（踩过：合成页里
    # Figure 2 的范围被扩到 Figure 1 的刻度区，纯图形图于是"凭空有内容"）。
    ceiling = top - max_up
    for other in captions or []:
        oy = float(other["bbox"][3])
        if oy < top and oy > ceiling:
            ceiling = oy
    try:
        blocks = page.get_text("blocks")
    except Exception:  # noqa: BLE001
        return top
    changed = True
    while changed:
        changed = False
        for b in blocks:
            bx0, by0, bx1, by1, text = float(b[0]), float(b[1]), float(b[2]), float(b[3]), b[4]
            if by1 > top + 2 or by1 < ceiling:
                continue
            if bx1 < x0 or bx0 > x1:
                continue
            # ⚠️ 往上走遇到**题注**就停：那是上一张图的结束。
            # 不判的话会一路吞掉上一张图的刻度（测试当场抓到：合成页的
            # "纯图形图"因此凭空多出内容，模型就不写译注了）。
            if looks_caption(text):
                continue
            if not looks_like_figure_label(text):
                continue
            top = by0
            changed = True
    return top


def extract_figures(page: Any, captions: list[dict] | None,
                    used: list[dict], size: float = 10.0,
                    regions: list[dict] | None = None) -> list[dict]:
    """抽取"图"→ [{bbox, kind:"figure", caption, content}]。

    为什么值得单独抽：图里的文字（图例、示意文字）以前要么被当正文翻掉、
    要么被丢弃（挂了 `Figure` 题注的区域直接不处理）。参照成品稿的做法是
    **把图框起来**：图内的文字照翻，图题也翻；图是纯图形时给一条**译注**
    说明"这里原本是一幅什么图"。

    `used` 是已经识别成表格/伪代码的区域，避免把图上的网格线再认一遍。
    """
    out: list[dict] = []
    for cap in captions or []:
        if not _FIGURE_CAPTION_RE.match(cap.get("text") or ""):
            continue
        if any(_rects_overlap(cap["bbox"], u["bbox"]) for u in used):
            continue
        # 优先用"已识别的区域"当图的范围：带边框的图（安全游戏那种盒子）
        # 上下两条横线本身就是它的边界 —— 而"从图题往上按空白走"会在盒子内部停下，
        # 把整块内容当成空白（踩过：Figure 4 的 17 行步骤全丢）。
        area = None
        for reg in regions or []:
            if reg["ys"][-1] >= cap["bbox"][1] - 2:
                continue
            if cap["bbox"][1] - reg["ys"][-1] > 40:
                continue
            if reg["x1"] < cap["bbox"][0] or reg["x0"] > cap["bbox"][2]:
                continue
            # 下界用**题注的顶边**：用底边会把题注自己的文字算进"图内文字"
            # （踩过：纯图形图因此"多出 3 行"，模型就不写译注了）
            area = {"x0": reg["x0"], "x1": reg["x1"],
                    "top": reg["ys"][0], "bottom": cap["bbox"][1] - 2}
            break
        if area is None:
            area = figure_area(page, cap, captions)     # 无边框的图：退回按空白走
        # 把图上方的"标签块"也圈进来（图例、子图标题那些）
        area["top"] = _extend_up_for_labels(page, area, captions)

        content = figure_text(page, area, size)
        out.append({"bbox": (area["x0"], area["top"], area["x1"], cap["bbox"][3]),
                    "kind": "figure",
                    "figure": {"caption": cap["text"], "content": content},
                    "caption_bbox": tuple(cap["bbox"]),
                    "note": f"图内文字 {len(content)} 行" if content else "纯图形（需模型写译注）"})
    return out

    """区域上下 40pt 内有没有题注，是 Table 还是 Figure。

    这是**最可靠的判据**：论文里真表格一定有 `Table N:` 题注，
    而图（含坐标轴网格线、散落的刻度标签）挂的是 `Figure N:`。
    实测用它一次就把 7 个误判（正文/图表刻度）全部摘掉，7 个真表格全部保留。
    """
    if not captions:
        return None
    top, bot = region["ys"][0], region["ys"][-1]
    x0, x1 = region["x0"], region["x1"]
    for cap in captions:
        cy = cap["bbox"][1]
        if not ((top - 40 <= cy <= top + 4) or (bot - 4 <= cy <= bot + 40)):
            continue
        if cap["bbox"][2] < x0 or cap["bbox"][0] > x1:      # 横向也要有交集
            continue
        text = (cap.get("text") or "").strip()
        if _TABLE_CAPTION_RE.match(text):
            return "table"
        if _FIGURE_CAPTION_RE.match(text):
            return "figure"
    return None


def _caption_kind(captions: list[dict] | None, region: dict) -> str | None:
    """区域上下 40pt 内有没有题注，是 Table 还是 Figure。

    这是**最可靠的判据**：论文里真表格一定有 `Table N:` 题注，
    而图（含坐标轴网格线、散落的刻度标签）挂的是 `Figure N:`。
    实测用它一次就把 7 个误判（正文/图表刻度）全部摘掉，7 个真表格全部保留。
    """
    cap = _caption_near(captions, region)
    if not cap:
        return None
    text = _norm_ws(cap.get("text") or "")
    if _TABLE_CAPTION_RE.match(text):
        return "table"
    if _FIGURE_CAPTION_RE.match(text):
        return "figure"
    return None


def _looks_like_table(grid: dict, limit_cols: int = 8) -> bool:
    """没有题注时的严格回退：列数适中、行数够、格子填得比较满。

    用来放行"有表格但没有 Table 题注"的情况（例如附录里的表），
    同时挡住被切碎的正文（那种往往列数很多、空格子很多）。
    """
    columns = int(grid.get("columns") or 0)
    rows = grid.get("rows") or []
    if not (2 <= columns <= limit_cols) or len(rows) < 2:
        return False
    cells = [c for r in rows for c in r if isinstance(c, dict)]
    filled = sum(1 for c in cells if (c.get("text") or "").strip())
    return bool(cells) and filled / len(cells) >= 0.55


def _drop_caption_lines(lines: list[dict]) -> list[dict]:
    """把题注行从表体里剔掉。

    `_words_in` 会向下多取几个点（怕漏掉贴着横线的内容），于是**表格下方的
    `Table N:` 题注会被当成表体的一行**参与列切分 —— 题注的文字分布跟表体完全不同，
    会在走廊计算里制造出一堆假边界（实测：自造的符号表因此从 2 列变成 8 列）。
    题注本来由 extract.py 单独抽成 caption 段落，这里剔掉即可。
    """
    out: list[dict] = []
    for ln in lines:
        text = " ".join(w["t"] for w in ln["words"]).strip()
        if looks_caption(text):
            continue
        out.append(ln)
    return out


def extract_tables(page: Any, size: float = 10.0,
                   captions: list[dict] | None = None) -> list[dict]:
    """抽取本页的结构化块 → [{bbox, kind, table|algorithm, note}]。

    - `kind="table"`：{columns, head_rows, rows}
    - `kind="algorithm"`：{lines}（伪代码，保留行号与缩进）
    - 判定不确定的直接**丢弃**（返回里没有它），这样该区域的正文不会被抑制，
      仍会作为普通段落抽出来 —— 比硬塞进表格里好。
    """
    out: list[dict] = []
    regions = table_regions(page)
    for reg in regions:
        words = _words_in(page, reg)
        if len(words) < 4:
            continue
        bbox_full = (reg["x0"], reg["ys"][0], reg["x1"], reg["ys"][-1])
        score, why = pseudocode_score(reg, page, size)
        if score >= 4:
            lines = algorithm_lines(page, reg, size)
            if len(lines) >= 3:
                out.append({"bbox": bbox_full, "kind": "algorithm",
                            "algorithm": {"lines": lines},
                            "note": "伪代码：" + "、".join(why)})
                continue
        cap = _caption_near(captions, reg)
        cap_kind = None
        if cap:
            text = _norm_ws(cap.get("text") or "")
            cap_kind = ("table" if _TABLE_CAPTION_RE.match(text)
                        else "figure" if _FIGURE_CAPTION_RE.match(text) else None)
        if cap_kind == "figure":
            continue        # 挂 Figure 题注 → 是图，交给 extract_figures

        lines = _drop_caption_lines(_physical_lines(words, size))
        if not lines:
            continue
        ys = reg["ys"]
        head_lim = ys[1] if len(ys) >= 2 else ys[0]     # 第二条横线 = midrule
        header_lines = [ln for ln in lines if ln["y"] < head_lim - 1.0]
        body_lines = [ln for ln in lines if ln["y"] >= head_lim - 1.0]
        slots = _slots(body_lines) or _slots(lines)
        if not slots:
            continue
        columns = len(slots)

        head_rows: list[list[dict]] = _build_header(header_lines, slots, size)
        body_rows = [_merge_row(lns, slots) for lns in _group_rows(body_lines, size)]

        rows = _rows_to_grid(head_rows + body_rows, columns)
        # 丢掉全空行，但表头条数要按"丢掉之后"重新数
        grid: list[list[Any]] = []
        head_count = 0
        for i, row in enumerate(rows):
            if not _has_text(row):
                continue
            grid.append(row)
            if i < len(head_rows):
                head_count += 1
        if len(grid) < 2:
            continue
        table = {"columns": columns, "head_rows": head_count, "rows": grid}
        if cap_kind != "table" and not _looks_like_table(table):
            continue        # 既没 Table 题注、结构又不像表格 → 丢给普通段落
        out.append({"bbox": bbox_full, "kind": "table", "table": table,
                    "caption": (_norm_ws(cap["text"]) if cap_kind == "table" and cap else ""),
                    "caption_bbox": (tuple(cap["bbox"]) if cap_kind == "table" and cap else None),
                    "note": "有 Table 题注" if cap_kind == "table" else "结构判定"})
    out.extend(_ruled_tables(page, out, captions))
    # 图：挂在 Figure 题注上的那块（含图内文字）。放在表格之后，
    # 因为要按已识别的表格/伪代码区域排除重叠。
    out.extend(extract_figures(page, captions, out, size, list(regions)))
    out.sort(key=lambda t: (t["bbox"][1], t["bbox"][0]))
    return out
