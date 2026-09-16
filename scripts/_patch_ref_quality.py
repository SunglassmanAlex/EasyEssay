"""按参照稿改两处：① 每幅图都要一条译注；② 分页标记行。用完即删。

参照稿的做法（`E:/Workbench/store/密码学论文阅读/…html`）：
* 每一幅图都配一条 `.note` 说明"原文此处是什么图"，例如
  `〔译注：原文此处为四幅并排的遍历过程示意（a–d），图中节点编号 1–7 表示迭代轮次。〕`
  —— 不只是"图内无文字"时才写。我原来只在无文字时写 → 导出里 0 条（参照稿 19 条）。
* 页与页之间插 `.row.pbreak`，**左右两栏都有**（保证中英分界线连续）：
  `<div class="row pbreak" data-pg="2→3"><div class="en">— page 2 ends · page 3 begins —</div>
   <div class="zh">—— 原文第 2 页结束 · 第 3 页开始 ——</div></div>`
"""
from pathlib import Path

pp = Path("app/translate.py")
s = pp.read_text(encoding="utf-8")

# ---------------- ① 图：每幅都必须有译注 ----------------
OLD_RULE = """- **kind 为 `figure` 的段落是图**：输入给的是 `figure.caption`（图题）与
  `figure.content`（图内文字，可能为空数组）。返回 "
  "`\"figure\": {\"caption\": \"图题译文\", \"content\": [...], \"note\": \"...\"}`；
  **`caption` 必须有**（`Figure 1:` 译成 `图 1：`）；
  `content` 是图里的文字（图例、示意文字、示意框图），逐条翻译，允许重新断行；
  **图里没有可读文字时（`content` 为空）必须写 `note`** —— 用一条译注说明
  “这里原本是一幅什么图、说明了什么”，形如
  `〔译注：原文此处为一幅 HNSW 多层图示意，含图例六项：已访问节点、…〕`。
  依据只能是图题与上下文，**不要编造图里没有的数字或结论**；
  图内文字非空时 `note` 给空字符串。
"""
NEW_RULE = """- **kind 为 `figure` 的段落是图**：输入给的是 `figure.caption`（图题）、
  `figure.content`（图内文字，可能是空的数组）与 `figure.labels`（图上标签：
  坐标轴刻度、图例、子图标题，这些**不翻译**）。
  返回 `"figure": {"caption": "图题译文", "content": [...], "note": "..."}`；
  **`caption` 必须有**（`Figure 1:` 译成 `图 1：`）；
  `content` 是图里**成句的文字**（示意框里的词、检索结果文本），逐条翻译，允许重新断行；
  **`note` 每一幅图都必须写**（这是本任务的硬要求，不是可选项）——
  用一条译注告诉读者"原文这里是什么图、表达了什么"，形如：
  · 示意图：`〔译注：原文此处为四幅并排的遍历过程示意（a–d），图中节点编号 1–7 表示迭代轮次。此处保留图题与图例。〕`
  · 数据图：`〔译注：原文此处为柱状图，上述数值为图中各柱的标注值，按原文顺序照录；具体对应关系以原图为准。〕`
  · 曲线图：`〔译注：原文此处为按数据集分面的四条 MRR@10–延迟曲线，无数值表格。此处保留图题与图例。〕`
  依据只能是**图题、图内文字、标签与上下文**，**不要编造图里没有的数字或结论**；
  说明"图题与图例是否已保留"以及"数值是不是图中标注值"这类对读者有用的事实。
"""
assert OLD_RULE in s, "figure 规则锚点未匹配"
s = s.replace(OLD_RULE, NEW_RULE)

# 下发时带上 labels（模型写译注的依据）
OLD_SEND = '''            item["figure"] = figure_for_prompt(fig)'''
NEW_SEND = '''            item["figure"] = figure_for_prompt(fig)'''
assert OLD_SEND in s
s = s.replace('''def figure_for_prompt(fig: dict) -> dict:
    """图给模型的形状：图题 + 图内文字（可能是空的）+ 上下文（用于写译注）。"""
    return {"caption": fig.get("caption", ""),
            "content": [str(x) for x in (fig.get("content") or [])]}''',
'''def figure_for_prompt(fig: dict) -> dict:
    """图给模型的形状：图题 + 图内文字 + 图上标签（写译注的依据）。"""
    return {"caption": fig.get("caption", ""),
            "content": [str(x) for x in (fig.get("content") or [])],
            # 标签（坐标轴刻度、图例）不翻译，但**必须给模型看** ——
            # 它要靠这些写"原文此处是什么图、数值是不是图中标注值"（参照稿就是这么写的）
            "labels": [str(x) for x in (fig.get("labels") or [])]}''', 1)

