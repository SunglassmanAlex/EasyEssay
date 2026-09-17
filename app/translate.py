"""逐段翻译流水线 + 选中片段问答。

翻译策略：
- 按字数/段数切批，一批一次请求，返回 JSON（段落 id 一一对应），从机制上保证左右对齐；
- 术语表跨批累积并回灌给模型，保证全文术语译法一致；
- 单批失败自动二分重试，仍失败的段落单独重试；
- 每批完成即落盘，可中断、可续翻（只翻缺失段落）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterator

from . import mathify, store, tables
from .deepseek import DeepSeekClient, DeepSeekError, make_client

OUTPUT_SPEC = r"""

【输出格式（必须严格遵守）】
只输出一个 JSON 对象，不要任何解释、不要 Markdown 代码块，形如：
{"items":[{"id":"p0001","en":"重建后的英文原文（公式为标准 LaTeX）","zh":"译文正文","terms":[{"en":"commitment","zh":"承诺"}]}]}
规则：
- items 与输入段落一一对应，id 必须原样返回，顺序一致，不得遗漏、合并或增补；
- en 是**重建后的英文原文**：文字内容与原文完全一致，只把数学内容规范化成标准 LaTeX
  （行内 $...$，独立 $$...$$）；若该段没有公式或本来就很规范，en 原样返回；
- zh 是译文正文；其中的数学内容必须保持 $...$（行内）或 $$...$$（独立）的 LaTeX 写法；
- 输入里的 `⟦?⟧` 是 PDF 无法辨认的字形（多为大型括号/矩阵分隔符/分式线）：
  按上下文还原成最可能的 LaTeX，**绝不在 zh 里保留 `⟦?⟧`**；
- 若该段是纯公式（$$...$$），en 与 zh 都原样返回该公式，不要添加解释；
- terms 只列该段的关键术语（最多 4 个，en/zh 对应），没有则给空数组 []；
- **kind 为 `figure` 的段落是图**：输入给的是 `figure.caption`（图题）、
  `figure.content`（图内文字，可能为空数组）与 `figure.labels`（图上标签：坐标轴刻度、
  图例、子图标题 —— 这些**不翻译**，但你要看它们来写译注）。
  返回 `"figure": {"caption": "图题译文", "content": [...], "note": "..."}`；
  **`caption` 必须有**（`Figure 1:` 译成 `图 1：`）；
  `content` 是图里**成句的文字**（示意框里的词、检索结果文本），逐条翻译，允许重新断行；
  **`note` 每一幅图都必须写**（硬要求，不是可选项）—— 一条译注告诉读者
  "原文这里是什么图、表达了什么"，例如：
  · 示意图：`〔译注：原文此处为四幅并排的遍历过程示意（a–d），图中节点编号 1–7 表示迭代轮次。此处保留图题与图例。〕`
  · 柱状图：`〔译注：原文此处为柱状图，上述数值为图中各柱的标注值，按原文顺序照录；具体对应关系以原图为准。〕`
  · 曲线图：`〔译注：原文此处为按数据集分面的四条 MRR@10–延迟曲线，无数值表格。此处保留图题与图例。〕`
  依据只能是**图题、图内文字、标签与上下文**，**不要编造图里没有的数字或结论**；
  并说明对读者有用的事实：图题与图例是否已保留、数值是不是图中标注值。
- **kind 为 `algorithm` 的段落是伪代码，按行处理**：输入给的是 `algorithm.lines`
  （一行的列表，已保留行号与缩进）。返回 `"algorithm": {"lines": [译文行, ...]}`，
  **行数与输入完全一致**，也不要返回 en/zh 字段。
  **每一行都要处理，必须真的翻译**（原样抄回来 = 没完成）：
  · 说明文字要翻成中文：`Input: query q, entry point ep,` → `输入：查询 q、入口点 ep`，
    `Output: ef closest neighbors to q` → `输出：与 q 最近的 ef 个邻居`；
  · `//` 之后的注释翻成中文；
  · 标识符/算法名/变量名（SEARCH、efspec、C、W）、关键字（for/if/while/return/foreach）、
    运算符（←）、数字**保持原样**；
  · 缩进与行号位置不要动。
- **kind 为 `table` 的段落要整表翻译**：输入给的是 `table.rows`（二维单元格数组）
  与 `table.columns`/`head_rows`。请返回 `"table": {"rows": [[译文, ...], ...]}`，
  **行列数与输入完全一致**（空的占位格保持为空），不要返回 en/zh 字段；
  表头同样要翻译（术语与正文保持一致）；数字、单位、模型名、缩写、符号原样保留；
  若输入给了 `caption`（表题），返回时也要带 `caption` 字段（`Table 1:` → `表 1：`）。。"""

# 模型把占位符改写成 `?`（或空括号）时，用于第二次尝试的加强指令
STRICT_REPAIR_HINT = r"""

【重要·上一轮的问题】你对无法辨认的字形占位符 ⟦?⟧ 的处理不合格 —— 要么改写成了 `?`，
要么换成了 `\left[\;\right]` 这种**空括号**。两者都是把问题藏起来：
`?` 不是公式的一部分；空括号虽然语法合法，但渲染出来是一对空框，内容照样丢了。
这次请务必：
- 不要出现任何 `?`；
- 不要给出任何空的定界符（`\left[\right]`、`\left(\right)`、`\langle\rangle` 一律不行），
  定界符里面**必须有内容**；
- **重点看前后相邻段落**：这次给你的不止目标段，还有它的上下文。论文里常有一条公式
  被抽取切成好几段的情况，某一段的可见字符可能只剩下括号或运算符，单看它必然无从
  判断 —— 它的真实含义在相邻的段落里。请把相邻段落当作同一条公式的一部分来理解。
- 结合上下文给出**最可能的字面推断**。按实测统计，⟦?⟧ 来自两类字形：
  ① **大型运算符**（`\sum`、`\prod`、`\int` 及其上下限）—— 注意不少"整段只有
     ⟦?⟧⟦?⟧"的碎片，其实是求和号加上它的上下限，或者是矩阵的左右括号；
  ② **定界符**：矩阵/向量的 `[ ]`、`( )`（竖排用
     `\left[\begin{matrix}a \\ b\end{matrix}\right]`）、集合的 `\{ \}`、
     范数 `\| \|`、内积/期望的 `\langle \rangle`，或分式横线。
  同一行成对出现的两个 ⟦?⟧ 基本就是矩阵方括号，而夹在 ⟦?⟧…⟦?⟧ 之间被竖排的
  那一串元素，正是要放进 `\begin{matrix}…\end{matrix}` 里的内容；
- 实在无法确定时，选择上下文里最常用的那一种，但**不要留 `?`、不要留 ⟦?⟧、
  不要留空括号**。
"""


# 关闭「原文重建」时的输出协议（只翻译）
OUTPUT_SPEC_NO_RESTORE = r"""

【输出格式（必须严格遵守）】
只输出一个 JSON 对象，不要任何解释、不要 Markdown 代码块，形如：
{"items":[{"id":"p0001","zh":"译文正文","terms":[{"en":"commitment","zh":"承诺"}]}]}
规则：
- items 与输入段落一一对应，id 必须原样返回，顺序一致，不得遗漏、合并或增补；
- zh 是译文正文；其中的数学内容必须保持 $...$（行内）或 $$...$$（独立）的 LaTeX 写法；
- 输入里的 `⟦?⟧` 是 PDF 无法辨认的字形（多为大型括号/矩阵分隔符/分式线）：
  按上下文还原成最可能的 LaTeX，**绝不在 zh 里保留 `⟦?⟧`**；
- 若该段是纯公式（$$...$$），zh 原样返回该公式，不要添加解释；
- terms 只列该段的关键术语（最多 4 个，en/zh 对应），没有则给空数组 []；
- **kind 为 `figure` 的段落是图**：输入给的是 `figure.caption`（图题）、
  `figure.content`（图内文字，可能为空数组）与 `figure.labels`（图上标签：坐标轴刻度、
  图例、子图标题 —— 这些**不翻译**，但你要看它们来写译注）。
  返回 `"figure": {"caption": "图题译文", "content": [...], "note": "..."}`；
  **`caption` 必须有**（`Figure 1:` 译成 `图 1：`）；
  `content` 是图里**成句的文字**（示意框里的词、检索结果文本），逐条翻译，允许重新断行；
  **`note` 每一幅图都必须写**（硬要求，不是可选项）—— 一条译注告诉读者
  "原文这里是什么图、表达了什么"，例如：
  · 示意图：`〔译注：原文此处为四幅并排的遍历过程示意（a–d），图中节点编号 1–7 表示迭代轮次。此处保留图题与图例。〕`
  · 柱状图：`〔译注：原文此处为柱状图，上述数值为图中各柱的标注值，按原文顺序照录；具体对应关系以原图为准。〕`
  · 曲线图：`〔译注：原文此处为按数据集分面的四条 MRR@10–延迟曲线，无数值表格。此处保留图题与图例。〕`
  依据只能是**图题、图内文字、标签与上下文**，**不要编造图里没有的数字或结论**；
  并说明对读者有用的事实：图题与图例是否已保留、数值是不是图中标注值。
