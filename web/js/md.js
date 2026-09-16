/* EasyEssay · 轻量 Markdown + LaTeX 渲染
   要点：数学片段先切分再转义，保证 $...$ 内容原样交给 MathJax。 */
(function (global) {
  'use strict';

  var ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;' };
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>]/g, function (c) { return ESC[c]; }); }

  // 行内/独立公式（含 \( \) \[ \] 形式）；\$ 转义不参与匹配
  var MATH_RE = /(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|(?<!\\)\$(?!\s)(?:\\\$|[^$\n])*?(?<!\\)\$)/g;

  function splitMath(text) {
    var parts = [], last = 0, m;
    MATH_RE.lastIndex = 0;
    text = String(text || '');
    while ((m = MATH_RE.exec(text)) !== null) {
      if (m.index > last) parts.push({ math: false, s: text.slice(last, m.index) });
      parts.push({ math: true, s: m[0] });
      last = m.index + m[0].length;
    }
    if (last < text.length) parts.push({ math: false, s: text.slice(last) });
    return parts;
  }

  function plain(s) {
    var out = esc(s);
    out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
    out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    out = out.replace(/(^|[^*\w])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    out = out.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
    out = out.replace(/\\([*_`$#>\-\[\]])/g, '$1');   // 反转义
    return out;
  }

  function inlineMd(text) {
    return splitMath(text).map(function (p) {
      return p.math ? esc(p.s) : plain(p.s);
    }).join('');
  }

  function node(tag, cls, html) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (html != null) el.innerHTML = html;
    return el;
  }

  var BLOCK_MATH_RE = /^\$\$[\s\S]*\$\$$|^\\\[[\s\S]*\\\]$/;

  /** 把一段富文本渲染进容器：支持标题、列表、引用、表格、代码块、独立公式。 */
  function richInto(el, text, opts) {
    opts = opts || {};
    el.innerHTML = '';
    text = String(text == null ? '' : text).replace(/\r/g, '');
    var blocks = text.split(/\n{2,}/);

    blocks.forEach(function (block) {
      var t = block.trim();
      if (!t) return;

      // 代码块
      var fence = t.match(/^```([\w+-]*)\n?([\s\S]*?)```$/);
      if (fence) {
        el.appendChild(node('pre', null, esc(fence[2])));
        return;
      }
      // 独立公式：进 `.eq` 框（参照稿里**每个独立公式都有一个框**）。
      // 原来只标了个没有样式的 `md-display` —— 于是正文里的公式是裸的，
      // 而只有 `kind=equation` 的段才有框，观感就比参照稿差一截。
      // 两个类都留着：`eq` 负责样式，`md-display` 供既有查询/测试识别。
      if (BLOCK_MATH_RE.test(t)) {
        el.appendChild(node('div', 'eq md-display', esc(t)));
        return;
      }
      // 标题
      var h = t.match(/^(#{1,6})\s+(.*)$/);
      if (h && !t.includes('\n')) {
        var lv = Math.min(6, h[1].length);
        el.appendChild(node(lv <= 3 ? 'h' + lv : 'h4', 'md-h', inlineMd(h[2])));
        return;
      }

      var lines = block.split('\n');
      var allBullet = lines.every(function (l) { return /^\s*[-*•]\s+/.test(l); });
      var allNum = lines.every(function (l) { return /^\s*\d+[.)]\s+/.test(l); });
      var allQuote = lines.every(function (l) { return /^\s*>\s?/.test(l); });

      if (allBullet || allNum) {
        var list = node(allBullet ? 'ul' : 'ol');
        lines.forEach(function (l) {
          var item = l.replace(/^\s*(?:[-*•]|\d+[.)])\s+/, '');
          list.appendChild(node('li', null, inlineMd(item)));
        });
        el.appendChild(list);
        return;
      }
      if (allQuote) {
        el.appendChild(node('blockquote', null,
          lines.map(function (l) { return inlineMd(l.replace(/^\s*>\s?/, '')); }).join('<br>')));
        return;
      }
      // 表格
      if (lines.length >= 2 && /\|/.test(lines[0]) && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[1])) {
        var tbl = node('table', 't');
        var head = node('thead');
        var hr = node('tr');
        lines[0].split('|').filter(function (c, i, arr) { return !(i === 0 && !c.trim()) && !(i === arr.length - 1 && !c.trim()); })
          .forEach(function (c) { hr.appendChild(node('th', null, inlineMd(c.trim()))); });
        head.appendChild(hr); tbl.appendChild(head);
        var tb = node('tbody');
        lines.slice(2).forEach(function (l) {
          if (!l.trim()) return;
          var tr = node('tr');
          var cells = l.split('|').filter(function (c, i, arr) { return !(i === 0 && !c.trim()) && !(i === arr.length - 1 && !c.trim()); });
          cells.forEach(function (c, ci) {
            tr.appendChild(node('td', ci === 0 ? 'l' : null, inlineMd(c.trim())));
          });
          tb.appendChild(tr);
        });
        tbl.appendChild(tb);
        el.appendChild(node('div', 'tblbox')).appendChild(tbl);
        return;
      }

      var p = node('p');
      p.innerHTML = lines.map(inlineMd).join('<br>');
      el.appendChild(p);
    });
    return el;
  }

  /** 把纯文本按空行切成段落数组（用于 OCR 结果等） */
  function toParagraphs(text) {
    return String(text || '').split(/\n\s*\n/).map(function (s) { return s.trim(); }).filter(Boolean);
  }

  global.EasyEssay = global.EasyEssay || {};
  global.EasyEssay.md = { esc: esc, splitMath: splitMath, inlineMd: inlineMd, richInto: richInto, toParagraphs: toParagraphs };
})(window);
