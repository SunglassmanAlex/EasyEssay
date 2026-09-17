/* EasyEssay · 双栏对照渲染核心
   左栏英文原文 / 右栏中文译文，逐段严格对齐。排版对齐「论文中英对照」成品的做法：
   - 衬线正文 + 左侧页码栏（p.5 / p.1–2）
   - 页码只出现在每段开头（左栏 p.5 / p.5–6），正文里不插换页提示
   - 独立公式放进公式框；表格左右各完整一份（数值 / 行序 / 列义一致）
   - 章节标题 / 图表题注 / 参考文献分级；页尾「全文完」
   - 术语高亮、选中片段浮出「问 AI」、右下角深浅色切换
   应用内阅读页与导出的离线 HTML 共用这份代码。 */
(function (global) {
  'use strict';

  var EE = global.EasyEssay = global.EasyEssay || {};
  var md = EE.md;

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  // localStorage 在 file:// 等 opaque origin 下访问会直接抛 SecurityError，
  // 导出的离线 HTML 正是这种场景，必须包起来。
  var store = {
    get: function (k) {
      try { return global.localStorage ? global.localStorage.getItem(k) : null; } catch (e) { return null; }
    },
    set: function (k, v) {
      try { if (global.localStorage) global.localStorage.setItem(k, v); } catch (e) { }
    }
  };

  function typeset(root, after) {
    // `after` 在排版真正跑完之后调用一次。必须等排版完再动 DOM ——
    // 往 `$...$` 中间插元素会把公式切成两半（见 markMissingGlyphs）。
    var fired = false;

    function done() {
      if (fired) return;
      fired = true;
      if (after) { try { after(); } catch (e) { } }
    }

    function attempt() {
      var MJ = global.MathJax;
      if (!MJ) return false;
      if (MJ.startup && MJ.startup.promise) {
        MJ.startup.promise.then(function () {
          try { MJ.typesetPromise([root]).then(done).catch(done); } catch (e) { done(); }
        }).catch(done);
        return true;
      }
      if (typeof MJ.typesetPromise === 'function') {
        try { MJ.typesetPromise([root]).then(done).catch(done); return true; } catch (e) { return false; }
      }
      return false;
    }
    if (attempt()) return;
    var tries = 0;
    var timer = setInterval(function () {
      tries += 1;
      if (attempt() || tries > 66) {
        clearInterval(timer);
        if (tries > 66) done();   // MathJax 起不来也要把标记打上
      }
    }, 150);
  }

  /**
   * 把 ⟦?⟧ 换成看得懂的标记。
   *
   * 它是"PDF 字体表损坏、这个字形认不出来"的占位符（见 app/mathify.py）。
   * 裸着晾出来跟乱码没区别 —— 用户第一反应就是"你这识别怎么全是乱码"。
   * 这里包一层带 tooltip 的小标记，让它是"一个明确的提示"而不是"乱码"。
   *
   * 只在排版**之后**调用，且跳过 MathJax 生成的 mjx-container ——
   * 公式内部的字符由 MathJax 自己画，往里插元素会破坏公式。
   */
  function markMissingGlyphs(root) {
    if (!root || !document.createTreeWalker) return 0;
    var GL = '\u27e6?\u27e7';        // ⟦?⟧

    function insideMath(node) {
      var p = node.parentNode;
      while (p && p !== root) {
        var tag = (p.tagName || '').toLowerCase();
        if (tag === 'mjx-container' || tag === 'mjx-assistive-mml') return true;
        var cls = p.className;
        if (typeof cls === 'string' && (cls.indexOf('mjx') >= 0 || cls.indexOf('MathJax') >= 0)) {
          return true;
        }
        p = p.parentNode;
      }
      return false;
    }

    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        if (!node.nodeValue || node.nodeValue.indexOf(GL) < 0) return NodeFilter.FILTER_SKIP;
        return insideMath(node) ? NodeFilter.FILTER_SKIP : NodeFilter.FILTER_ACCEPT;
      }
    }, false);

    var targets = [];
    var n;
    while ((n = walker.nextNode())) targets.push(n);
    targets.forEach(function (node) {
      var parts = node.nodeValue.split(GL);
      var frag = document.createDocumentFragment();
      parts.forEach(function (seg, i) {
        if (i) {
          var s = el('span', 'glyph-missing', '字形?');
          s.title = 'PDF 的字体表损坏了，这个字形认不出来 —— 不是乱码，也不是识别错误。'
            + '它多半是个大括号或大型运算符；已交给 AI 按上下文还原：'
            + '切到顶栏「重建后」看结果，若仍是这个样子，可点「修复公式段」重试。';
          frag.appendChild(s);
        }
        if (seg) frag.appendChild(document.createTextNode(seg));
      });
      if (node.parentNode) node.parentNode.replaceChild(frag, node);
    });
    return targets.length;
  }

  // ------------------------------------------------------------ 术语高亮

  /**
   * 这个位置在不在 `$…$` 公式区间里（用 `$` 的奇偶判断，`\$` 不算）。
   *
   * ⚠️ 必须判：术语高亮是往**文本节点里插 `<span>`**，如果插进了 `$…$`，
   * MathJax 拿到的是 `t_{<span ...>read</span>}` —— 直接解析失败，公式整块崩
   * （实测导出里出现过，交付自检也能抓到）。
   */
  function insideMath(text, pos) {
    var n = 0;
    for (var i = 0; i < pos && i < text.length; i++) {
      if (text.charAt(i) === '$' && text.charAt(i - 1) !== '\\') n++;
    }
    return n % 2 === 1;
  }

  /**
   * 术语高亮：**每个术语只标首次出现**（全篇一次），并带上"英文 · 中文"提示。
   *
   * ⚠️ 为什么不是"每处都标"：逐段各标一次，同一术语会被标几十次 ——
   * 实测 24 页论文标出 **1367 处**，而交付规格给的参考密度是 **100–150 处**
   * （≈ 去重术语数）。满篇高亮等于没有重点，还把正文淹了。
   */
  // ⚠️ 必须是**模块级**变量：highlightTerms 是模块级函数，
  // 看不到 renderBilingual 内部的 state —— 写成 state.seenTerms 会直接
  // `ReferenceError: state is not defined`，整页渲染不出来
  // （真实浏览器自检当场抓到；jsdom 那次也栽在同类的 IIFE 作用域上）。
  // 每次渲染开始时重置（见 renderBilingual 里的赋值）。
  var seenTerms = { en: {}, zh: {} };
  // 上一行的结束页：用来插分页标记行（渲染开始时重置）
  var prevPage = 0;

  function highlightTerms(root, terms, lang) {
    if (!terms || !terms.length) return;
    var seen = seenTerms[lang];
    var pairs = [];
    terms.forEach(function (t) {
      var needle = lang === 'zh' ? t.zh : t.en;
      if (!needle || needle.length < 2) return;
      if (seen[needle]) return;          // 已标过 → 跳过
      // 提示 = 英文 · 中文 — 解释（有解释时）。首次出现的那个标记就是"术语登场点"，
      // 解释挂在这里最自然（规格 §7「首次出现给英文 + 中文 + 简短解释」）。
      var tip = [t.en, t.zh].filter(Boolean).join(' · ');
      if (t.note) tip += '——' + t.note;
      pairs.push({ needle: needle, tip: tip });
    });
    if (!pairs.length) return;
    pairs.sort(function (a, b) { return b.needle.length - a.needle.length; });

    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        var p = node.parentElement;
        while (p && p !== root) {
          var tag = p.tagName || '';
          if (p.classList && (p.classList.contains('math') || p.classList.contains('term')
            || p.classList.contains('term-en'))) return NodeFilter.FILTER_REJECT;
          if (tag === 'CODE' || tag === 'PRE' || tag.indexOf('MJX') === 0) return NodeFilter.FILTER_REJECT;
          p = p.parentElement;
        }
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);

    nodes.forEach(function (node) {
      var text = node.nodeValue;
      var hit = null, at = -1;
      for (var i = 0; i < pairs.length; i++) {
        // 从每个可能位置往后找**第一个不在公式里的**匹配
        var from = 0, idx = -1;
        while (from <= text.length - pairs[i].needle.length) {
          var k = text.indexOf(pairs[i].needle, from);
          if (k < 0) break;
          if (!insideMath(text, k)) { idx = k; break; }
          from = k + 1;
        }
        if (idx >= 0 && (at < 0 || idx < at)) { at = idx; hit = pairs[i]; }
      }
      if (!hit) return;
      var frag = document.createDocumentFragment();
      var rest = text;
      while (at >= 0) {
        if (at > 0) frag.appendChild(document.createTextNode(rest.slice(0, at)));
        var span = el('span', lang === 'zh' ? 'term' : 'term-en', md.esc(hit.needle));
        span.title = hit.tip;
        frag.appendChild(span);
        seen[hit.needle] = true;          // 记下，后续段落不再重复标
        rest = rest.slice(at + hit.needle.length);
        // 同一段里同一术语**只标一次**（记进 seen 后，外层循环的下一次调用会跳过它）
        at = -1;
        at = -1; hit = null;
        for (var j = 0; j < pairs.length; j++) {
          var k = rest.indexOf(pairs[j].needle);
          if (k >= 0 && (at < 0 || k < at)) { at = k; hit = pairs[j]; }
        }
      }
      if (rest) frag.appendChild(document.createTextNode(rest));
      if (node.parentNode) node.parentNode.replaceChild(frag, node);
    });
  }

  // ------------------------------------------------------------ 单元格

  /** 独立公式：把 $$...$$ 提出来放进公式框；残留文字（公式编号等）另起一行 */
  function fillEquationCell(cell, text) {
    var m = String(text || '').match(/\$\$[\s\S]+?\$\$|\$[^$\n]+?\$/);
    if (m && m[0].length >= text.replace(/\s/g, '').length * 0.55) {
      var rest = (text.slice(0, m.index) + ' ' + text.slice(m.index + m[0].length)).trim();
      var box = el('div', 'eq');
      box.innerHTML = md.esc(m[0].indexOf('$$') === 0 ? m[0] : '$' + m[0] + '$');
      cell.appendChild(box);
      if (rest) cell.appendChild(el('div', 'eq-tag', md.inlineMd(rest)));
      return;
    }
    md.richInto(cell, text);
  }

  /**
   * 表格单元格：渲染成**真正的 <table>**，而不是把单元格拍成一行文字。
   *
   * 数据来自抽取阶段重建的网格（见 app/tables.py）：
   *   {columns, head_rows, rows: [[{text, span} | null], ...]}
   * 跨列用 colspan 表达 —— markdown 做不到这一点，所以表头会错位，
   * 这正是"表格被拆开、排得乱七八糟"的来源。
   */
  function fillTableCell(cell, grid) {
    var rows = (grid && grid.rows) || [];
    if (!rows.length) return false;
    var head = grid.head_rows || 0;
    var box = el('div', 'tblbox');
    var table = document.createElement('table');
    // 列数写进属性：CSS 据此给"≥8 列的表"用小一号字（半栏里塞 10+ 列会溢出）
    var ncols = (grid && grid.columns) || 0;
    table.setAttribute('data-cols', String(ncols));
    table.className = 't';                      // 与参照稿同名，样式可对齐
    if (ncols >= 8) table.className += ' sm';   // 宽表缩小一号（参照稿的 table.t.sm）
    var thead = document.createElement('thead');
    var tbody = document.createElement('tbody');

    // —— 先判列的性子，再渲染（列级判据比逐格猜可靠）——
    // textCol[i]：第 i 列是不是"说明列"（该左对齐 + 允许换行）
    // symCol[i] ：第 i 列是不是"符号列"（该按数学渲染，参照稿里是 `$M$`）
    var nCols = (grid && grid.columns) || 0;
    var textCol = [], symCol = [];
    for (var ci = 0; ci < nCols; ci++) {
      var vals = [];
      for (var ri = 0; ri < rows.length; ri++) {
        var cl = rows[ri] && rows[ri][ci];
        var cv = (typeof cl === 'object' ? (cl && cl.text) : cl);
        if (cv !== null && cv !== undefined && String(cv).trim()) vals.push(String(cv).trim());
      }
      var longish = vals.filter(function (v) { return v.length > 12 && /\s/.test(v); }).length;
      var symbols = vals.filter(function (v) { return /^[A-Za-z][A-Za-z0-9]{0,4}$/.test(v); }).length;
      textCol[ci] = vals.length > 0 && longish / vals.length >= 0.5;
      symCol[ci] = !textCol[ci] && vals.length > 0 && symbols / vals.length >= 0.6;
    }

    function makeRow(cols, isHead, cells) {
      var tr = document.createElement('tr');
      for (var i = 0; i < cells.length; i++) {
        var c = cells[i];
        if (c === null || c === undefined) continue;   // 被 colspan 覆盖的位置
        var cellEl = document.createElement(isHead ? 'th' : 'td');
        var span = (typeof c === 'object' && c.span) ? c.span : 1;
        if (span > 1) cellEl.colSpan = span;
        var txt = (typeof c === 'object' ? (c.text || '') : String(c));
        // 说明列左对齐 + 允许换行（参照稿的 td.l）；其余列靠 CSS 的 nowrap 保持一行
        if (!isHead && textCol[i]) cellEl.className = 'l';
        // 符号列按数学渲染（`M` → `$M$`，斜体），与参照稿一致；已是 $…$ 的不动
        if (symCol[i] && /^[A-Za-z][A-Za-z0-9]{0,4}$/.test(txt)) txt = '$' + txt + '$';
        // ⚠️ 单元格必须用**行内**渲染：richInto 是块级渲染，会给每格套一个 <p>，
        // `<p>` 自带上下边距 → 表格虚胖、行距不一致（这是观感差的主因）。
        cellEl.innerHTML = md.inlineMd(txt);
        tr.appendChild(cellEl);
      }
      return tr;
    }

    for (var r = 0; r < rows.length; r++) {
      (r < head ? thead : tbody).appendChild(makeRow(grid.columns, r < head, rows[r]));
    }
    if (thead.childNodes.length) table.appendChild(thead);
    table.appendChild(tbody);
    box.appendChild(table);
    cell.appendChild(box);
    return true;
  }

  // 注意：**不再渲染任何换页提示**。段落开头的页码（左栏的 p.5 / p.5–6）已经说明了
  // 它来自第几页、是否跨页；再在正文里插一行提示只是噪音（用户明确要求去掉）。
  // 所以这里连 page_break_at 都不再消费。
  /**
   * 伪代码/算法块：**按行**渲染，保留行号与缩进。
   *
   * 为什么不能走表格或普通段落：
   *   - 表格会把每行拆成单元格（`1 V ← ep // set of visited nodes` 变成 5 个格子），
   *     行号与缩进这一层结构就没了 —— 用户实测反馈过这一点；
   *   - 普通段落会把连续几行并成一段，同样看不出循环层级。
   * 所以每行一个 <div>、`white-space: pre` 保住前导空格。
   */
  /**
   * 协议块（第二轮 AI 的产物）：标题 + 输入输出 + **编号步骤**（可带子步骤）。
   *
   * 与"按行渲染"的区别：这里靠 `<ol>` 表达"第几步"，不再靠缩进。
   * 参照成品级对照稿的 `.proto` + `<ol>` 就是这个做法 ——
   * 安全游戏那种带 (a)(b)(c) 的段落，按行渲染读起来是一团。
   * 模型偶尔偷懒（把 17 步压成 3 句）：后端有验收闸门拦着，
   * 拦不住就退回按行渲染（见 fillAlgorithmCell 的兜底）。
   */
  function fillProtocolCell(cell, proto, lang) {
    if (!proto) return false;
    // 英文栏：放**原文**那几条（编号 + 原句），用按行渲染保持原样。
    // 协议的结构化步骤是中文的 —— 直接摆到英文栏会串味
    // （实测英文栏里出现过「图 4：Compass 的安全博弈」）。
    if (lang === 'en') {
      var enLines = proto.lines_en || [];
      if (!enLines.length && !proto.title_en) return false;
      var ebox = el('div', 'proto');
      if (proto.title_en) {
        var et = el('div', 'pt');
        et.textContent = proto.title_en;
        ebox.appendChild(et);
      }
      enLines.forEach(function (ln) {
        var d = document.createElement('div');
        d.className = 'algline';
        d.textContent = ln;
        ebox.appendChild(d);
      });
      cell.appendChild(ebox);
      return true;
    }
    if (!proto.steps || !proto.steps.length) return false;
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

  function fillAlgorithmCell(cell, lines, proto, lang) {
    // 优先用第二轮整理好的结构化协议；没有（或偷懒被闸门拦下）就按行渲染
    if (fillProtocolCell(cell, proto, lang)) return true;
    if (!lines || !lines.length) return false;
    var box = el('div', 'algobox');
    var start = 0;
    // 首行是 `Algorithm 1: ...` / `Protocol 2:` 这类标题 → 拎出来当 accent 标题，
    // 与成品级对照稿的协议块视觉一致（.pt）
    if (/^\s*(Algorithm|Protocol|算法|协议)\s*\d+\s*[:：]/i.test(lines[0])) {
      var t = el('div', 'pt');
      t.textContent = lines[0].trim();
      box.appendChild(t);
      start = 1;
    }
    for (var i = start; i < lines.length; i++) {
      var line = document.createElement('div');
      line.className = 'algline';
      // 用 textContent：伪代码里的 < > ← 等符号不该被当成 HTML/Markdown
      line.textContent = lines[i];
      box.appendChild(line);
    }
    cell.appendChild(box);
    return true;
  }

  /**
   * 图框：把"图"框起来 —— 图内文字 + 图题 + 可选的译注。
   *
   * 参照成品级对照稿的做法（`.figbox` / `.figcap` / `.note`）：
   * 图里的文字（图例、示意框）单独成块，图题在底部用虚线隔开，
   * 图是纯图形时显示模型写的译注（〔译注：原文此处为一幅…〕）——
   * 这样读者知道"这里原本有一张图、它是关于什么的"，而不是一片空白。
   */
  function fillFigureCell(cell, fig, para0_note) {
    if (!fig || !fig.caption) return false;
    var box = el('div', 'figbox');
    (fig.content || []).forEach(function (line) {
      var d = el('div', 'fline');
      d.textContent = line;
      box.appendChild(d);
    });
    // 图上的标签（坐标轴刻度、图例）：弱化展示，**不翻译**（规格 §5）
    if (fig.labels && fig.labels.length) {
      var lab = el('div', 'figlabels');
      lab.textContent = fig.labels.join(' · ');
      box.appendChild(lab);
    }
    // 译注：本侧没有就用另一侧带过来的（参照稿左右各一份）
    var noteText = fig.note || para0_note;
    if (noteText) {
      var note = el('div', 'note');
      note.textContent = noteText;
      box.appendChild(note);
    }
    var cap = el('div', 'figcap');
    cap.textContent = fig.caption;
    box.appendChild(cap);
    cell.appendChild(box);
    return true;
  }

  /** 表题：贴在表格上方（.tcap），与表格同一格，左右两栏各自一份 */
  function tableCaption(cell, caption) {
    if (!caption) return;
    var d = el('div', 'tcap');
    d.textContent = caption;
    cell.appendChild(d);
  }

  /** 项目符号列表：`• a • b` → `<ul><li>a</li><li>b</li></ul>`（标准答案的排法）。
   *
   * 标准答案里**全文没有 `•`** —— 条目一律进 `<ul><li>`，且条目标签加粗
   * （`<b>INIT(</b>$D$<b>):</b> …`），读起来才像 API 手册而不是一坨文字。
   */
  function fillListCell(cell, text) {
    var parts = String(text || '').split(/\s*•\s*/);
    if (parts.length < 3) return false;      // 至少两个条目才算列表
    var intro = parts.shift().trim();
    var ul = document.createElement('ul');
    ul.className = 'md-ul';
    parts.forEach(function (item) {
      item = item.trim();
      if (!item) return;
      var li = document.createElement('li');
      // 条目标签（第一个冒号/括号之前）加粗，与标准答案一致
      var m = item.match(/^([^:：]{1,42}[:：])\s*(.*)$/);
      if (m && m[1].length <= 42) {
        li.innerHTML = '<b>' + md.esc(m[1]) + '</b> ' + md.inlineMd(m[2]);
      } else {
        li.innerHTML = md.inlineMd(item);
      }
      ul.appendChild(li);
    });
    if (intro) {
      var ip = document.createElement('p');
      ip.innerHTML = md.inlineMd(intro);
      cell.appendChild(ip);
    }
    cell.appendChild(ul);
    return true;
  }

  function fillCell(cell, para, kind, lang) {
    var text = para.text || '';
    if (kind === 'figure') {
      // ⚠️ 顺序要紧：**协议框优先**。安全游戏那种"图"其实是规则条目，
      // 参照稿把它渲染成 `.proto` + 编号步骤；而图框分支一旦先 return，
      // 协议分支就永远走不到（实测导出里 `.proto` 一直是 0）。
      if (para.protocol && fillProtocolCell(cell, para.protocol, lang)) return;
      var fig = para.figure;
      if (fig && fig.caption && fillFigureCell(cell, fig, para.figureNote)) return;
    }
    if (kind === 'table' && para.table) {
      // ⚠️ 顺序：表格在上、表题在下（参照稿就是 `<div class="tblbox">…</div>`
      // 紧跟 `<div class="tcap">Table 1: …</div>`）。
      if (fillTableCell(cell, para.table)) {
        tableCaption(cell, para.caption);
        return;
      }
    }
    if (kind === 'algorithm') {
      var lines = (para.algorithm && para.algorithm.lines) || String(text).split('\n');
      if (fillAlgorithmCell(cell, lines, para.protocol, lang)) return;
    }
    if (kind === 'equation') { fillEquationCell(cell, text); return; }
    // 项目符号列表（`• a • b`）→ 渲染成 <ul><li>，而不是把符号摆出来
    if (String(text).indexOf('•') >= 0 && fillListCell(cell, text)) return;
    md.richInto(cell, text);
  }

  // ------------------------------------------------------------ 论文头部

  function buildPaperHead(doc, stats, opts) {
    var head = el('header', 'paper-head');
    var title = doc.title || (doc.meta || {}).title || 'EasyEssay';
    head.appendChild(el('h1', null, md.esc(title) + ' · 全文中英对照'));

    var zhTitle = (doc.translations || {})[opts.titleParaId || 'p0001'];
    var sub = zhTitle && zhTitle.zh ? zhTitle.zh : (opts.subtitle || '');
    if (sub) head.appendChild(el('p', 'sub', md.inlineMd(sub.split('\n')[0])));

    var authors = '';
    var raw = (doc.paragraphs || [])[opts.authorParaIndex == null ? 1 : opts.authorParaIndex];
    if (raw && raw.text && raw.text.length < 400) {
      authors = raw.text.replace(/\$[^$]*\$/g, '').replace(/\s+/g, ' ').trim();
    }
    var metaBits = [];
    if (authors) metaBits.push(md.esc(authors));
    if ((doc.meta || {}).page_count) metaBits.push('全文 ' + (doc.meta || {}).page_count + ' 页');
    if (stats.paragraphs) metaBits.push('共 ' + stats.paragraphs + ' 段');
    if (stats.translated) metaBits.push('已译 ' + stats.translated + ' 段');
    if ((doc.meta || {}).settings && (doc.meta || {}).settings.model) {
      metaBits.push('模型 ' + md.esc((doc.meta || {}).settings.model));
    }
    if (metaBits.length) head.appendChild(el('p', 'meta', metaBits.join(' · ')));

    head.appendChild(el('p', 'hint',
      '左 = 英文原文 · 右 = 中文译文，段落逐一对齐，两栏分界线自首页至参考文献保持同一条。'
      + '最左侧页栏标出每段在 PDF 中的<b>起始页</b>；每两页之间有一条虚线<b>分页标记行</b>。'
      + '若某段原文被页边界切断，页码标为区间 <code>p.1–2</code>。'
      + '表格在左右两栏各完整复现一份（左英文表头 / 右中文表头），<b>数值、行序、列义与原文一致</b>。'
      + '公式由 MathJax 渲染；若显示为 <code>$…$</code> 源码，联网后刷新即可。'));
    return head;
  }

  // ------------------------------------------------------------ 主渲染

  function renderBilingual(container, doc, opts) {
    opts = opts || {};
    var translations = doc.translations || {};
    var paragraphs = doc.paragraphs || [];
    var state = {
      viewMode: opts.viewMode || 'all',
      hideRefs: !!opts.hideRefs,
      showRaw: false,
      scale: parseFloat(store.get('ee-scale') || '1') || 1
    };
    // 每次渲染重置"术语已标过"的集合 → 每个术语全篇只标首次出现
    seenTerms = { en: {}, zh: {} };
    prevPage = 0;

    // 术语表的取得：优先用**全局术语表**（规格 §7 说它是唯一真源）；
    // 老文档没有 glossary 时，从各段 terms 去重兜底。
    var docTerms = (doc.glossary && doc.glossary.length)
      ? doc.glossary.map(function (g) {
          // 第 3 个元素是"一句话解释"（规格 §7：首次出现给「英文 + 中文 + 简短解释」）
          return { en: g[0] || g.en || '', zh: g[1] || g.zh || '', note: g[2] || '' };
        })
      : (function () {
          var seen = {}, out = [];
          paragraphs.forEach(function (p) {
            (((translations || {})[p.id] || {}).terms || []).forEach(function (t) {
              var k = (t.en || '').toLowerCase();
              if (k && !seen[k]) { seen[k] = 1; out.push(t); }
            });
          });
          return out;
        })();

    var wrapMeta = (doc.meta || {}).stats || {};
    var stats = {
      paragraphs: wrapMeta.paragraphs || paragraphs.length,
      translated: wrapMeta.translated || Object.keys(translations).length
    };

    container.innerHTML = '';
    container.classList.add('ee-reader');
    container.classList.remove('view-en', 'view-zh', 'hide-refs', 'show-raw');

    var wrap = el('div', 'wrap');
    container.appendChild(wrap);

    if (opts.header !== false) wrap.appendChild(buildPaperHead(doc, stats, opts));

    var colhead = el('div', 'colhead');
    colhead.appendChild(el('div', 'l', 'English（原文）'));
    colhead.appendChild(el('div', 'r', '中文译文'));
    wrap.appendChild(colhead);

    var rowMap = {};
    var switches = [];
    var toc = [];

    paragraphs.forEach(function (p) {
      var from = p.page || 1;
      var to = p.page_end || from;

      // 页与页之间插一条**左右两栏都有**的分页标记行。
      // 参照稿 `.row.pbreak` 就是这么写的 —— 关键点是**两栏都占位**：
      // 只在一侧标记会让中英分界线在那一行断掉（规格 §8 的原话）。
      if (prevPage && from > prevPage) {
        var pb = el('div', 'row pbreak');
        pb.dataset.pg = prevPage + '→' + from;
        var pbEn = el('div', 'en');
        pbEn.textContent = '— page ' + prevPage + ' ends · page ' + from + ' begins —';
        var pbZh = el('div', 'zh');
        pbZh.textContent = '—— 原文第 ' + prevPage + ' 页结束 · 第 ' + from + ' 页开始 ——';
        pb.appendChild(pbEn);
        pb.appendChild(pbZh);
        wrap.appendChild(pb);      // 容器变量叫 wrap（写成 container 会 ReferenceError）
      }
      prevPage = to;

      var tr = translations[p.id] || {};
      var kind = p.kind || 'text';
      var cls = 'row brow';
      if (kind === 'title') cls += ' head';
      else if (kind === 'heading') cls += (p.level && p.level >= 2) ? ' sub' : ' head';
      else if (kind === 'caption') cls += ' caption';
      else if (kind === 'equation') cls += ' eq';
      else if (kind === 'reference') cls += ' tiny ref';
      var row = el('section', cls);
      row.id = 'para-' + p.id;
      row.dataset.id = p.id;
      row.dataset.pg = (to !== from) ? ('p.' + from + '–' + to) : ('p.' + from);
      if (kind === 'heading' || kind === 'title') {
        row.dataset.heading = p.level || 1;
        toc.push({ id: p.id, label: (p.text || '').slice(0, 90),
                   level: kind === 'title' ? 1 : (p.level || 1) });
      }

      var en = el('div', 'en bcell');
      var zh = el('div', 'zh bcell');

      // 左栏优先用「重建原文」；可一键切回 PDF 直抽
      var rebuilt = typeof tr.en === 'string' && tr.en.trim() && tr.en !== p.text;
      if (rebuilt) {
        fillCell(en, { kind: kind, text: state.showRaw ? p.text : tr.en, table: p.table,
                    caption: p.caption,
                    figure: tr.en_figure || p.figure,
                    // 译注在两侧都显示（参照稿的排法）——译文侧带的 note 借给英文侧
                    figureNote: (tr.figure || {}).note || '',
                    protocol: tr.protocol,
                    algorithm: (state.showRaw ? p.algorithm : (tr.en_algorithm || p.algorithm)) },
                   kind, 'en');
        en.dataset.rebuilt = '1';
        en.title = '左栏为「重建原文」（公式已还原为标准 LaTeX）。点顶栏「原始抽取」可切回 PDF 直抽的原始文本。';
        switches.push({ cell: en, para: p, fixed: tr.en, table: p.table,
                       caption: p.caption, figure: tr.en_figure || p.figure,
                       protocol: tr.protocol, algorithm: tr.en_algorithm || p.algorithm });
      } else {
        fillCell(en, p, kind, 'en');
      }

      if (tr.zh) {
        fillCell(zh, { kind: kind, text: tr.zh, table: tr.table, caption: tr.caption,
                      figure: tr.figure, protocol: tr.protocol,
                      algorithm: tr.algorithm }, kind, 'zh');
        // 术语高亮：**只标译文侧**（规格 §7「把术语标进译文」）、
        // 用**全局术语表**（唯一真源）、且**表格与伪代码内不标**
        // （规格 §7「表格内不标」；伪代码是代码，标了反而干扰）。
        if (docTerms.length && kind !== 'table' && kind !== 'algorithm') {
          highlightTerms(zh, docTerms, 'zh');
        }
      } else {
        zh.classList.add('pending');
        zh.appendChild(el('div', 'empty',
          kind === 'equation' ? '（公式无需翻译）' : '待翻译…'));
      }

      row.appendChild(en);
      row.appendChild(zh);
      wrap.appendChild(row);
      rowMap[p.id] = row;
    });

    // 页尾
    var end = el('div', 'row tiny');
    end.dataset.pg = '';
    end.appendChild(el('div', 'en', '— End of paper —'));
    end.appendChild(el('div', 'zh', '— 全文完 —'));
    wrap.appendChild(end);

    var api = {
      state: state,
      toc: toc,
      typeset: function () { typeset(wrap, function () { markMissingGlyphs(wrap); }); },
      setViewMode: function (mode) {
        state.viewMode = mode;
        container.classList.toggle('view-en', mode === 'en');
        container.classList.toggle('view-zh', mode === 'zh');
        return mode;
      },
      toggleRefs: function (force) {
        state.hideRefs = force == null ? !state.hideRefs : !!force;
        container.classList.toggle('hide-refs', state.hideRefs);
        return state.hideRefs;
      },
      toggleRaw: function (force) {
        state.showRaw = force == null ? !state.showRaw : !!force;
        container.classList.toggle('show-raw', state.showRaw);
        switches.forEach(function (s) {
          s.cell.innerHTML = '';
          if (state.showRaw) {
            fillCell(s.cell, s.para, s.para.kind || 'text');
          } else {
            fillCell(s.cell, { kind: s.para.kind, text: s.fixed, table: s.table,
                           caption: s.caption, figure: s.figure,
                           protocol: s.protocol, algorithm: s.algorithm }, s.para.kind || 'text');
          }
        });
        if (switches.length) typeset(wrap, function () { markMissingGlyphs(wrap); });
        return state.showRaw;
      },
      setScale: function (scale) {
        state.scale = Math.min(1.8, Math.max(0.75, scale));
        var r = document.documentElement.style;
        r.setProperty('--fs-en', (15.5 * state.scale).toFixed(2) + 'px');
        r.setProperty('--fs-zh', (15.5 * state.scale).toFixed(2) + 'px');
        // 结构化块（表格 / 伪代码 / 图框 / 表题）也一起缩放 ——
        // 它们原来是写死的 px，用户把正文字号调大后会显得明显偏小。
        r.setProperty('--fs-block', (13.5 * state.scale).toFixed(2) + 'px');
        r.setProperty('--fs-block-sm', (12.5 * state.scale).toFixed(2) + 'px');
        store.set('ee-scale', String(state.scale));
        return state.scale;
      },
      toggleSerif: function (force) {
        var sans = force == null ? !document.body.classList.contains('ee-sans') : !!force;
        document.body.classList.toggle('ee-sans', sans);
        store.set('ee-font', sans ? 'sans' : 'serif');
        return sans;
      },
      scrollTo: function (paraId) {
        var row = rowMap[paraId];
        if (row) row.scrollIntoView({ behavior: 'smooth', block: 'start' });
      },
      markActive: function (paraId) {
        Object.keys(rowMap).forEach(function (k) { rowMap[k].classList.remove('active'); });
        if (rowMap[paraId]) rowMap[paraId].classList.add('active');
      },
      rows: rowMap,
      markMissingGlyphs: function () { return markMissingGlyphs(wrap); }
    };

    api.setViewMode(state.viewMode);
    if (state.hideRefs) container.classList.add('hide-refs');
    api.setScale(state.scale);
    if (store.get('ee-font') === 'sans') document.body.classList.add('ee-sans');
    typeset(wrap, function () { markMissingGlyphs(wrap); });
    if (opts.onAsk) attachSelection(container, opts.onAsk);
    if (opts.themeToggle !== false) {
      var tgl = el('button', 'tgl', '切换深/浅色');
      tgl.onclick = function () { document.body.classList.toggle('dark'); };
      container.appendChild(tgl);
    }
    EE.__api = api;
    EE.__wrap = wrap;
    return api;
  }

  // ------------------------------------------------------------ 选中即问

  function attachSelection(root, onAsk) {
    var pill = null;
    function hide() { if (pill) { pill.remove(); pill = null; } }

    function show(rect, payload) {
      hide();
      pill = el('div', 'ee-selbtn');
      var b1 = el('button', null, '问 AI');
      b1.onclick = function () { hide(); onAsk(payload); };
      pill.appendChild(b1);
      var b2 = el('button', null, '解释公式');
      b2.onclick = function () {
        hide();
        onAsk(Object.assign({}, payload, { question: '请逐步解释这段内容里的公式：每个符号的含义、取值范围，以及整体在证明中的作用。' }));
      };
      pill.appendChild(b2);
      var b3 = el('button', null, '译得更准');
      b3.onclick = function () {
        hide();
        onAsk(Object.assign({}, payload, { question: '请指出这段译文可能不准确或不自然的地方，并给出更好的译法（保留公式原样）。' }));
      };
      pill.appendChild(b3);
      document.body.appendChild(pill);
      var w = pill.offsetWidth || 200;
      var left = Math.min(global.innerWidth - w - 10, Math.max(8, rect.left + rect.width / 2 - w / 2));
      var top = rect.top - pill.offsetHeight - 8;
      if (top < 60) top = rect.bottom + 8;
      pill.style.left = left + 'px';
      pill.style.top = top + 'px';
    }

    document.addEventListener('mousedown', function (e) {
      if (pill && !pill.contains(e.target)) hide();
    });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') hide(); });
    root.addEventListener('mouseup', function () {
      setTimeout(function () {
        var sel = global.getSelection();
        if (!sel || sel.isCollapsed) { hide(); return; }
        var text = String(sel.toString() || '').trim();
        if (text.length < 2) { hide(); return; }
        var range = sel.getRangeAt(0);
        var n = range.startContainer;
        var node = n.nodeType === 1 ? n : n.parentElement;
        if (!node || !node.closest) { hide(); return; }
        var cell = node.closest('.bcell');
        var row = node.closest('.row') || node.closest('.brow');
        if (!cell || !row || !root.contains(cell)) { hide(); return; }
        var rect = range.getBoundingClientRect();
        if (!rect || (!rect.width && !rect.height)) return;
        show(rect, {
          paraId: row.dataset.id,
          selection: text.slice(0, 4000),
          side: cell.classList.contains('zh') ? 'zh' : 'en'
        });
      }, 0);
    });
  }

  // ------------------------------------------------------------ 工具条

  function bindToolbar(bar, readerEl) {
    if (!bar) return;
    var viewBtns = bar.querySelectorAll('[data-act^="view-"]');
    function syncView(mode) {
      viewBtns.forEach(function (b) {
        var on = b.dataset.act === 'view-' + mode;
        b.classList.toggle('active', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
    }
    function api() { return EE.__api; }

    bar.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-act]');
      if (!btn) return;
      var act = btn.dataset.act;
      var a = api();
      switch (act) {
        case 'view-all': case 'view-en': case 'view-zh':
          if (a) { a.setViewMode(act.slice(5)); syncView(act.slice(5)); }
          break;
        case 'toggle-ref':
          if (a) btn.classList.toggle('active', a.toggleRefs());
          break;
        case 'toggle-raw':
          if (a) btn.classList.toggle('active', a.toggleRaw());
          break;
        case 'toggle-font':
          if (a) btn.classList.toggle('active', a.toggleSerif());
          break;
        case 'font-plus':
          if (a) a.setScale(a.state.scale + 0.1);
          break;
        case 'font-minus':
          if (a) a.setScale(a.state.scale - 0.1);
          break;
        case 'toggle-theme':
          document.body.classList.toggle('dark');
          document.body.classList.toggle('ee-dark');
          store.set('ee-theme', document.body.classList.contains('dark') ? 'dark' : 'light');
          break;
        case 'toggle-ask':
          if (EE.askPanel) EE.askPanel.toggle();
          break;
        case 'print':
          global.print();
          break;
      }
    });

    if (store.get('ee-theme') === 'dark') {
      document.body.classList.add('dark');
      document.body.classList.add('ee-dark');
    }
    if (api()) syncView(api().state.viewMode);
  }

  EE.renderBilingual = renderBilingual;
  EE.bindToolbar = bindToolbar;
  EE.typeset = typeset;
  EE.highlightTerms = highlightTerms;
})(window);