- **kind 为 `algorithm` 的段落是伪代码，按行处理**：输入给的是 `algorithm.lines`
  （一行的列表，已保留行号与缩进）。返回 `"algorithm": {"lines": [译文行, ...]}`，
  **行数与输入完全一致**，也不要返回 en/zh 字段。
  **每一行都要处理，必须真的翻译**（原样抄回来 = 没完成）：
  · 说明文字要翻成中文：`Input: query q, entry point ep,` → `输入：查询 q、入口点 ep`，
    `Output: ef closest neighbors to q` → `输出：与 q 最近的 ef 个邻居`；
  · `//` 之后的注释翻成中文；
  · 标识符/算法名/变量名（SEARCH、efspec、C、W）、关键字（for/if/while/return/foreach）、
    运算符（←）、数字**保持原样**；
  · 缩进与行号位置不要动。
- **kind 为 `table` 的段落要整表翻译**：输入给的是 `table.rows`（二维单元格数组）
  与 `table.columns`/`head_rows`。请返回 `"table": {"rows": [[译文, ...], ...]}`，
  **行列数与输入完全一致**（空的占位格保持为空），不要返回 en/zh 字段；
  表头同样要翻译（术语与正文保持一致）；数字、单位、模型名、缩写、符号原样保留。。"""

# 「只重建左栏、不重译」用的输出协议：输出更短，省钱
OUTPUT_SPEC_RESTORE = r"""

【任务】把每段英文原文**重建**成规范排版的文本：文字内容一字不改，只把数学内容
（上下标、分式、求和号、\vec/\mathrm/\langle 等语义命令、被拉平或粘连的公式）
按数学含义还原成标准 LaTeX（行内 $...$，独立 $$...$$）。

