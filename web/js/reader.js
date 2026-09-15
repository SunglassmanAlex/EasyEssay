/* EasyEssay · 阅读页：加载文档、控制翻译进度、目录、快捷键 */
(function () {
  'use strict';
  var api = EasyEssay.api;
  var $ = function (id) { return document.getElementById(id); };

  var docId = new URLSearchParams(location.search).get('doc') || '';
  var readerEl = $('reader');
  var API = window.EASYESSay_API || '';
  var bapi = null;
  var pollTimer = null;
  var lastStatus = '';

  if (!docId) {
    readerEl.innerHTML = '<div class="ee-empty-state">缺少 <code>?doc=</code> 参数。' +
      '请从<a href="/">首页</a>的文档库进入。</div>';
    return;
  }

  function setProgress(p) {
    $('progress-bar').style.width = Math.round((p || 0) * 100) + '%';
  }

  function render(meta, paragraphs, translations, running) {
    var stats = meta.stats || {};
    document.title = (meta.title || 'EasyEssay') + ' · 中英对照';
    $('doc-title').textContent = meta.title || docId;
    $('doc-title').title = meta.title || docId;
    var notes = [];
    if (meta.page_count) notes.push(meta.page_count + ' 页');
    notes.push((stats.paragraphs || paragraphs.length) + ' 段');
    notes.push('已译 ' + (stats.translated || Object.keys(translations).length) + ' 段');
    var nBuilt = Object.keys(translations).filter(function (k) {
      return translations[k] && translations[k].en;
    }).length;
    if (nBuilt) notes.push('左栏重建 ' + nBuilt + ' 段');
    if (meta.settings && meta.settings.model) notes.push(meta.settings.model);
    if (meta.ocr_pages && meta.ocr_pages.length) notes.push('OCR ' + meta.ocr_pages.length + ' 页');
    if (meta.status === 'extracting') notes.push('解析中…');
    $('doc-note').textContent = notes.join(' · ');
    $('btn-source').href = API + '/api/docs/' + encodeURIComponent(docId) + '/source';
    $('btn-export').onclick = function () {
      // 带上「限定本文档」的回连令牌：导出文件用 file:// 打开后仍可问 AI。
      // 令牌只能访问这一篇文档，可在「账号」页随时撤销。
      location.href = API + '/api/docs/' + encodeURIComponent(docId) + '/export?with_token=1';
    };

    if (!paragraphs.length) {
      readerEl.innerHTML = '<div class="ee-empty-state">这篇文档没有抽取到任何文本。' +
        '可能是扫描件而 OCR 未安装，或页码范围设置过窄。' +
        '可回到<a href="/">首页</a>重新上传（勾选 OCR，或安装 <code>requirements-ocr.txt</code> 后重试）。</div>';
      return;
    }

    bapi = EasyEssay.renderBilingual(readerEl, {
      paragraphs: paragraphs,
      translations: translations,
      id: docId
    }, {
      onAsk: function (payload) {
        EasyEssay.askPanel.open({
          paraId: payload.paraId,
          selection: payload.selection,
          side: payload.side,
          question: payload.question
        });
      }
    });

    buildToc(bapi.toc);
    setProgress(meta.progress || (stats.paragraphs ? (stats.translated / stats.paragraphs) : 0));
    toggleRunning(!!running || meta.status === 'translating' || meta.status === 'restoring');
  }

  function toggleRunning(run) {
    $('btn-stop').hidden = !run;
    $('btn-translate').textContent = run ? '翻译中…' : (lastStatus === 'ready' ? '重新翻译' : '继续翻译');
    $('btn-translate').disabled = run;
    $('btn-restore').disabled = run;
    if (run) $('btn-translate').classList.remove('primary');
    else $('btn-translate').classList.add('primary');
  }

  function buildToc(toc) {
    var box = $('toc');
    box.innerHTML = '<h4>目录</h4>';
    if (!toc || !toc.length) {
      box.innerHTML += '<div class="ee-help">未识别出章节标题。</div>';
      return;
    }
    toc.forEach(function (t) {
      var a = document.createElement('a');
      a.href = '#para-' + t.id;
      a.className = 'lv' + (t.level === 2 ? 2 : 1);
      a.textContent = t.label;
      a.onclick = function () { $('toc').hidden = true; };
      box.appendChild(a);
    });
  }

  $('btn-toc').onclick = function () { $('toc').hidden = !$('toc').hidden; };

  // ------------------------------------------------------------ 加载 / 轮询
  var lastTranslated = -1;
  var lastBuilt = -1;

  function load(keepScroll) {
    var y = window.scrollY;
    return api.get('/api/docs/' + encodeURIComponent(docId)).then(function (d) {
      var meta = d.meta || {};
      lastStatus = meta.status;
      var translations = d.translations || {};
      var n = Object.keys(translations).length;
      var nBuilt = Object.keys(translations).filter(function (k) {
        return translations[k] && translations[k].en;
      }).length;
      var needRender = !bapi || n !== lastTranslated || meta.status === 'extracting'
        || nBuilt !== lastBuilt;
      lastTranslated = n;
      lastBuilt = nBuilt;
      if (needRender) {
        render(meta, d.paragraphs || [], translations, d.running);
        if (keepScroll) window.scrollTo({ top: y });
      } else {
        $('doc-note').textContent = [
          meta.page_count ? meta.page_count + ' 页' : '',
          ((meta.stats || {}).paragraphs || 0) + ' 段',
          '已译 ' + n + ' 段',
          nBuilt ? '左栏重建 ' + nBuilt + ' 段' : '',
          meta.message || ''
        ].filter(Boolean).join(' · ');
        setProgress(meta.progress || 0);
        toggleRunning(!!d.running);
      }
      var busy = d.running || ['translating', 'extracting', 'restoring'].indexOf(meta.status) >= 0;
      clearTimeout(pollTimer);
      if (busy) pollTimer = setTimeout(function () { load(true); }, 1500);
      return d;
    }).catch(function (e) {
      readerEl.innerHTML = '<div class="ee-empty-state">加载失败：' + EasyEssay.md.esc(e.message) + '</div>';
    });
  }

  $('btn-translate').onclick = function () {
    var force = lastStatus === 'ready' || lastStatus === 'partial';
    if (force && !confirm('重新翻译全部段落？已有译文会被覆盖。')) return;
    api.post('/api/docs/' + encodeURIComponent(docId) + '/translate', { force: force })
      .then(function () {
        api.toast('已开始翻译，可随时阅读已译部分');
        toggleRunning(true);
        setTimeout(function () { load(true); }, 800);
      })
      .catch(function (e) { api.toast(e.message, true); });
  };

  $('btn-stop').onclick = function () {
    api.post('/api/docs/' + encodeURIComponent(docId) + '/stop', {})
      .then(function () { api.toast('已请求停止'); load(true); })
      .catch(function (e) { api.toast(e.message, true); });
  };

  $('btn-restore').onclick = function () {
    if (!confirm('只重建左栏公式（不重译，已有译文保持不变）？\n\n这一步会重新调用模型，但输出只有重建后的原文，花费远低于重新翻译。')) return;
    api.post('/api/docs/' + encodeURIComponent(docId) + '/restore', {})
      .then(function () { api.toast('已开始重建左栏公式'); toggleRunning(true); setTimeout(function () { load(true); }, 800); })
      .catch(function (e) { api.toast(e.message, true); });
  };

  // ------------------------------------------------------------ 快捷键
  document.addEventListener('keydown', function (e) {
    var tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'textarea' || tag === 'input' || e.target.isContentEditable) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'j' || e.key === 'n') { scrollStep(1); e.preventDefault(); }
    else if (e.key === 'k' || e.key === 'p') { scrollStep(-1); e.preventDefault(); }
    else if (e.key === '[') { if (bapi) bapi.setScale(bapi.state.scale - 0.1); }
    else if (e.key === ']') { if (bapi) bapi.setScale(bapi.state.scale + 0.1); }
    else if (e.key === 't') { $('toc').hidden = !$('toc').hidden; }
  });

  function scrollStep(dir) {
    var rows = Array.prototype.slice.call(document.querySelectorAll('.brow'));
    if (!rows.length) return;
    var cur = 0;
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].getBoundingClientRect().top <= 140) cur = i;
    }
    var target = rows[Math.min(rows.length - 1, Math.max(0, cur + dir))];
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  // ------------------------------------------------------------ 启动
  EasyEssay.askPanel.mount($('ask'), { docId: docId, apiBase: API });
  load(false);
})();
