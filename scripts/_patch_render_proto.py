"""给 bilingual.js 加"结构化协议"渲染（.proto + <ol>）。用完即删。"""
from pathlib import Path

p = Path("web/js/bilingual.js")
s = p.read_text(encoding="utf-8")

OLD = """  function fillAlgorithmCell(cell, lines) {
    if (!lines || !lines.length) return false;
    var box = el('div', 'algobox');
    var start = 0;"""

NEW = """  /**
   * 协议块（第二轮 AI 的产物）：标题 + 输入输出 + **编号步骤**（可带子步骤）。
   *
   * 与"按行渲染"的区别：这里靠 `<ol>` 表达"第几步"，不再靠缩进。
   * 参照成品级对照稿的 `.proto` + `<ol>` 就是这个做法 ——
   * 安全游戏那种带 (a)(b)(c) 的段落，按行渲染读起来是一团。
   * 模型偶尔偷懒（把 17 步压成 3 句）：后端有验收闸门拦着，
   * 拦不住就退回按行渲染（见 fillAlgorithmCell 的兜底）。
   */
  function fillProtocolCell(cell, proto) {
    if (!proto || !proto.steps || !proto.steps.length) return false;
    var box = el('div', 'proto');
    if (proto.title) {
      var t = el('div', 'pt');
      t.textContent = proto.title;
      box.appendChild(t);
    }
    (proto.setup || []).forEach(function (x) {
      var d = el('div', 'in');
      d.textContent = x;
      box.appendChild(d);
    });
    var ol = document.createElement('ol');
    ol.className = 'steps';
    proto.steps.forEach(function (st) {
      var li = document.createElement('li');
      var body = (typeof st === 'object' && st) ? st : { text: String(st || '') };
      // 步骤正文走 markdown：里面有 $...$ 公式、斜体标记
      if (body.text) md.richInto(li, body.text);
      var subs = body.subs || [];
      if (subs.length) {
        var sol = document.createElement('ol');
        sol.className = 'subs';
        subs.forEach(function (sub) {
          var sli = document.createElement('li');
          md.richInto(sli, sub);
          sol.appendChild(sli);
        });
        li.appendChild(sol);
      }
      ol.appendChild(li);
    });
    box.appendChild(ol);
    cell.appendChild(box);
    return true;
  }

  function fillAlgorithmCell(cell, lines, proto) {
    // 优先用第二轮整理好的结构化协议；没有（或偷懒被闸门拦下）就按行渲染
    if (fillProtocolCell(cell, proto)) return true;
    if (!lines || !lines.length) return false;
    var box = el('div', 'algobox');
    var start = 0;"""

assert OLD in s, "fillAlgorithmCell 未匹配"
s = s.replace(OLD, NEW, 1)

reps = [
    ("""    if (kind === 'algorithm') {
      var lines = (para.algorithm && para.algorithm.lines) || String(text).split('\\n');
      if (fillAlgorithmCell(cell, lines)) return;
    }""",
     """    if (kind === 'algorithm') {
      var lines = (para.algorithm && para.algorithm.lines) || String(text).split('\\n');
      if (fillAlgorithmCell(cell, lines, para.protocol)) return;
    }
    if (kind === 'figure' && para.protocol && fillProtocolCell(cell, para.protocol)) return;"""),
    ("""                    figure: tr.en_figure || p.figure,
                    algorithm: (state.showRaw ? p.algorithm : (tr.en_algorithm || p.algorithm)) }, kind);""",
     """                    figure: tr.en_figure || p.figure, protocol: tr.protocol,
                    algorithm: (state.showRaw ? p.algorithm : (tr.en_algorithm || p.algorithm)) }, kind);"""),
    ("""                      figure: tr.figure, algorithm: tr.algorithm }, kind);""",
     """                      figure: tr.figure, protocol: tr.protocol,
                      algorithm: tr.algorithm }, kind);"""),
    ("""                       caption: p.caption, figure: tr.en_figure || p.figure,
                       algorithm: tr.en_algorithm || p.algorithm });""",
     """                       caption: p.caption, figure: tr.en_figure || p.figure,
                       protocol: tr.protocol, algorithm: tr.en_algorithm || p.algorithm });"""),
    ("""                           caption: s.caption, figure: s.figure,
                           algorithm: s.algorithm }, s.para.kind || 'text');""",
     """                           caption: s.caption, figure: s.figure,
                           protocol: s.protocol, algorithm: s.algorithm }, s.para.kind || 'text');"""),
]
for old, new in reps:
    assert old in s, f"未匹配：{old[:60]!r}"
    s = s.replace(old, new, 1)

p.write_text(s, encoding="utf-8")
print("渲染已支持结构化协议；fillProtocolCell 出现", s.count("fillProtocolCell"), "次")