【输出格式（必须严格遵守）】
只输出一个 JSON 对象，不要任何解释、不要 Markdown 代码块，形如：
{"items":[{"id":"p0001","en":"重建后的英文原文"}]}
规则：
- items 与输入段落一一对应，id 必须原样返回，顺序一致，不得遗漏；
- 不要输出 zh 字段，不要翻译，不要增删或改写任何文字；
- 若某段本来就很规范，en 原样返回即可；
- **kind 为 `table` / `algorithm` / `figure` 的段落原样返回**，不要翻译、不要改动内容。"""


def _chunk(paras: list[dict], batch_size: int, max_chars: int) -> list[list[dict]]:
    batches: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0
    for p in paras:
        length = len(p.get("text", ""))
        if cur and (len(cur) >= batch_size or cur_chars + length > max_chars):
            batches.append(cur)
            cur, cur_chars = [], 0
        cur.append(p)
        cur_chars += length
    if cur:
        batches.append(cur)
    return batches


_WS_RE = re.compile(r"\s+")


def _norm_ws(s: str) -> str:
    return _WS_RE.sub(" ", (s or "")).strip()


def table_for_prompt(grid: dict) -> dict:
    """把结构化表格转成"给模型看"的形状：二维字符串数组。

    跨列单元格的文本放在它起始列，被覆盖的格子给空串 —— 模型只需原样保留形状，
    返回译好的二维数组；span 结构由我们自己按原网格套回去（见 `apply_table_rows`）。
    """
    rows: list[list[str]] = []
    for row in grid.get("rows") or []:
        rows.append([(c or {}).get("text", "") if isinstance(c, dict) else "" for c in row])
    return {"columns": int(grid.get("columns") or 0),
            "head_rows": int(grid.get("head_rows") or 0),
            "rows": rows}


def apply_table_rows(grid: dict, translated: Any) -> dict | None:
    """把模型返回的二维数组套回原来的网格结构（保留 span 与表头行数）。

    行数/列数不一致就返回 None —— 宁可让调用方如实标记未翻译，也不要错位。
    """
    if not isinstance(translated, dict):
        return None
    rows = translated.get("rows")
    src = grid.get("rows") or []
    if not isinstance(rows, list) or len(rows) != len(src):
        return None
    columns = int(grid.get("columns") or 0)
    out_rows: list[list[Any]] = []
    for src_row, new_row in zip(src, rows):
        if not isinstance(new_row, list) or len(new_row) != len(src_row):
            return None
        built: list[Any] = []
        for src_cell, text in zip(src_row, new_row):
            if not isinstance(src_cell, dict):
                built.append(None)
                continue
            t = str(text or "").strip() or src_cell.get("text", "")
            built.append({"text": t, "span": src_cell.get("span", 1)})
        out_rows.append(built)
    return {"columns": columns,
            "head_rows": int(grid.get("head_rows") or 0),
            "rows": out_rows}


def figure_for_prompt(fig: dict) -> dict:
    """图给模型的形状：图题 + 图内文字（可能是空的）+ 上下文（用于写译注）。"""
    return {"caption": fig.get("caption", ""),
            "content": [str(x) for x in (fig.get("content") or [])],
            # 标签（坐标轴刻度、图例）不翻译，但**必须给模型看** ——
            # 它要靠这些写"原文此处是什么图、数值是不是图中标注值"（参照稿就是这么写的）
            "labels": [str(x) for x in (fig.get("labels") or [])]}


def apply_figure(fig: dict, translated: Any) -> dict | None:
    """套回模型返回的图块。

    图内文字允许重新断行（它不是伪代码，位置不代表结构），
    但**图题必须有** —— 没有图题的图块渲染出来是无根之木。
    """
    if not isinstance(translated, dict):
        return None
    caption = str(translated.get("caption") or "").strip()
    if not caption:
        return None
    content = translated.get("content")
    if not isinstance(content, list):
        content = [str(x) for x in (fig.get("content") or [])]
    note = str(translated.get("note") or "").strip()
    if not note:
        # 译注是硬要求（参照稿每幅图都有）：没有就判失败，让这一轮重试
        return None
    return {"caption": caption,
            "content": [str(x).strip() for x in content if str(x).strip()],
            "note": note}


def algorithm_for_prompt(alg: dict) -> dict:
    """伪代码给模型的形状：一行的列表（保序、保行数）。"""
    return {"lines": [str(x) for x in (alg.get("lines") or [])]}


def apply_algorithm_lines(alg: dict, translated: Any) -> dict | None:
    """套回模型返回的伪代码。

    **行数必须一致** —— 伪代码的结构就是"第几行是什么"，
    行数对不上说明模型合并/漏掉了语句，此时宁可判为失败（交给补漏重试），也不要错位。
    """
    if not isinstance(translated, dict):
        return None
    lines = translated.get("lines")
    src = alg.get("lines") or []
    if not isinstance(lines, list) or len(lines) != len(src):
        return None
    out = ["" if x is None else str(x) for x in lines]
    # ⚠️ "原样抄回"要判失败：实测模型会把整块伪代码原封不动返回
    # （规则里只说了"标识符保持原样"，它就把 Input/Output 的说明文字也留成英文了），
    # 结果右栏还是英文，等于没翻。这里只在**整块逐行完全一致**时才判失败 ——
    # 纯符号的算法块本来就该保持一致，不能按"有没有变化"一刀切。
    if out == ["" if x is None else str(x) for x in src] and any(
            re.search(r"[A-Za-z]{3,}\s+[A-Za-z]{3,}", ln or "") for ln in src):
        return None
    return {"lines": out}


def _build_messages(batch: list[dict], system_prompt: str, target_lang: str,
                    glossary: list[list[str]], restore_original: bool) -> list[dict]:
    spec = OUTPUT_SPEC if restore_original else OUTPUT_SPEC_NO_RESTORE
    sys = system_prompt.rstrip()
    # 若用户自定义提示词里没提「任务 A」，补一句，避免模型不返回 en
    if restore_original and "任务 A" not in sys:
        sys += "\n\n另外：每个 item 还要返回 en —— 把该段英文原文重建为规范排版的文本" \
               "（文字不变，只把数学内容写成标准 LaTeX）。"
    if not restore_original:
        sys = re.sub(r"【任务 A】[\s\S]*?(?=【任务 B】)", "", sys)
    sys += spec
    payload: dict[str, Any] = {
        "target_language": target_lang,
        "task": "translate_each_paragraph",
        "need_rebuilt_original": bool(restore_original),
        "paragraphs": [],
    }
    for p in batch:
        item: dict[str, Any] = {"id": p["id"], "kind": p.get("kind", "text")}
        grid = p.get("table")
        if p.get("kind") == "table" and isinstance(grid, dict) and grid.get("rows"):
            # 表格整块下发：给二维数组而不是拍平的文本，列对齐才不会丢
            item["table"] = table_for_prompt(grid)
        fig = p.get("figure")
        if p.get("kind") == "figure" and isinstance(fig, dict) and fig.get("caption"):
            item["figure"] = figure_for_prompt(fig)
        cap = p.get("caption")
        if p.get("kind") == "table" and cap:
            # 表题跟着表格一起翻，渲染时贴在同一格上方（.tcap）
            item["caption"] = cap
        alg = p.get("algorithm")
        if p.get("kind") == "algorithm" and isinstance(alg, dict) and alg.get("lines"):
            # 伪代码按行下发：行号与缩进就是它的结构，绝不能拍平或网格化
            item["algorithm"] = algorithm_for_prompt(alg)
        elif p.get("kind") != "figure":
            item["en"] = p["text"]
        payload["paragraphs"].append(item)
    if restore_original:
        payload["note"] = ("输入里的 en 是从 PDF 抽出的原文，数学内容可能是碎的；"
                           "请按数学含义重建为规范 LaTeX 后返回。")
    if glossary:
        payload["glossary"] = glossary
        payload["glossary_note"] = "以下术语必须沿用既有译法（英文 -> 中文）"
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": "请翻译以下段落并按要求返回 JSON：\n"
                                    + json.dumps(payload, ensure_ascii=False)},
    ]


def _norm_item(item: dict) -> tuple[str, str, list[dict], str]:
    """返回 (id, zh, terms, en_rebuilt)。en_rebuilt 为空串表示模型认为原文无需改动。"""
    pid = str(item.get("id") or item.get("paragraph_id") or "").strip()
    zh = item.get("zh") or item.get("translation") or item.get("text") or ""
    en = item.get("en") or item.get("en_fixed") or item.get("original") or ""
    terms = item.get("terms") or []
    clean_terms = []
    if isinstance(terms, list):
        for t in terms:
            if isinstance(t, dict) and t.get("en"):
                clean_terms.append({"en": str(t["en"]).strip(),
                                    "zh": str(t.get("zh") or "").strip()})
    return pid, str(zh).strip(), clean_terms, str(en).strip()


def _context_window(pool: list[dict], pid: str, radius: int = 1) -> list[dict]:
    """取出 pid 及前后各 radius 段，作严格重试时的参考上下文。

    为什么需要：论文里真有"整段就剩两个 ⟦?⟧"的公式碎片（抽取把一条公式切成了
    好几段，其中一段的可见字符只有括号/运算符）。这种段单独发给模型，它手里
    没有任何线索，只能瞎猜 —— 实测结果是给一对空括号敷衍过去。把前后段落一并
    带上，模型才能看出"这两个占位符是上一条公式的矩阵括号"之类的事实。
    """
    for i, p in enumerate(pool):
        if p.get("id") == pid:
            return pool[max(0, i - radius): i + radius + 1]
    return [p for p in pool if p.get("id") == pid]


def _translate_batch(client: DeepSeekClient, batch: list[dict], settings: dict,
                     glossary: list[list[str]],
                     ctx: list[dict] | None = None) -> tuple[dict, list[dict]]:
    """翻译一批，返回 ({id: {"zh":..,"terms":..,"en":..}}, 新术语)。失败自动二分。

    `ctx` 是"整篇段落"的参考池，仅用于严格重试时取邻居当上下文；不给就和 batch 等价。
    """
    if not batch:
        return {}, []
    restore = bool(settings.get("restore_original", True))
    messages = _build_messages(
        batch, settings.get("system_prompt", ""), settings.get("target_lang", "简体中文"),
        glossary, restore,
    )
    try:
        data = client.chat_json(
            messages,
            model=settings.get("model"),
            temperature=float(settings.get("temperature", 1.0) or 1.0),
        )
    except DeepSeekError:
        if len(batch) == 1:
            raise
        mid = len(batch) // 2
        a, ta = _translate_batch(client, batch[:mid], settings, glossary)
        b, tb = _translate_batch(client, batch[mid:], settings, glossary + ta)
        return {**a, **b}, ta + tb

    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        items = []
    raw_by_id = {p["id"]: p["text"] for p in batch}
    src_by_id = {p["id"]: p for p in batch}
    out: dict[str, Any] = {}
    new_terms: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        pid, zh, terms, en = _norm_item(it)
        if not pid:
            continue
        src = src_by_id.get(pid) or {}
        fig = src.get("figure") if isinstance(src.get("figure"), dict) else None
        if fig and fig.get("caption"):
            zh_fig = apply_figure(fig, it.get("figure"))
            if zh_fig is None:
                continue          # 没有图题 → 当作没返回
            rec_f: dict[str, Any] = {
                "zh": zh_fig["caption"] + ("\n" + "\n".join(zh_fig["content"])
                                            if zh_fig["content"] else ""),
                "terms": terms, "figure": zh_fig,
            }
            if restore:
                rec_f["en"] = fig["caption"] + ("\n" + "\n".join(fig.get("content") or [])
                                                if fig.get("content") else "")
                rec_f["en_figure"] = fig
            out[pid] = rec_f
            new_terms.extend(terms)
            continue
        alg = src.get("algorithm") if isinstance(src.get("algorithm"), dict) else None
        if alg and alg.get("lines"):
            zh_alg = apply_algorithm_lines(alg, it.get("algorithm"))
            if zh_alg is None:
                continue          # 行数不对 → 当作没返回，交给补漏/重试
            rec_a: dict[str, Any] = {"zh": "\n".join(zh_alg["lines"]), "terms": terms,
                                     "algorithm": zh_alg}
            if restore:
                rec_a["en"] = "\n".join(alg["lines"])
                rec_a["en_algorithm"] = alg
            out[pid] = rec_a
            new_terms.extend(terms)
            continue
        grid = src.get("table") if isinstance(src.get("table"), dict) else None
        # 表格段落：zh 由返回的二维数组拼出来，结构套回原网格（保留 span）
        if grid and grid.get("rows"):
            zh_grid = apply_table_rows(grid, it.get("table"))
            if zh_grid is None:
                continue          # 形状不对 → 当作没返回，让补漏/重试逻辑处理
            rec_t: dict[str, Any] = {
                "zh": tables.grid_to_markdown(zh_grid),
                "terms": terms,
                "table": zh_grid,
            }
            cap_zh = str(it.get("caption") or "").strip()
            if src.get("caption"):
                rec_t["caption"] = cap_zh or src["caption"]
            if restore:
                rec_t["en"] = tables.grid_to_markdown(grid)
                rec_t["en_table"] = grid
            out[pid] = rec_t
            new_terms.extend(terms)
            continue
        if not zh:
            continue
        rec: dict[str, Any] = {"zh": zh, "terms": terms}
        # 统一保存 en（即使与原文相同）：这样 translated.json 自描述、重建任务幂等，
        # 前端也只对「与原文不同」的段落打「公式已重建」标记。
        if restore and en:
            rec["en"] = en
        out[pid] = rec
        new_terms.extend(terms)

    # 校验：模型有没有用 `?` / 空括号顶替占位符，或干脆照抄占位符
    #
    # 注意 `_no_repair_retry`：重试时的子调用必须跳过这段校验，否则单段重试会
    # 再次触发"校验→重试→校验"，模型永远修不好时就会无限递归（第一版就是这么挂的）。
    if not settings.get("_no_repair_retry"):
        def _bad(rec: dict[str, Any], src: str) -> list[str]:
            return mathify.find_unrepaired(
                (rec.get("en") or "") + (rec.get("zh") or ""),
                # 源文本里没有占位符的段，输出里的 `?` 是原文自带的，不该算异常
                had_placeholder=mathify.MISSING_GLYPH in src)

        src_of = {p["id"]: (p.get("text") or "") for p in batch}
        bad = [pid for pid, rec in out.items() if _bad(rec, src_of.get(pid, ""))]
        for pid in bad:
            para = next((p for p in batch if p["id"] == pid), None)
            if not para:
                continue
            try:
                strict = dict(settings)
                strict["_no_repair_retry"] = True
                strict["system_prompt"] = (settings.get("system_prompt", "") or "") + STRICT_REPAIR_HINT
                # 带上前后邻居：只有 ⟦?⟧ 的公式碎片必须靠上下文才可能还原
                sub, _t = _translate_batch(client, _context_window(ctx or batch, pid),
                                           strict, glossary, ctx=ctx)
                if sub and pid in sub and not _bad(sub[pid], para.get("text") or ""):
                    out[pid] = sub[pid]
                    continue
            except DeepSeekError:
                pass
            # 重试仍不行：如实标记"待人工确认"，不要让用户以为修好了
            out[pid]["unrepaired"] = True

    missing = [p for p in batch if p["id"] not in out]
    if missing:
        if len(missing) == len(batch):
            if len(batch) > 1:
                mid = len(batch) // 2
                a, ta = _translate_batch(client, batch[:mid], settings, glossary)
                b, tb = _translate_batch(client, batch[mid:], settings, glossary + ta)
                return {**a, **b}, new_terms + ta + tb
            raise DeepSeekError("批次翻译返回为空")
        # 只有部分缺失：逐个补
        for p in missing:
            try:
                sub, _t = _translate_batch(client, [p], settings, glossary)
                out.update(sub)
            except DeepSeekError:
                continue
    # 入库前统一平衡 $ 转义（导出与应用内阅读共用同一份数据）
    for rec in out.values():
        _balance_strings(rec)
    return out, new_terms


def _update_glossary(glossary: list[list[str]], terms: list[dict], limit: int = 80) -> None:
    known = {g[0].lower() for g in glossary}
    for t in terms:
        en, zh = t.get("en", "").strip(), t.get("zh", "").strip()
        if not en or not zh or en.lower() in known:
            continue
        if len(en) > 40:
            continue
        glossary.append([en, zh])
        known.add(en.lower())
    del glossary[limit:]


def run_enrich_rounds(doc_id: str, paras: list[dict], st: dict,
                      failed: list[str], should_stop=None) -> tuple[dict, dict, str]:
    """跑"后两轮"：② 协议结构化、③ 术语统一。返回 (proto_stat, term_stat, proto_err)。

    ⚠️ 抽成公共函数是**必须的**：这两轮原先只写在"有段落要翻"的主路径里，
    而"早就翻完了"的文档走的是"无需翻译"的提前返回分支 —— 于是协议整理
    与术语统一**永远轮不到它们执行**（两个都踩过：术语轮一次、协议轮一次）。
    现有文库绝大多数都是这种"已完成"状态，这个 bug 的杀伤面很大。
    """
    proto_stat: dict = {}
    proto_err = ""
    if st.get("enrich_protocol", True) and not (should_stop and should_stop()):
        try:
            client2 = make_client(st)
            try:
                store.update_meta(doc_id, {"message": "正在整理协议/算法步骤…"})
                proto_stat = finalize_protocols(doc_id, paras, st, client2, failed,
                                                should_stop)
            finally:
                client2.close()
        except Exception as e:  # noqa: BLE001
            # 不能静默 pass：协议整理失败很难察觉（渲染会退回按行渲染，
            # 看着"只是没整理"）。实测就是被吞了异常，查半天才发现整个第二轮没跑。
            proto_err = str(e)[:200]
    term_stat: dict = {}
    if st.get("unify_terms", True):
        try:
            term_stat = unify_terms(doc_id)
        except Exception as e:  # noqa: BLE001
            term_stat = {"error": str(e)[:120]}
    # 术语解释（规格 §7：首次出现给「英文 + 中文 + 简短解释」）。
    # 放在术语统一之后：统一会定下最终译法，解释要解释那个译法。
    if st.get("glossary_notes", True):
        try:
            note_stat = enrich_glossary(doc_id, st, should_stop)
            if note_stat.get("added"):
                term_stat = {**term_stat, "notes": note_stat["added"]}
            if note_stat.get("error"):
                proto_err = proto_err or f"术语解释失败：{note_stat['error']}"
        except Exception as e:  # noqa: BLE001
            proto_err = proto_err or f"术语解释失败：{str(e)[:120]}"
    return proto_stat, term_stat, proto_err


def _enrich_note(proto_stat: dict, term_stat: dict, proto_err: str) -> str:
    """把后两轮的成果拼成一句人话（附在"完成：N/N 段"后面）。"""
    bits = []
    if isinstance(proto_stat, dict) and proto_stat.get("ok"):
        bits.append(f"协议整理 {proto_stat['ok']} 处")
    if isinstance(proto_stat, dict) and proto_stat.get("skipped"):
        bits.append(f"协议未整理 {len(proto_stat['skipped'])} 处（{proto_stat['skipped'][0]}）")
    if proto_err:
        bits.append(f"协议整理失败：{proto_err}")
    if (term_stat or {}).get("replaced"):
        bits.append(f"术语统一 {term_stat['replaced']} 处")
    if (term_stat or {}).get("notes"):
        bits.append(f"术语解释 {term_stat['notes']} 条")
    return ("，" + "，".join(bits)) if bits else ""


def translate_document(
    doc_id: str,
    settings: dict | None = None,
    only_ids: list[str] | None = None,
    force: bool = False,
    progress: Callable[[dict], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """执行翻译，返回统计结果。"""
    from .config import load_settings

    st = dict(load_settings())
    if settings:
        st.update({k: v for k, v in settings.items() if v is not None})

    extracted = store.load_extracted(doc_id)
    paras = extracted.get("paragraphs", [])
    if not paras:
        raise RuntimeError("该文档没有可翻译的段落（抽取结果为空）")
    # 先把结构对不上的旧译文清掉：重抽之后表的列数会变，
    # 旧译文留着会渲出"英文 2 列 / 中文 10 列"的矛盾表（踩过）
    try:
        stale = purge_stale(doc_id)
    except Exception:  # noqa: BLE001
        stale = []
    done = store.load_translations(doc_id)

    def _has_zh(pid: str) -> bool:
        return bool(((done.get(pid) or {}).get("zh") or "").strip())

    if only_ids:
        todo = [p for p in paras if p["id"] in set(only_ids)]
    elif force:
        todo = list(paras)
    else:
        # 注意：记录存在但 zh 为空（例如重抽后合并段落丢失了半段译文）也要重译
        todo = [p for p in paras if p["id"] not in done or not _has_zh(p["id"])]
    # 两条路径（续译 / 已全译）都要用，所以定义在分支之前
    failed: list[str] = []
    if not todo:
        # ⚠️ 已经全译的文档也要跑第三轮（术语统一）——
        # 现有文库绝大多数就是"早就翻完了"的状态，如果这里直接 return，
        # 术语统一永远轮不到它们执行（踩过）。
        # ⚠️ 这里也要跑后两轮（协议整理 + 术语统一）—— 别只跑术语轮：
        # "已全译"是**最常见**的状态（用户点"继续翻译"时文档早就译完了），
        # 只跑一半的话协议整理永远不生效（踩过）
        proto_stat, term_stat, proto_err = run_enrich_rounds(
            doc_id, paras, st, failed, should_stop)
        msg = "无需翻译的段落" + _enrich_note(proto_stat, term_stat, proto_err)
        store.update_meta(doc_id, {"status": "ready", "progress": 1.0, "message": msg})
        return {"newly": 0, "translated": len(done), "total": len(paras),
                "skipped": True, "terms": term_stat, "protocol": proto_stat}

    batches = _chunk(todo, int(st.get("translate_batch_size", 8) or 8),
                     int(st.get("max_chars_per_batch", 3500) or 3500))
    glossary: list[list[str]] = []
    # 用已有译文里的术语预填术语表
    for v in done.values():
        _update_glossary(glossary, (v or {}).get("terms") or [])

    client = make_client(st)
    total = len(todo)
    completed = 0
    store.update_meta(doc_id, {"status": "translating", "progress": len(done) / max(1, len(paras)),
                               "message": f"待翻译 {total} 段", "error": ""})
    try:
        for bi, batch in enumerate(batches, 1):
            if should_stop and should_stop():
                store.update_meta(doc_id, {"status": "partial",
                                           "message": "已手动停止"})
                break
            try:
                mapping, terms = _translate_batch(client, batch, st, glossary, ctx=paras)
                _update_glossary(glossary, terms)
                store.merge_translations(doc_id, mapping)
                completed += len(mapping)
                failed.extend(p["id"] for p in batch if p["id"] not in mapping)
            except Exception as e:  # noqa: BLE001
                failed.extend(p["id"] for p in batch)
                store.update_meta(doc_id, {"error": str(e)[:300],
                                           "message": f"第 {bi}/{len(batches)} 批失败：{str(e)[:120]}"})
            # ⚠️ 数"真的有中文的段数"，不是"译文记录数"：
            # 重抽/重译后记录里可能只有 en（还没翻），按记录数会虚报成 100%，
            # 用户看到"已翻译 401/402 段"却满屏"待翻译"（踩过）。
            translated = sum(1 for v in store.load_translations(doc_id).values()
                             if (v or {}).get("zh"))
            info = {
                "batch": bi, "batches": len(batches), "completed": completed,
                "total": total, "translated": translated,
                "paragraphs": len(paras),
                "progress": translated / max(1, len(paras)),
            }
            store.update_meta(doc_id, {
                "progress": info["progress"],
                "message": f"已翻译 {translated}/{len(paras)} 段",
                "stats": {"paragraphs": len(paras), "translated": translated},
            })
            if progress:
                progress(info)
    finally:
        client.close()

    final_meta = store.get_meta(doc_id)
    total_translated = sum(1 for v in store.load_translations(doc_id).values()
                           if (v or {}).get("zh"))
    status = "ready" if total_translated >= len(paras) else ("partial" if total_translated else "error")
    if failed and total_translated:
        status = "ready" if total_translated >= len(paras) else "partial"
    store.update_meta(doc_id, {
        "status": status, "progress": total_translated / max(1, len(paras)),
        "stats": {"paragraphs": len(paras), "translated": total_translated},
        "message": f"完成：{total_translated}/{len(paras)} 段" + (f"，{len(failed)} 段失败" if failed else ""),
    })
    # —— 第二轮：协议/伪代码结构化（.proto + <ol>）——
    # 只在"有协议块"的文档上多花 1–2 次请求；失败不影响翻译结果（渲染时退回原始行）。
    # —— 后两轮：协议结构化 + 术语统一（两条路径共用同一个实现）——
    proto_stat, term_stat, proto_err = run_enrich_rounds(
        doc_id, paras, st, failed, should_stop)
    # 成果并进最终 message —— 顺序很重要：这两轮原本写在最终 update_meta
    # **之后**，它们的"正在整理…"会盖住"完成：N/N 段"（踩过）
    # ⚠️ 条件里必须包含 skipped：只算 ok/replaced 的话，当"协议一个都没整理成功"
    # 时就**不会**做最终 message 更新，界面会一直显示"正在整理协议/算法步骤…"（踩过）
    if (proto_stat.get("ok") or proto_stat.get("skipped")
            or term_stat.get("replaced") or proto_err):
        store.update_meta(doc_id, {
            "message": f"完成：{total_translated}/{len(paras)} 段"
                       + _enrich_note(proto_stat, term_stat, proto_err)})

    # —— 第三轮：术语统一（确定性替换，不再花 AI 请求）——
    # 分批翻译必然产生"同一术语两种译法"，这里统一成多数派；
    # 同时把术语表落盘，供重译/重抽时沿用。
    term_stat: dict = {}
    if st.get("unify_terms", True):
        try:
            term_stat = unify_terms(doc_id)
        except Exception:  # noqa: BLE001
            term_stat = {}
    if term_stat.get("replaced"):
        store.update_meta(doc_id, {
            "message": f"完成：{total_translated}/{len(paras)} 段"
                       + f"，术语统一 {term_stat['replaced']} 处"})

    # newly：本次真正新翻译的段数；translated：翻译后累计已译段数；total：文档总段数
    return {"newly": completed, "translated": total_translated, "total": len(paras),
            "failed": sorted(set(failed)), "batches": len(batches),
            "terms": term_stat,
            "protocol": proto_stat if isinstance(proto_stat, dict) else {},
            "previous_status": final_meta.get("status")}


# ---------------------------------------------------------------- 术语解释（第二轮附加）

GLOSSARY_NOTE_SYSTEM = """你在为"读论文的人"写术语表。给定一批术语（英文 + 中文译名），
为每条写一句**简短解释**（中文，不超过 40 字），说明它在这类论文里的含义。