# 校验：note 现在必须非空（缺失就判失败，让上游重试）
OLD_VAL = '''    return {"caption": caption,
            "content": [str(x).strip() for x in content if str(x).strip()],
            "note": str(translated.get("note") or "").strip()}'''
NEW_VAL = '''    note = str(translated.get("note") or "").strip()
    if not note:
        # 译注是硬要求（参照稿每幅图都有）：没有就判失败，让这一轮重试
        return None
    return {"caption": caption,
            "content": [str(x).strip() for x in content if str(x).strip()],
            "note": note}'''
assert OLD_VAL in s, "apply_figure 锚点未匹配"
s = s.replace(OLD_VAL, NEW_VAL)

# 旧译文失效：图缺译注也算失效（否则永远不会补）
OLD_STALE = '''    if kind == "figure":
        en = (para.get("figure") or {}).get("content") or []
        zh = (rec.get("figure") or {}).get("content") or []
        # 源图里本来没文字、译文却凭空有 / 反之，都说明是旧结构
        if bool(zh) != bool(en):
            return True
        return False'''
NEW_STALE = '''    if kind == "figure":
        en = (para.get("figure") or {}).get("content") or []
        zh = (rec.get("figure") or {}).get("content") or []
        # 源图里本来没文字、译文却凭空有 / 反之，都说明是旧结构
        if bool(zh) != bool(en):
            return True
        # 缺译注也算失效：译注是硬要求，缺了要补（参照稿每幅图都有一条）
        if not (rec.get("figure") or {}).get("note"):
            return True
        return False'''
assert OLD_STALE in s, "_is_stale figure 锚点未匹配"
s = s.replace(OLD_STALE, NEW_STALE)

pp.write_text(s, encoding="utf-8")
import ast
ast.parse(s)
print("① 图必须带译注：规则 + 校验 + 失效判据 三处都改了")

# ---------------- ② 分页标记行 ----------------
pj = Path("web/js/bilingual.js")
js = pj.read_text(encoding="utf-8")
OLD_LOOP = """      var to = p.page_end || from;"""
assert OLD_LOOP in js, "行渲染锚点未匹配"

# 在行创建前插入"跨页标记行"
NEW_LOOP = """      // 页与页之间插一条**两栏都有**的分页标记行（参照稿 .row.pbreak 的写法）。
      // 目的：中英分界线全程连续 —— 只在一侧标记会让分界线断掉。
      if (prevPage && from > prevPage) {
        var pb = el('div', 'row pbreak');
        pb.dataset.pg = prevPage + '→' + from;
        var pbEn = el('div', 'en');
        pbEn.textContent = '— page ' + prevPage + ' ends · page ' + from + ' begins —';
        var pbZh = el('div', 'zh');
        pbZh.textContent = '—— 原文第 ' + prevPage + ' 页结束 · 第 ' + from + ' 页开始 ——';
        pb.appendChild(pbEn);
        pb.appendChild(pbZh);
        container.appendChild(pb);
      }
      prevPage = to;
      var to = p.page_end || from;"""
# 注意：prevPage 必须在 to 计算之后才能赋值 —— 上面先算 from/to 再插标记
NEW_LOOP = """      var to = p.page_end || from;
      // 页与页之间插一条**两栏都有**的分页标记行（参照稿 .row.pbreak 的写法）。
      // 目的：中英分界线全程连续 —— 只在一侧标记会让分界线断掉。
      if (prevPage && from > prevPage) {
        var pb = el('div', 'row pbreak');
        pb.dataset.pg = prevPage + '→' + from;
        var pbEn = el('div', 'en');
        pbEn.textContent = '— page ' + prevPage + ' ends · page ' + from + ' begins —';
        var pbZh = el('div', 'zh');
        pbZh.textContent = '—— 原文第 ' + prevPage + ' 页结束 · 第 ' + from + ' 页开始 ——';
        pb.appendChild(pbEn);
        pb.appendChild(pbZh);
        container.appendChild(pb);
      }
      prevPage = to;"""
js = js.replace(OLD_LOOP, NEW_LOOP, 1)

# 声明 prevPage（模块级，渲染时重置）
js = js.replace("""  var seenTerms = { en: {}, zh: {} };""",
"""  var seenTerms = { en: {}, zh: {} };
  // 上一行的结束页：用来插分页标记行（渲染开始时重置）
  var prevPage = 0;""", 1)
js = js.replace("""    seenTerms = { en: {}, zh: {} };""",
"""    seenTerms = { en: {}, zh: {} };
    prevPage = 0;""", 1)
pj.write_text(js, encoding="utf-8")
print("② 分页标记行：渲染逻辑已加")
