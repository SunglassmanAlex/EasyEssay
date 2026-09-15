/* EasyEssay · 首页逻辑：上传 / 文档库 / 设置 */
(function () {
  'use strict';
  var api = EasyEssay.api;
  var $ = function (id) { return document.getElementById(id); };
  var settingsCache = null;
  var pollTimer = null;

  // ------------------------------------------------------------ 主题
  try { if (localStorage.getItem('ee-theme') === 'dark') document.body.classList.add('ee-dark'); } catch (e) { }
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-act="toggle-theme"]');
    if (!b) return;
    document.body.classList.toggle('ee-dark');
    try { localStorage.setItem('ee-theme', document.body.classList.contains('ee-dark') ? 'dark' : 'light'); } catch (e2) { }
  });

  function badge(status) {
    var map = {
      extracting: ['run', '解析中'],
      extracted: ['warn', '待翻译'],
      translating: ['run', '翻译中'],
      restoring: ['run', '重建左栏中'],
      ready: ['ok', '已完成'],
      partial: ['warn', '部分完成'],
      error: ['err', '出错']
    };
    var m = map[status] || ['', status || '未知'];
    return '<span class="ee-badge ' + m[0] + '">' + m[1] + '</span>';
  }

  function esc(s) { return EasyEssay.md.esc(s); }

  // ------------------------------------------------------------ 文档库
  function renderDocs(docs) {
    var box = $('doclist');
    $('doc-count').textContent = docs.length ? '（' + docs.length + ' 篇）' : '';
    if (!docs.length) {
      box.innerHTML = '<div class="ee-help">还没有文档。先上传一个 PDF 试试。</div>';
      return;
    }
    box.innerHTML = '';
    docs.forEach(function (d) {
      var st = d.stats || {};
      var item = document.createElement('div');
      item.className = 'ee-docitem';
      var running = ['translating', 'extracting', 'restoring'].indexOf(d.status) >= 0;
      var acts = [];
      acts.push('<a class="ee-btn" href="/reader?doc=' + encodeURIComponent(d.id) + '">打开阅读</a>');
      if (d.status === 'extracted' || d.status === 'partial') {
        acts.push('<button class="ee-btn primary" data-act="translate" data-id="' + d.id + '" data-force="0">开始翻译</button>');
      } else if (d.status === 'ready') {
        acts.push('<button class="ee-btn" data-act="translate" data-id="' + d.id + '" data-force="1">重新翻译</button>');
        if ((st.translated || 0) > (st.rebuilt || 0)) {
          acts.push('<button class="ee-btn" data-act="restore" data-id="' + d.id + '">重建左栏公式</button>');
        }
      } else if (d.status === 'translating' || d.status === 'restoring') {
        acts.push('<button class="ee-btn" data-act="stop" data-id="' + d.id + '">停止</button>');
      } else if (d.status === 'error') {
        acts.push('<button class="ee-btn" data-act="translate" data-id="' + d.id + '" data-force="0">重试翻译</button>');
      }
      acts.push('<a class="ee-btn" href="/api/docs/' + encodeURIComponent(d.id) + '/export" target="_blank">导出 HTML</a>');
      acts.push('<button class="ee-btn" data-act="reextract" data-id="' + d.id + '" title="用最新抽取器重排版面（页码栏/分页标记/表格），已译内容按内容锚点保留">重新抽取</button>');
      acts.push('<a class="ee-btn" href="/api/docs/' + encodeURIComponent(d.id) + '/source" target="_blank">原始文件</a>');
      acts.push('<button class="ee-btn ghost" data-act="del" data-id="' + d.id + '">删除</button>');

      item.innerHTML =
        '<div class="main">' +
        '<div class="t">' + esc(d.title || d.id) + '</div>' +
        '<div class="m">' + badge(d.status) +
        (d.page_count ? '<span class="ee-badge">' + d.page_count + ' 页</span>' : '') +
        '<span class="ee-badge">' + (st.translated || 0) + '/' + (st.paragraphs || 0) + ' 段</span>' +
        (d.created_at ? '<span class="ee-badge">' + esc(d.created_at) + '</span>' : '') +
        (d.message ? '<span>' + esc(d.message) + '</span>' : '') +
        (d.error ? '<span style="color:#dc2626">' + esc(String(d.error).slice(0, 120)) + '</span>' : '') +
        '</div>' +
        '<div class="ee-mini-progress"><i style="width:' + Math.round((d.progress || 0) * 100) + '%"></i></div>' +
        '</div>' +
        '<div class="acts">' + acts.join('') + '</div>';
      box.appendChild(item);
    });
  }

  function loadDocs() {
    return api.get('/api/docs').then(function (docs) {
      renderDocs(docs || []);
      var busy = (docs || []).some(function (d) {
        return ['translating', 'extracting', 'restoring'].indexOf(d.status) >= 0;
      });
      schedulePoll(busy);
      return docs;
    }).catch(function (e) {
      $('doclist').innerHTML = '<div class="ee-help">读取文档库失败：' + esc(e.message) + '</div>';
    });
  }

  function schedulePoll(active) {
    clearTimeout(pollTimer);
    if (active) pollTimer = setTimeout(loadDocs, 2000);
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-act]');
    if (!btn) return;
    var act = btn.dataset.act, id = btn.dataset.id;
    if (act === 'translate') {
      var force = btn.dataset.force === '1';
      if (force && !confirm('重新翻译全部段落？已有的译文会被覆盖。')) return;
      btn.disabled = true;
      api.post('/api/docs/' + encodeURIComponent(id) + '/translate', { force: force })
        .then(function () { api.toast(force ? '已开始重新翻译' : '已开始翻译，可随时关闭页面'); loadDocs(); })
        .catch(function (err) { api.toast(err.message, true); btn.disabled = false; });
    } else if (act === 'reextract') {
      if (!confirm('用最新抽取器重新抽取？（段落可能被合并，已译内容会按内容自动保留）')) return;
      btn.disabled = true;
      api.post('/api/docs/' + encodeURIComponent(id) + '/reextract', {})
        .then(function () { api.toast('已开始重新抽取'); loadDocs(); })
        .catch(function (err) { api.toast(err.message, true); btn.disabled = false; });
    } else if (act === 'restore') {
      if (!confirm('只重建左栏公式（不重译，已有译文保持不变）？')) return;
      btn.disabled = true;
      api.post('/api/docs/' + encodeURIComponent(id) + '/restore', { force: false })
        .then(function () { api.toast('已开始重建左栏公式'); loadDocs(); })
        .catch(function (err) { api.toast(err.message, true); btn.disabled = false; });
    } else if (act === 'stop') {
      api.post('/api/docs/' + encodeURIComponent(id) + '/stop', {})
        .then(function () { api.toast('已请求停止'); loadDocs(); })
        .catch(function (err) { api.toast(err.message, true); });
    } else if (act === 'del') {
      if (!confirm('删除这篇文档及其译文？此操作不可撤销。')) return;
      api.del('/api/docs/' + encodeURIComponent(id))
        .then(function () { api.toast('已删除'); loadDocs(); })
        .catch(function (err) { api.toast(err.message, true); });
    }
  });

  $('btn-refresh').onclick = function () { loadDocs(); api.toast('已刷新'); };

  // ------------------------------------------------------------ 上传
  var drop = $('drop'), fileInput = $('file');
  drop.onclick = function () { fileInput.click(); };
  $('btn-pick').onclick = function () { fileInput.click(); };
  fileInput.onchange = function () { if (fileInput.files[0]) doUpload(fileInput.files[0]); };

  ['dragenter', 'dragover'].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add('over'); });
  });
  ['dragleave', 'drop'].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove('over'); });
  });
  drop.addEventListener('drop', function (e) {
    var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) doUpload(f);
  });

  function statusLine(html) {
    var box = $('up-status');
    box.hidden = false;
    box.innerHTML = html;
  }

  function doUpload(file) {
    if (!/\.(pdf|png|jpe?g|webp|bmp)$/i.test(file.name)) {
      api.toast('只支持 PDF 与图片文件', true);
      return;
    }
    var fields = {
      title: $('up-title').value.trim(),
      page_from: parseInt($('up-from').value || '0', 10) || 0,
      page_to: parseInt($('up-to').value || '0', 10) || 0,
      use_ocr: $('up-ocr').checked ? 'true' : 'false'
    };
    statusLine('正在上传 <b>' + esc(file.name) + '</b> …');
    api.upload(file, fields, function (p) {
      statusLine('正在上传 <b>' + esc(file.name) + '</b> … ' + Math.round(p * 100) + '%');
    }).then(function (res) {
      statusLine('已上传，正在解析版面与公式…（文档 id: <code>' + esc(res.id) + '</code>）');
      api.toast('上传成功，正在解析');
      fileInput.value = '';
      pollDoc(res.id, 0);
    }).catch(function (e) {
      statusLine('上传失败：' + esc(e.message));
      api.toast(e.message, true);
    });
  }

  function pollDoc(id, n) {
    api.get('/api/docs/' + encodeURIComponent(id) + '?with_text=false').then(function (d) {
      var status = (d.meta || {}).status;
      if (status === 'extracting' && n < 900) {
        statusLine('正在解析 <b>' + esc((d.meta || {}).title || '') + '</b> …');
        setTimeout(function () { pollDoc(id, n + 1); }, 1200);
        return;
      }
      if (status === 'error') {
        statusLine('解析失败：' + esc((d.meta || {}).error || '未知错误'));
      } else {
        var stats = (d.meta || {}).stats || {};
        statusLine('解析完成：共 <b>' + (stats.paragraphs || 0) + '</b> 段。' +
          '<a href="/reader?doc=' + encodeURIComponent(id) + '">进入阅读页</a>，或直接开始翻译。');
      }
      loadDocs();
    }).catch(function (e) {
      statusLine('查询状态失败：' + esc(e.message));
    });
  }

  // ------------------------------------------------------------ 设置
  function openSettings() {
    api.get('/api/settings').then(function (s) {
      settingsCache = s;
      $('s-key').value = '';
      $('s-key').placeholder = s.api_key_set ? ('已配置：' + s.api_key_masked) : 'sk-...';
      $('s-base').value = s.base_url || '';
      $('s-model').value = s.model || '';
      $('s-askmodel').value = s.ask_model || '';
      $('s-lang').value = s.target_lang || '';
      $('s-temp').value = s.temperature;
      $('s-batch').value = s.translate_batch_size;
      $('s-ctx').value = s.context_paragraphs;
      $('s-fix').checked = !!s.fix_formula;
      $('s-restore').checked = s.restore_original !== false;
      $('s-prompt').value = s.system_prompt || '';
      $('s-askprompt').value = s.ask_system_prompt || '';
      $('engine-note').textContent = s.api_key_set
        ? ('API 已配置 · 模型 ' + (s.model || ''))
        : '尚未配置 API Key';
      $('settings').hidden = false;
    }).catch(function (e) { api.toast(e.message, true); });
  }

  $('btn-settings').onclick = openSettings;
  $('s-cancel').onclick = function () { $('settings').hidden = true; };
  $('settings').addEventListener('click', function (e) {
    if (e.target === $('settings')) $('settings').hidden = true;
  });

  $('s-reset').onclick = function () {
    if (!settingsCache) return;
    $('s-prompt').value = settingsCache.default_prompts.translate;
    $('s-askprompt').value = settingsCache.default_prompts.ask;
  };

  function collect() {
    var p = {
      base_url: $('s-base').value.trim(),
      model: $('s-model').value.trim(),
      ask_model: $('s-askmodel').value.trim(),
      target_lang: $('s-lang').value.trim(),
      temperature: parseFloat($('s-temp').value),
      translate_batch_size: parseInt($('s-batch').value, 10),
      context_paragraphs: parseInt($('s-ctx').value, 10),
      fix_formula: $('s-fix').checked,
      restore_original: $('s-restore').checked,
      system_prompt: $('s-prompt').value,
      ask_system_prompt: $('s-askprompt').value
    };
    var k = $('s-key').value.trim();
    if (k) p.api_key = k;
    return p;
  }

  $('s-save').onclick = function () {
    api.post('/api/settings', collect()).then(function () {
      api.toast('设置已保存');
      $('settings').hidden = true;
      loadDocs();
    }).catch(function (e) { api.toast(e.message, true); });
  };

  $('s-test').onclick = function () {
    var btn = $('s-test');
    btn.disabled = true; btn.textContent = '测试中…';
    api.post('/api/settings/test', collect()).then(function (r) {
      if (r.ok) {
        api.toast('连接正常，模型回复：' + (r.reply || 'ok') +
          (r.models && r.models.length ? '（可用模型 ' + r.models.join(', ') + '）' : ''));
        api.post('/api/settings', collect()).catch(function () { });
      } else {
        api.toast('连接失败：' + r.error, true);
      }
    }).catch(function (e) { api.toast(e.message, true); })
      .finally(function () { btn.disabled = false; btn.textContent = '测试连接'; });
  };

  // ------------------------------------------------------------ 启动
  // ------------------------------------------------------------ 首次使用：填 API Key
  // 没有账号概念：打开页面后填一次自己的 DeepSeek Key 即可（存在本机）。
  function openWelcome(settings) {
    $('welcome').hidden = false;
    $('w-model').value = (settings && settings.model) || 'deepseek-chat';
    $('w-key').value = '';
    setTimeout(function () { $('w-key').focus(); }, 50);
  }

  function saveWelcome() {
    var key = $('w-key').value.trim();
    if (!key) { api.toast('请先填 API Key', true); return; }
    if (key.indexOf('sk-') !== 0) api.toast('提示：DeepSeek 的 Key 一般以 sk- 开头');
    var btn = $('w-save');
    btn.disabled = true; btn.textContent = '保存中…';
    api.post('/api/settings', { api_key: key, model: $('w-model').value.trim() || undefined })
      .then(function () {
        $('w-status').hidden = false;
        $('w-status').innerHTML = '已保存到本机。正在测试连接…';
        return api.post('/api/settings/test', {});
      })
      .then(function (r) {
        if (r.ok) {
          $('w-status').innerHTML = '连接正常（' + (r.reply || 'ok') + '），开始用吧。';
          api.toast('API Key 已保存');
          setTimeout(function () { $('welcome').hidden = true; }, 600);
        } else {
          $('w-status').innerHTML = '已保存，但测试失败：' + (r.error || '') +
            '<br>可以到「设置」里改，或检查网络。';
        }
      })
      .catch(function (e) { api.toast(e.message, true); })
      .finally(function () { btn.disabled = false; btn.textContent = '保存并开始'; });
  }

  $('w-save').onclick = saveWelcome;
  $('w-later').onclick = function () { $('welcome').hidden = true; };
  $('w-key').addEventListener('keydown', function (e) { if (e.key === 'Enter') saveWelcome(); });

  api.get('/api/health').then(function (h) {
    var e = (h.engines || []);
    $('engine-note').textContent = e.length ? ('OCR 引擎：' + e.join('/')) : '未装 OCR（扫描件与图片暂不可用）';
  }).catch(function () { });

  // 没有配置 Key → 直接引导填一次（这是唯一的"登录"动作）
  api.get('/api/settings').then(function (s) {
    settingsCache = s;
    $('engine-note').textContent = s.api_key_set
      ? ('模型 ' + (s.model || 'deepseek-chat'))
      : '尚未配置 API Key';
    if (!s.api_key_set) openWelcome(s);
  }).catch(function () { });

  loadDocs();
})();