要求：
- 解释的是**该领域里的含义**，不是字面翻译；有公认英文缩写可带上；
- 不要写"这是一个术语""指某种方法"这类废话；
- 不要重复术语名本身当解释；
- 只输出 JSON：{"terms": [{"en": "原英文", "note": "一句话解释"}]}
- `en` 必须与输入**逐字一致**，便于对齐；条数与输入一致。"""


def _note_batches(gl: list[list], size: int = 25) -> list[list[list]]:
    """只挑"还没有解释"的术语，按批次切开。"""
    todo = [g for g in gl if len(g) >= 2 and len(g) < 3 or (len(g) >= 3 and not g[2])]
    return [todo[i:i + size] for i in range(0, len(todo), size)]


def enrich_glossary(doc_id: str, settings: dict,
                    should_stop: Callable[[], bool] | None = None) -> dict:
    """给术语表补解释。返回 {"added": n, "error": "..."}。"""
    gl = store.load_glossary(doc_id)
    if not gl:
        return {"added": 0}
    batches = _note_batches(gl)
    if not batches:
        return {"added": 0}          # 都已经有解释 → 幂等跳过
    by_en = {str(g[0]).lower(): g for g in gl}
    added, err = 0, ""
    client = make_client(settings)
    try:
        for batch in batches:
            if should_stop and should_stop():
                break
            payload = [{"en": g[0], "zh": g[1]} for g in batch]
            messages = [
                {"role": "system", "content": GLOSSARY_NOTE_SYSTEM},
                {"role": "user", "content": "请为这些术语写解释并返回 JSON：\n"
                                            + json.dumps({"terms": payload},
                                                         ensure_ascii=False)},
            ]
            try:
                data = client.chat_json(messages, model=settings.get("model"),
                                        temperature=float(settings.get("temperature", 1.0) or 1.0))
            except Exception as e:  # noqa: BLE001
                err = str(e)[:120]
                break
            for item in (data or {}).get("terms") or []:
                if not isinstance(item, dict):
                    continue
                en = str(item.get("en") or "").strip().lower()
                note = " ".join(str(item.get("note") or "").split())
                if not en or not note or len(note) > 80:
                    continue
                row = by_en.get(en)
                if not row or len(row) >= 3:
                    continue
                # 校验：解释不能是废话，且要跟术语对得上
                if note in ("这是一个术语", "术语", "-", "无"):
                    continue
                row.append(note)
                added += 1
    finally:
        client.close()
    if added:
        store.save_glossary(doc_id, gl)
    return {"added": added, "error": err}


# ---------------------------------------------------------------- 术语统一（第三轮）

def _is_stale(para: dict, rec: dict) -> bool:
    """这条译文还算不算数。

    ⚠️ 重抽之后**结构会变**（符号表从 10 列修成 2 列、伪代码行数变了），
    而旧译文还挂在同一个 id 上 —— 不清理就会渲染出"英文 2 列 / 中文 10 列"
    这种自相矛盾的表（实测用户文档里就有：p0063 en 2 列 vs zh 10 列）。
    判据：结构化块比形状，文字块比来源文本。
    """
    kind = para.get("kind")
    if kind == "table":
        en = para.get("table") or {}
        zh = rec.get("table")
        if not isinstance(zh, dict):
            return False
        if zh.get("columns") != en.get("columns"):
            return True
        if len(zh.get("rows") or []) != len(en.get("rows") or []):
            return True
        return False
    if kind == "algorithm":
        en = (para.get("algorithm") or {}).get("lines") or []
        zh = (rec.get("algorithm") or {}).get("lines") or []
        if zh and len(zh) != len(en):
            return True
        # ⚠️ "半翻"也算失效：实测模型只翻了 `//` 注释，
        # `Input:`/`Output:` 的说明文字整行留英文（规格里点名的就是这条）。
        # 判据：**存在"仍是英文散文"的行** —— 即该行与源行一模一样、
        # 且含 4 个以上连续英文词。纯符号/关键字行天然会一致，不会被误判。
        for i, src_line in enumerate(zh and en or []):
            if i >= len(zh):
                break
            if en[i].strip() != zh[i].strip():
                continue
            if re.search(r"(?:[A-Za-z][A-Za-z'\-]*\s+){3,}[A-Za-z][A-Za-z'\-]*",
                         en[i] or ""):
                return True
        return False
    if kind == "figure":
        en = (para.get("figure") or {}).get("content") or []
        zh = (rec.get("figure") or {}).get("content") or []
        # 源图里本来没文字、译文却凭空有 / 反之，都说明是旧结构
        if bool(zh) != bool(en):
            return True
        # 缺译注也算失效：译注是硬要求，缺了要补（参照稿每幅图都有一条）
        if not (rec.get("figure") or {}).get("note"):
            return True
        return False
    return False


def purge_stale(doc_id: str) -> list[str]:
    """清掉"结构已经对不上"的旧译文，让它们重新走翻译。返回被清掉的 id。"""
    ex = store.load_extracted(doc_id)
    done = store.load_translations(doc_id)
    drop = [p["id"] for p in ex.get("paragraphs", [])
            if p["id"] in done and _is_stale(p, done[p["id"]] or {})]
    if drop:
        for pid in drop:
            done.pop(pid, None)
        store.save_translations(doc_id, done)
    return drop


def collect_term_variants(done: dict) -> dict[str, dict[str, int]]:
    """汇总全篇术语 → {英文小写: {中文译法: 出现次数}}。"""
    out: dict[str, dict[str, int]] = {}
    for rec in done.values():
        for t in (rec or {}).get("terms") or []:
            en = str(t.get("en") or "").strip().lower()
            zh = str(t.get("zh") or "").strip()
            if not en or not zh:
                continue
            out.setdefault(en, {})
            out[en][zh] = out[en].get(zh, 0) + 1
    return out


def build_term_map(variants: dict[str, dict[str, int]]) -> tuple[dict[str, str], list[str]]:
    """决定"少数派 → 多数派"的替换表，并挡掉会误伤长词的替换。

    返回 (替换表, 跳过的说明)。多数派按出现次数取最大，平票取更长的那个
    （更具体通常意味着更准确）。
    """
    chosen: list[str] = []
    plan: dict[str, str] = {}
    for en, zh_map in variants.items():
        if len(zh_map) < 2:
            continue
        best = sorted(zh_map.items(), key=lambda kv: (kv[1], len(kv[0])), reverse=True)[0][0]
        chosen.append(best)
        for zh in zh_map:
            if zh != best and len(zh) >= 2:
                plan[zh] = best
    skipped: list[str] = []
    fixed_plan: dict[str, str] = {}
    for src, dst in plan.items():
        # 若 src 是**其他**术语选定译法的子串，替换会毁掉那个更长的术语
        if any(src != c and src in c for c in chosen):
            skipped.append(f"{src}→{dst}（是更长术语的一部分）")
            continue
        fixed_plan[src] = dst
    return fixed_plan, skipped


def _balance_strings(obj) -> None:
    """把记录里所有字符串的"落单货币 $ "补成 \\$（就地改）。

    ⚠️ 必须**递归覆盖所有字段**：只处理 `zh` 的话，表格单元格 / 图题 /
    伪代码行 / 协议步骤里的 `$` 仍会破坏 `$` 成对，公式就渲染不出来
    （规格 §6 的自检就是查这个）。
    """
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                obj[i] = mathify.escape_math_angles(
                    mathify.repair_bare_commands(mathify.balance_dollars(v)))
            else:
                _balance_strings(v)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                # 两步都做：平衡 $ 转义 + 修掉缺参数的 LaTeX 命令
                # （`\sqrt` 少参数会让 MathJax 把 "Missing argument for sqrt"
                #  当文字渲染进正文 —— 用户截图报的就是这个）
                obj[k] = mathify.escape_math_angles(
                    mathify.repair_bare_commands(mathify.balance_dollars(v)))
            else:
                _balance_strings(v)


def _replace_in(obj, plan: dict[str, str]) -> int:
    """递归替换记录里所有字符串字段。返回替换处数。"""
    n = 0
    if isinstance(obj, str):
        return 0
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                new = v
                for a, b in plan.items():
                    if a in new:
                        n += new.count(a)
                        new = new.replace(a, b)
                obj[i] = new
            else:
                n += _replace_in(v, plan)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                new = v
                for a, b in plan.items():
                    if a in new:
                        n += new.count(a)
                        new = new.replace(a, b)
                obj[k] = new
            else:
                n += _replace_in(v, plan)
    return n


def unify_terms(doc_id: str) -> dict:
    """第三轮：把同一术语的多种译法统一成多数派。

    只做字符串层替换，**不再请求模型** —— 这一步的正确性完全可验证，
    交给模型反而有改写风险。改动量会写进 meta，便于核对。
    """
    done = store.load_translations(doc_id)
    variants = collect_term_variants(done)
    plan, skipped = build_term_map(variants)
    conflicts = {en: z for en, z in variants.items() if len(z) > 1}
    stat = {"conflicts": len(conflicts), "replaced": 0, "skipped": skipped,
            "terms": [[en, sorted(z, key=lambda k: -z[k])[0]] for en, z in conflicts.items()]}
    if plan:
        total = 0
        for pid, rec in done.items():
            if not isinstance(rec, dict):
                continue
            total += _replace_in(rec, plan)
        if total:
            store.save_translations(doc_id, done)
        stat["replaced"] = total
    # 术语表落盘：重译/重抽时沿用，减少再次出现不一致
    # 术语表落盘时**保留已有解释**（第 3 个元素）—— 直接重建会把解释抹掉
    old_notes = {str(g[0]).lower(): g[2] for g in store.load_glossary(doc_id)
                 if len(g) >= 3 and g[2]}
    glossary: list[list[str]] = []
    for en_map, zh_map in sorted(variants.items()):
        best = sorted(zh_map.items(), key=lambda kv: (kv[1], len(kv[0])), reverse=True)[0][0]
        row = [en_map, best]
        if old_notes.get(en_map):
            row.append(old_notes[en_map])
        glossary.append(row)
    if glossary:
        try:
            store.save_glossary(doc_id, glossary[:120])
        except Exception:  # noqa: BLE001
            pass
    return stat


# ---------------------------------------------------------------- 术语统一（第三轮）

def collect_term_variants(done: dict) -> dict[str, dict[str, int]]:
    """汇总全篇术语 → {英文小写: {中文译法: 出现次数}}。"""
    out: dict[str, dict[str, int]] = {}
    for rec in done.values():
        for t in (rec or {}).get("terms") or []:
            en = str(t.get("en") or "").strip().lower()
            zh = str(t.get("zh") or "").strip()
            if not en or not zh:
                continue
            out.setdefault(en, {})
            out[en][zh] = out[en].get(zh, 0) + 1
    return out


def build_term_map(variants: dict[str, dict[str, int]]) -> tuple[dict[str, str], list[str]]:
    """决定"少数派 → 多数派"的替换表，并挡掉会误伤长词的替换。

    返回 (替换表, 跳过的说明)。多数派按出现次数取最大，平票取更长的那个
    （更具体通常意味着更准确）。
    """
    chosen: list[str] = []
    plan: dict[str, str] = {}
    for en, zh_map in variants.items():
        if len(zh_map) < 2:
            continue
        best = sorted(zh_map.items(), key=lambda kv: (kv[1], len(kv[0])), reverse=True)[0][0]
        chosen.append(best)
        for zh in zh_map:
            if zh != best and len(zh) >= 2:
                plan[zh] = best
    skipped: list[str] = []
    fixed_plan: dict[str, str] = {}
    for src, dst in plan.items():
        # 若 src 是**其他**术语选定译法的子串，替换会毁掉那个更长的术语
        if any(src != c and src in c for c in chosen):
            skipped.append(f"{src}→{dst}（是更长术语的一部分）")
            continue
        fixed_plan[src] = dst
    return fixed_plan, skipped


def _replace_in(obj, plan: dict[str, str]) -> int:
    """递归替换记录里所有字符串字段。返回替换处数。"""
    n = 0
    if isinstance(obj, str):
        return 0
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                new = v
                for a, b in plan.items():
                    if a in new:
                        n += new.count(a)
                        new = new.replace(a, b)
                obj[i] = new
            else:
                n += _replace_in(v, plan)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                new = v
                for a, b in plan.items():
                    if a in new:
                        n += new.count(a)
                        new = new.replace(a, b)
                obj[k] = new
            else:
                n += _replace_in(v, plan)
    return n


def unify_terms(doc_id: str) -> dict:
    """第三轮：把同一术语的多种译法统一成多数派。

    只做字符串层替换，**不再请求模型** —— 这一步的正确性完全可验证，
    交给模型反而有改写风险。改动量会写进 meta，便于核对。
    """
    done = store.load_translations(doc_id)
    variants = collect_term_variants(done)
    plan, skipped = build_term_map(variants)
    conflicts = {en: z for en, z in variants.items() if len(z) > 1}
    stat = {"conflicts": len(conflicts), "replaced": 0, "skipped": skipped,
            "terms": [[en, sorted(z, key=lambda k: -z[k])[0]] for en, z in conflicts.items()]}
    if plan:
        total = 0
        for pid, rec in done.items():
            if not isinstance(rec, dict):
                continue
            total += _replace_in(rec, plan)
        if total:
            store.save_translations(doc_id, done)
        stat["replaced"] = total
    # 术语表落盘：重译/重抽时沿用，减少再次出现不一致
    # ⚠️ 落盘时**保留已有解释**（第 3 个元素）：术语统一每次都重建术语表，
    # 不保留的话，刚花 API 写好的"一句话解释"会被抹掉（实测确认过）。
    old_notes = {str(g[0]).lower(): g[2] for g in store.load_glossary(doc_id)
                 if len(g) >= 3 and g[2]}
    glossary: list[list[str]] = []
    for en_map, zh_map in sorted(variants.items()):
        best = sorted(zh_map.items(), key=lambda kv: (kv[1], len(kv[0])), reverse=True)[0][0]
        row = [en_map, best]
        if old_notes.get(en_map):
            row.append(old_notes[en_map])
        glossary.append(row)
    if glossary:
        try:
            store.save_glossary(doc_id, glossary[:120])
        except Exception:  # noqa: BLE001
            pass
    return stat


# ---------------------------------------------------------------- 协议结构化（第二轮）

# 第二轮：把已经翻好的伪代码/协议行整理成「标题 + 输入输出 + 编号步骤」。
# 为什么值得单独一轮：这一步是**重排与归纳**，和"逐行翻译"是两种不同的任务，
# 混在一次请求里会让模型两头都做不好（实测参照成品稿的做法是分两步）。
PROTOCOL_SYSTEM = """你是排版助手。下面给你一段**已经翻译好的**协议/算法文本，
请把它整理成结构化的步骤。

