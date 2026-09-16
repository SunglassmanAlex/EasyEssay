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

## 已知边界（如实说明）

* `\\multirow`（跨行单元格）不支持：单元格竖直居中，会并进相邻行，
  实测表现为 `SIGMA 32`（正确是 `32 | SIGMA`）。仍是一张可读的表，只是该格归属有偏差。
* 完全没有横线的"纯空白对齐"表格识别不到（没有可靠的行边界）。
"""
from __future__ import annotations

import re
from typing import Any

_ROW_GAP_RATIO = 0.8        # 行距小于 0.8×字号 → 同一逻辑行的续行
_MIN_RULE_WIDTH = 30.0      # 太短的线段不算横线
_MIN_RULE_COUNT = 2         # 至少两条横线才可能是表格
_MIN_TABLE_HEIGHT = 15.0


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


def table_regions(page: Any) -> list[dict]:
    """按 x 范围把横线分组 → 表格区域。`\\cmidrule` 宽度不同，会自然落单。"""
    groups: dict[tuple[float, float], list[float]] = {}
    for y, x0, x1 in _rules(page):
        groups.setdefault((x0, x1), []).append(y)
    out: list[dict] = []
    for (x0, x1), ys in groups.items():
        ys = sorted(ys)
        if len(ys) >= _MIN_RULE_COUNT and ys[-1] - ys[0] >= _MIN_TABLE_HEIGHT:
            out.append({"x0": x0, "x1": x1, "ys": ys})
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
    """列槽 = 用"词数最多"的表体行切出的区间。"""
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


def _ruled_tables(page: Any, taken: list[dict]) -> list[dict]:
    """兜底：**带框线**的表格交给 PyMuPDF 的 `lines` 策略。

    上面那套"等宽横线"只认 booktabs（顶/中/底三条横线）。但表格也可能是**每个单元格
    都画了框**的（Word 导出、Excel 截图转 PDF 常见）—— 那种表的横线被列切开，
    每条只有一列宽，x 范围各不相同，按"等宽分组"就漏了（合成样张正是这种）。
    对这种表 `find_tables(strategy="lines")` 反而很好用，所以两条路都留着。

    与已识别的区域重叠时跳过，避免同一张表抽两遍。
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
        out.append({"bbox": bbox,
                    "table": {"columns": columns, "head_rows": 1, "rows": rows}})
    return out


def _rects_overlap(a: tuple, b: tuple) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def extract_tables(page: Any, size: float = 10.0) -> list[dict]:
    """抽取本页表格 → [{bbox, table}]，table = {columns, head_rows, rows}。

    两条路：booktabs（等宽横线）自己重建；带框线的交给 PyMuPDF。
    """
    out: list[dict] = []
    for reg in table_regions(page):
        words = _words_in(page, reg)
        if len(words) < 4:
            continue
        lines = _physical_lines(words, size)
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
        out.append({"bbox": (reg["x0"], ys[0] + 2.0, reg["x1"], ys[-1] - 2.0),
                    "table": table})
    out.extend(_ruled_tables(page, out))
    out.sort(key=lambda t: (t["bbox"][1], t["bbox"][0]))
    return out
