/* EasyEssay · 「问 AI」侧栏
   选中原文/译文中的任意片段即可提问；回答以 SSE 流式返回并渲染 Markdown + LaTeX。
   应用内阅读页与导出的离线 HTML 共用；离线 HTML 会连回本地服务。 */
(function (global) {
  'use strict';

  var EE = global.EasyEssay = global.EasyEssay || {};
  var md = EE.md;

  var CHIPS = [
    '解释我选中的这段内容',
    '这段在整体证明/论述中起什么作用？',
    '把这里出现的公式逐步推导一遍',
    '这段里的关键术语分别是什么意思？',
    '这段译文有误吗？请给出更准确的译法'
  ];

  var el = function (tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  };

  function create(opts) {
    var state = { paraId: '', selection: '', side: '', history: [], busy: false };
    var node = opts.node;
    var docId = opts.docId || '';
    var apiBase = (opts.apiBase || global.EASYESSay_API || '').replace(/\/$/, '');
    var onOpen = opts.onOpen, onClose = opts.onClose;

    node.innerHTML = '';
    var head = el('div', 'ee-ask-head');
    head.appendChild(el('span', 't', '问 AI'));
    var ctxLabel = el('span', 'ctx', '未选中内容');
    head.appendChild(ctxLabel);
    var closeBtn = el('button', 'ee-btn ghost', '关闭');
    head.appendChild(closeBtn);

    var body = el('div', 'ee-ask-body');
    var chips = el('div', 'ee-ask-chips');
    CHIPS.forEach(function (c) {
      var b = el('button', 'ee-chip', md.esc(c));
      b.onclick = function () { ask(c); };
      chips.appendChild(b);
    });

    var input = el('div', 'ee-ask-input');
    var ta = el('textarea');
    ta.placeholder = '针对选中的内容提问…（Enter 发送，Shift+Enter 换行）';
    var row = el('div', 'row');
    var ctxWrap = el('label');
    var cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = true;
    ctxWrap.appendChild(cb);
    ctxWrap.appendChild(document.createTextNode('附带段落上下文'));
    var spacer = el('span', 'spacer');
    var sendBtn = el('button', 'ee-btn primary', '发送');
    row.appendChild(ctxWrap); row.appendChild(spacer); row.appendChild(sendBtn);
    var hint = el('div', 'ee-ask-hint',
      apiBase ? '' : '');
    input.appendChild(ta); input.appendChild(row); input.appendChild(hint);

    node.appendChild(head); node.appendChild(body); node.appendChild(chips); node.appendChild(input);

    function setCtxLabel() {
      var parts = [];
      if (state.paraId) parts.push('段落 ' + state.paraId);
      if (state.selection) parts.push('选中 ' + state.selection.length + ' 字');
      ctxLabel.textContent = parts.length ? parts.join(' · ') : '未选中内容（将结合当前视口段落）';
    }

    function addMsg(role, html, cls) {
      var box = el('div', 'ee-msg ' + (cls || '') + ' ' + role);
      box.appendChild(el('div', 'who', role === 'user' ? '我' : 'AI'));
      var bubble = el('div', 'bubble');
      if (html) bubble.innerHTML = html;
      box.appendChild(bubble);
      body.appendChild(box);
      body.scrollTop = body.scrollHeight;
      return bubble;
    }

    function welcome() {
      body.innerHTML = '';
      addMsg('assistant',
        '<p>选中左侧原文或右侧译文里的任意片段，然后点「问 AI」；也可以直接在下面输入问题。</p>' +
        '<p>回答基于该段落及其前后文，涉及公式会用 LaTeX 正常渲染。</p>');
    }

    function toggle() {
      if (node.hidden) open({}); else close();
    }

    function open(ctx) {
      ctx = ctx || {};
      if (ctx.paraId != null) state.paraId = ctx.paraId;
      if (ctx.selection != null) state.selection = ctx.selection;
      if (ctx.side != null) state.side = ctx.side;
      node.hidden = false;
      setCtxLabel();
      if (EE.__api && state.paraId) EE.__api.markActive(state.paraId);
      if (!body.childNodes.length) welcome();
      if (ctx.question) { ta.value = ''; ask(ctx.question); }
      else setTimeout(function () { ta.focus(); }, 30);
      if (onOpen) onOpen();
    }

    function close() {
      node.hidden = true;
      if (EE.__api) EE.__api.markActive('');
      if (onClose) onClose();
    }

    async function ask(question) {
      question = (question || ta.value || '').trim();
      if (!question || state.busy) return;
      if (!docId) {
        addMsg('assistant', '<p>当前文档没有关联的服务端记录，无法提问。</p>', 'err');
        return;
      }
      ta.value = '';
      state.busy = true;
      sendBtn.disabled = true;
      addMsg('user', md.esc(question).replace(/\n/g, '<br>'));
      var bubble = addMsg('assistant', '<span class="ee-cursor"></span>');
      var acc = '';
      var url = apiBase + '/api/docs/' + encodeURIComponent(docId) + '/ask';
      var payload = {
        question: question,
        para_id: cb.checked ? state.paraId : '',
        selection: state.selection || '',
        history: state.history.slice(-6)
      };
      try {
        var res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (!res.ok) {
          var t = await res.text();
          throw new Error('HTTP ' + res.status + ' ' + t.slice(0, 200));
        }
        var reader = res.body.getReader();
        var dec = new TextDecoder();
        var buf = '';
        while (true) {
          var chunk = await reader.read();
          if (chunk.done) break;
          buf += dec.decode(chunk.value, { stream: true });
          var lines = buf.split('\n');
          buf = lines.pop();
          for (var i = 0; i < lines.length; i++) {
            var line = lines[i].trim();
            if (!line || line.indexOf('data:') !== 0) continue;
            var data = line.slice(5).trim();
            if (data === '[DONE]') continue;
            var obj = null;
            try { obj = JSON.parse(data); } catch (e) { continue; }
            if (obj.error) throw new Error(obj.error);
            if (obj.delta) {
              acc += obj.delta;
              bubble.innerHTML = md.esc(acc).replace(/\n/g, '<br>') + '<span class="ee-cursor"></span>';
              body.scrollTop = body.scrollHeight;
            }
          }
        }
        bubble.innerHTML = '';
        md.richInto(bubble, acc || '（无内容）');
        if (EE.typeset) EE.typeset(bubble);
        state.history.push({ role: 'user', content: question });
        state.history.push({ role: 'assistant', content: acc });
      } catch (err) {
        var msg = String(err && err.message || err);
        var extra = /Failed to fetch|NetworkError|Load failed/.test(msg)
          ? '<p>连不上本地 EasyEssay 服务。请确认服务正在运行（双击 run.bat），或把本文件放回 EasyEssay 应用里打开。</p>'
          : '';
        bubble.parentNode.classList.add('err');
        bubble.innerHTML = '<p>出错了：' + md.esc(msg.slice(0, 400)) + '</p>' + extra;
      } finally {
        state.busy = false;
        sendBtn.disabled = false;
        body.scrollTop = body.scrollHeight;
        if (hint && !apiBase) hint.textContent = '';
      }
    }

    closeBtn.onclick = close;
    sendBtn.onclick = function () { ask(); };
    ta.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); }
    });

    welcome();
    node.hidden = true;

    return {
      mount: true, open: open, close: close, toggle: toggle, state: state, ask: ask,
      isHidden: function () { return !!node.hidden; }
    };
  }

  var instance = null;
  EE.askPanel = {
    mount: function (node, opts) {
      instance = create(Object.assign({ node: node }, opts || {}));
      EE.askPanel._i = instance;
      return instance;
    },
    open: function (ctx) { if (instance) instance.open(ctx); },
    close: function () { if (instance) instance.close(); },
    toggle: function () { if (instance) instance.toggle(); },
    isOpen: function () { return !!instance && !instance.isHidden(); }
  };
})(window);