【硬约束（违反即作废）】
- **不得增删任何内容**：每一行里的信息都必须出现在 title / setup / steps 之一；
- 数字、变量名、算法名、函数名、关键字（for/if/return/←）**原样保留**，
  不要改写、不要"顺手优化"、不要补充原文没有的结论；
- 去掉手工行号（`1.` `2.` `21`）—— 改用列表序号；但行号里的数字若本身是内容
  （如 `4 n ← ef/efspec` 中的 4 是步号）则不保留；
- 子步骤（`(a)` `(b)`、`foreach` 内层）放进对应步骤的 `subs`。

【输出格式】只输出 JSON：
{"protocol": {"title": "算法/协议标题（含编号与签名）",
              "setup": ["输入：…", "输出：…"],
              "steps": [{"text": "步骤正文", "subs": ["子步骤一", "子步骤二"]}]}}
- `setup` 放 Input/Output 这类前置说明，没有就给空数组；
- `steps` 每一步一条主语句，`subs` 没有就给空数组；
- 不要输出解释、不要 Markdown 代码块。"""


# ---------------------------------------------------------------- 2. 哪些块要跑


def _protocol_en_source(para: dict) -> tuple[str, list[str]]:
    """取协议块**原文侧**的标题与行（渲染英文栏用）。

    英文栏必须放原文：协议的结构化步骤是中文的，直接摆到英文栏会串味
    （实测英文栏里出现过 `图 4：Compass 的安全博弈`）。
    """
    kind = para.get("kind")
    if kind == "algorithm":
        alg = para.get("algorithm") or {}
        lines = [str(x) for x in (alg.get("lines") or [])]
        return (lines[0].strip() if lines else ""), lines
    if kind == "figure":
        fig = para.get("figure") or {}
        return (str(fig.get("caption") or "").strip(),
                [str(x) for x in (fig.get("content") or [])])
    return "", []

def _protocol_lines(para: dict, rec: dict) -> tuple[str, list[str]] | None:
    """挑出需要结构化的块，返回 (原始首行/标题, 待整理的行)。

    两类：
      * `kind == "algorithm"`：伪代码/算法块
      * `kind == "figure"` 且图内文字**本身就是编号步骤**（如"安全游戏"示意图）——
        参照成品稿把 Figure 4 这种渲成了协议块而不是图框。
    """
    kind = para.get("kind")
    if kind == "algorithm":
        alg = (rec.get("algorithm") or para.get("algorithm") or {})
        lines = [str(x) for x in (alg.get("lines") or [])]
        if len(lines) >= 3:
            return (lines[0].strip(), lines)
        return None
    if kind == "figure":
        # ⚠️ 判据用**原文**的行号（`para.figure.content`）而不是译文：
        # 译文可能被加了前缀或改写，行号不再在行首，判断就失效了（踩过）。
        src_lines = [str(x) for x in ((para.get("figure") or {}).get("content") or [])]
        numbered = sum(1 for x in src_lines
                       if re.match(r"^\s*\(?\d+[\.\)]\s+\S", x))
        # 判据：**有编号项**就值得当协议整理（不要求过半都是编号行 ——
        # 安全游戏那种是"3 条主编号 + (a)(b)(c) 子项 + 折行续行"，
        # 按"过半"算会漏掉，实测就是这么漏的）
        if len(src_lines) >= 4 and numbered >= 2:
            fig = rec.get("figure") or {}
            lines = [str(x) for x in (fig.get("content") or src_lines)]
            title = str(fig.get("caption") or
                        (para.get("figure") or {}).get("caption") or "").strip()
            return (title, lines) if lines else None
    return None


# ---------------------------------------------------------------- 3. 校验

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]{1,}|\d+(?:\.\d+)?")


_STEP_NO_RE = re.compile(r"^\s*\(?\d+[\.\)]?\s+")


def _tokens(texts: list[str], drop_step_numbers: bool = False) -> set[str]:
    """抽关键 token（标识符 / 数字）。

    `drop_step_numbers=True` 用于**源**行：开头的 `1`、`21` 是排版用的手工行号，
    协议规范化时本来就该去掉 —— 把它们算成"内容"会让校验永远不通过（踩过）。
    单字符（`i`、`C`、`W`）噪声太大也不参与。
    """
    out: set[str] = set()
    for t in texts:
        text = _STEP_NO_RE.sub("", t or "") if drop_step_numbers else (t or "")
        for m in _TOKEN_RE.findall(text):
            if len(m) >= 2:
                out.add(m.lower())
    return out


def protocol_keeps_content(lines: list[str], proto: dict | None,
                           min_cover: float = 0.45,
                           min_steps: int | None = None) -> bool:
    """协议结构化结果的**验收闸门**。

    三个判据（都是针对真实失败模式设计的）：
      ① **数字必须全在** —— 下标、常量、单位、坐标值丢一个，协议就读错了；
      ② **步骤数不能大幅缩水** —— 模型最常见的偷懒是"把 17 步压成 3 句"，
         这种输出读起来通顺，但把协议毁了；
      ③ token 覆盖率兜底（默认 0.45，故意定得很低）—— 只抓"整段消失"。
         ⚠️ 不能要求 100%：第二轮输入里夹着英文残留词（`extract`、`nearest`），
         模型把它们译成中文是正确行为，按原样字符比对会误判（踩过）。
    """
    if not isinstance(proto, dict):
        return False
    src = _tokens(lines, drop_step_numbers=True)
    if not src:
        return False

    out_texts: list[str] = [str(proto.get("title") or "")]
    setup = proto.get("setup")
    if isinstance(setup, list):
        out_texts += [str(x) for x in setup]
    steps = proto.get("steps")
    if not isinstance(steps, list) or not steps:
        return False
    for st in steps:
        if isinstance(st, dict):
            out_texts.append(str(st.get("text") or ""))
            subs = st.get("subs")
            if isinstance(subs, list):
                out_texts += [str(x) for x in subs]
        else:
            out_texts.append(str(st))

    # ① 数字
    num_re = re.compile(r"\d+(?:\.\d+)?")
    src_nums = {m for t in lines for m in num_re.findall(_STEP_NO_RE.sub("", t or ""))}
    out_nums = {m for t in out_texts for m in num_re.findall(t or "")}
    if not src_nums <= out_nums:
        return False

    # ② 步骤数
    if min_steps is not None and len(steps) < min_steps:
        return False

    # ③ 覆盖率
    out = _tokens(out_texts)
    hit = sum(1 for t in src if t in out)
    return hit / len(src) >= min_cover


def norm_protocol(proto: dict) -> dict | None:
    """把模型输出规整成固定形状（容错：steps 里给裸字符串也接受）。"""
    steps: list[dict] = []
    for st in (proto.get("steps") or []):
        if isinstance(st, dict):
            text = str(st.get("text") or "").strip()
            subs = [str(x).strip() for x in (st.get("subs") or []) if str(x).strip()]
        else:
            text, subs = str(st).strip(), []
        if text or subs:
            steps.append({"text": text, "subs": subs})
    if not steps:
        return None
    return {"title": str(proto.get("title") or "").strip(),
            "setup": [str(x).strip() for x in (proto.get("setup") or []) if str(x).strip()],
            "steps": steps}


def enrich_protocol(client, title: str, lines: list[str], settings: dict,
                    min_steps: int = 2) -> tuple[dict | None, str]:
    """跑第二轮：把协议整理成结构化步骤。

    返回 `(协议, 失败原因)` —— 失败时协议为 None、原因是给人看的一句话。
    **必须把原因带出来**：这一轮失败是"静默降级"（渲染退回按行渲染，看起来
    只是没整理），不说明原因的话根本查不出是模型没返回还是被闸门拦了（踩过）。
    """
    payload = {"title": title, "lines": lines}
    messages = [
        {"role": "system", "content": PROTOCOL_SYSTEM},
        {"role": "user", "content": "请整理这段协议并按要求返回 JSON：\n"
                                    + json.dumps(payload, ensure_ascii=False)},
    ]
    try:
        data = client.chat_json(messages, model=settings.get("model"),
                                temperature=float(settings.get("temperature", 1.0) or 1.0))
    except Exception as e:  # noqa: BLE001
        return None, f"调用失败：{str(e)[:120]}"
    proto = data.get("protocol") if isinstance(data, dict) else None
    if not isinstance(proto, dict):
        return None, "模型没返回 protocol 字段"
    norm = norm_protocol(proto)
    if norm is None:
        return None, "返回里没有可用的 steps"
    # 步骤数下限由调用方给：**算法块**一行一条语句 → 按行数一半要求；
    # 而"图"（安全游戏那种）的物理行里有大量折行的续行，逻辑步骤远少于行数
    # （实测 15 行其实只有 4 条规则），按行数要求会把**正确结果**误杀（踩过）。
    if not protocol_keeps_content(lines, norm, min_steps=min_steps):
        # 不采用，但**如实说明为什么**（否则整轮失败悄无声息，很难查）
        return None, (f"验收闸门拦下：返回 {len(norm['steps'])} 步 / 源 {len(lines)} 行"
                      f"（要求 ≥{min_steps} 步且数字齐全）")
    return norm, ""


def finalize_protocols(doc_id: str, paras: list[dict], st: dict,
                       client, failed: list[str], should_stop=None) -> dict:
    """对所有需要结构化的块跑第二轮，把结果并回 translated.json。

    返回 {"ok": 成功数, "skipped": [没能结构化但**不影响交付**的 id]}。
    """
    done = store.load_translations(doc_id)
    n = 0
    skipped: list[str] = []
    for para in paras:
        if should_stop and should_stop():
            break
        rec = done.get(para["id"]) or {}
        if rec.get("protocol"):
            # 已有协议但缺英文侧（老数据）：就地补上，不花 API
            pr = rec["protocol"]
            if not pr.get("title_en"):
                en_title, en_lines = _protocol_en_source(para)
                pr["title_en"] = en_title
                pr["lines_en"] = en_lines
                store.merge_translations(doc_id, {para["id"]: {**rec, "protocol": pr}})
            continue
        picked = _protocol_lines(para, rec)
        if not picked:
            continue
        title, lines = picked
        if not lines:
            continue
        # 只有算法块才按"行数一半"要求步骤数（一行一条语句）；
        # 图内的散文行会折行，逻辑步骤天然少于行数
        floor = max(2, len(lines) // 2) if para.get("kind") == "algorithm" else 2
        proto, why = enrich_protocol(client, title, lines, st, min_steps=floor)
        if proto:
            en_title, en_lines = _protocol_en_source(para)
            proto["title_en"] = en_title
            proto["lines_en"] = en_lines
            store.merge_translations(doc_id, {para["id"]: {**rec, "protocol": proto}})
            n += 1
        else:
            # 结构化失败**不致命**（渲染时退回按行渲染），所以**不写进 failed**——
            # failed 是给用户看"哪些段没翻出来"的，混进来会误导（踩过：
            # 报告"失败 2 段"其实那两段翻译是好的，只是协议没整理成功）
            skipped.append(f"{para['id']}：{why}")
    return {"ok": n, "skipped": skipped}


# ---------------------------------------------------------------- 问答

def build_ask_context(doc_id: str, para_id: str, selection: str = "",
                      span: int = 2) -> str:
    extracted = store.load_extracted(doc_id)
    paras = extracted.get("paragraphs", [])
    translations = store.load_translations(doc_id)
    index = {p["id"]: i for i, p in enumerate(paras)}
    i = index.get(para_id)
    lines: list[str] = []
    if i is None:
        if selection:
            lines.append(f"【读者选中的片段】{selection}")
        return "\n".join(lines)
    lo, hi = max(0, i - span), min(len(paras), i + span + 1)
    for j in range(lo, hi):
        p = paras[j]
        tag = " <<< 读者当前所在的段落" if j == i else ""
        lines.append(f"【{p['id']}｜第{p['page']}页｜{p['kind']}】{tag}")
        tr = translations.get(p["id"]) or {}
        # 优先用「重建原文」喂给模型：公式是规范 LaTeX，比 PDF 直抽的碎片更好推理
        lines.append(f"EN: {tr.get('en') or p['text']}")
        if tr.get("zh"):
            lines.append(f"ZH: {tr['zh']}")
    if selection:
        lines.append("")
        lines.append(f"【读者选中的片段】{selection}")
    return "\n".join(lines)


def ask_stream(doc_id: str, question: str, para_id: str = "", selection: str = "",
               settings: dict | None = None, history: list[dict] | None = None) -> Iterator[str]:
    """流式回答关于某段/某片段的提问。"""
    from .config import load_settings

    st = dict(load_settings())
    if settings:
        st.update({k: v for k, v in settings.items() if v is not None})
    client = make_client(st)
    try:
        context = build_ask_context(doc_id, para_id, selection,
                                    int(st.get("context_paragraphs", 2) or 2))
        sys_prompt = st.get("ask_system_prompt", "") or ""
        messages: list[dict] = [{"role": "system", "content": sys_prompt}]
        for h in (history or [])[-6:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                messages.append({"role": h["role"], "content": str(h["content"])[:4000]})
        user = (f"以下是论文上下文（左列为英文原文，右列为已有中文译文）：\n\n{context}\n\n"
                f"我的问题：{question}\n\n"
                "请基于以上上下文作答，必要时引用段落编号。")
        messages.append({"role": "user", "content": user})
        model = st.get("ask_model") or st.get("model")
        yield from client.stream(messages, model=model,
                                 temperature=float(st.get("temperature", 1.0) or 1.0))
    finally:
        client.close()


# ------------------------------------------------- 只重建左栏（不重译）

def _restore_batch(client: DeepSeekClient, batch: list[dict], settings: dict) -> dict:
    """让模型只返回重建后的原文（en），不翻译——输出短、便宜。

    返回值包含**所有**被处理的段落 id：值为重建文本，值为空串表示"原文已规范、无需改动"。
    未被返回的 id 才算失败（响应缺失）。
    """
    if not batch:
        return {}
    messages = [
        {"role": "system", "content": (settings.get("system_prompt", "") or "").split("【任务 B】")[0]
         .rstrip() + OUTPUT_SPEC_RESTORE},
        {"role": "user", "content": "请重建以下段落的原文并按要求返回 JSON：\n"
         + json.dumps({"task": "restore_original",
                       "paragraphs": [{"id": p["id"], "en": p["text"]} for p in batch]},
                      ensure_ascii=False)},
    ]
    try:
        data = client.chat_json(messages, model=settings.get("model"),
                                temperature=float(settings.get("temperature", 1.0) or 1.0))
    except DeepSeekError:
        if len(batch) == 1:
            raise
        mid = len(batch) // 2
        return {**_restore_batch(client, batch[:mid], settings),
                **_restore_batch(client, batch[mid:], settings)}
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        items = []
    raw_by_id = {p["id"]: p["text"] for p in batch}
    out: dict[str, str] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        pid = str(it.get("id") or "").strip()
        en = str(it.get("en") or it.get("text") or "").strip()
        if pid in raw_by_id:
            out[pid] = en or raw_by_id[pid]
    missing = [p for p in batch if p["id"] not in out]
    if missing and len(missing) == len(batch) and len(batch) > 1:
        mid = len(batch) // 2
        return {**_restore_batch(client, batch[:mid], settings),
                **_restore_batch(client, batch[mid:], settings)}
    return out


def restore_document(doc_id: str, settings: dict | None = None,
                     only_ids: list[str] | None = None, force: bool = False,
                     progress: Callable[[dict], None] | None = None,
                     should_stop: Callable[[], bool] | None = None) -> dict:
    """只重建左栏原文，保留已有译文。用于"译文已翻好、只想修公式"的场景。"""
    from .config import load_settings

    st = dict(load_settings())
    if settings:
        st.update({k: v for k, v in settings.items() if v is not None})
    extracted = store.load_extracted(doc_id)
    paras = extracted.get("paragraphs", [])
    if not paras:
        raise RuntimeError("该文档没有可重建的段落")
    done = store.load_translations(doc_id)

    if only_ids:
        todo = [p for p in paras if p["id"] in set(only_ids)]
    elif force:
        todo = list(paras)
    else:
        # 已有 en 的不再重建（除非 force）
        todo = [p for p in paras if not (done.get(p["id"]) or {}).get("en")]
    if not todo:
        store.update_meta(doc_id, {"message": "左栏无需重建"})
        return {"newly": 0, "total": len(paras), "skipped": True}

    batches = _chunk(todo, int(st.get("translate_batch_size", 8) or 8),
                     int(st.get("max_chars_per_batch", 3500) or 3500))
    client = make_client(st)
    completed = 0
    unchanged = 0
    failed: list[str] = []
    store.update_meta(doc_id, {"status": "restoring",
                               "message": f"重建左栏：待处理 {len(todo)} 段"})
    try:
        for bi, batch in enumerate(batches, 1):
            if should_stop and should_stop():
                store.update_meta(doc_id, {"status": "partial", "message": "已手动停止"})
                break
            try:
                got = _restore_batch(client, batch, st)
                raw_map = {p["id"]: p["text"] for p in batch}
                patch = {}
                for pid, en in got.items():
                    cur = dict(store.load_translations(doc_id).get(pid) or {})
                    cur["en"] = en
                    cur.setdefault("zh", "")
                    cur.setdefault("terms", [])
                    patch[pid] = cur
                    if _norm_ws(en) == _norm_ws(raw_map.get(pid, "")):
                        unchanged += 1
                if patch:
                    store.merge_translations(doc_id, patch)
                    completed += len(patch)
                failed.extend(p["id"] for p in batch if p["id"] not in got)
            except Exception as e:  # noqa: BLE001
                failed.extend(p["id"] for p in batch)
                store.update_meta(doc_id, {"error": str(e)[:300]})
            if progress:
                progress({"batch": bi, "batches": len(batches),
                          "completed": completed, "total": len(todo)})
            store.update_meta(doc_id, {"message": f"重建左栏 {completed}/{len(todo)} 段"})
    finally:
        client.close()

        total_en = sum(1 for v in store.load_translations(doc_id).values() if (v or {}).get("en"))
        tr_total = sum(1 for v in store.load_translations(doc_id).values() if (v or {}).get("zh"))
        status = "ready" if tr_total >= len(paras) else ("partial" if tr_total else "extracted")
        store.update_meta(doc_id, {"status": status,
                                   "message": f"左栏已重建 {completed - unchanged} 段"
                                              + (f"（{unchanged} 段原文已规范）" if unchanged else "")
                                              + (f"，{len(failed)} 段失败" if failed else "")})
        return {"newly": completed - unchanged, "unchanged": unchanged,
                "covered": total_en, "failed": sorted(set(failed)), "total": len(paras)}


def retranslate_paragraph(doc_id: str, para_id: str, instruction: str = "",
                          settings: dict | None = None) -> dict:
    from .config import load_settings

    st = dict(load_settings())
    if settings:
        st.update({k: v for k, v in settings.items() if v is not None})
    extracted = store.load_extracted(doc_id)
    para = next((p for p in extracted.get("paragraphs", []) if p["id"] == para_id), None)
    if not para:
        raise RuntimeError(f"未找到段落 {para_id}")
    client = make_client(st)
    try:
        extra = f"\n\n本段额外要求（优先满足）：{instruction}" if instruction else ""
        messages = _build_messages([para], st.get("system_prompt", ""),
                                   st.get("target_lang", "简体中文"), [],
                                   bool(st.get("restore_original", True)))
        messages[1]["content"] += extra
        data = client.chat_json(messages, model=st.get("model"),
                                temperature=float(st.get("temperature", 1.0) or 1.0))
        items = data.get("items") if isinstance(data, dict) else data
        if not items:
            raise DeepSeekError("重译返回为空")
        pid, zh, terms, en = _norm_item(items[0])
        rec: dict[str, Any] = {"zh": zh, "terms": terms}
        if en and _norm_ws(en) != _norm_ws(para["text"]):
            rec["en"] = en
        store.merge_translations(doc_id, {para_id: rec})
        return {"id": para_id, **rec}
    finally:
        client.close()
