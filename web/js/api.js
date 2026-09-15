/* EasyEssay · 与本地后端通信的薄封装

这是一个**本机单用户**应用：没有账号、没有登录，API Key 存在本机
data/settings.json 里（也可用环境变量 DEEPSEEK_API_KEY）。这里只做一层薄封装。
*/
(function (global) {
  'use strict';
  var EE = global.EasyEssay = global.EasyEssay || {};

  function j(method, url, body) {
    var opt = { method: method, headers: {}, credentials: 'same-origin' };
    if (body !== undefined) {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    }
    return fetch(url, opt).then(function (r) {
      return r.text().then(function (t) {
        var data = null;
        try { data = t ? JSON.parse(t) : null; } catch (e) { data = { raw: t }; }
        if (!r.ok) {
          var msg = (data && (data.detail || data.error)) || t || ('HTTP ' + r.status);
          throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
        }
        return data;
      });
    });
  }

  var toastTimer = null;
  function toast(msg, isErr) {
    var box = document.getElementById('toast');
    if (!box) { alert(msg); return; }
    box.textContent = msg;
    box.className = 'ee-toast show' + (isErr ? ' err' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { box.className = 'ee-toast'; }, isErr ? 6000 : 2600);
  }

  EE.api = {
    get: function (u) { return j('GET', u); },
    post: function (u, b) { return j('POST', u, b || {}); },
    put: function (u, b) { return j('PUT', u, b || {}); },
    del: function (u) { return j('DELETE', u); },
    toast: toast,
    fmtTime: function (s) { return s || ''; },
    upload: function (file, fields, onProgress) {
      return new Promise(function (resolve, reject) {
        var fd = new FormData();
        fd.append('file', file);
        Object.keys(fields || {}).forEach(function (k) { fd.append(k, fields[k]); });
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/upload');
        xhr.withCredentials = true;
        xhr.upload.onprogress = function (e) {
          if (onProgress && e.lengthComputable) onProgress(e.loaded / e.total);
        };
        xhr.onload = function () {
          var data = null;
          try { data = JSON.parse(xhr.responseText); } catch (e) { }
          if (xhr.status >= 200 && xhr.status < 300) resolve(data);
          else reject(new Error((data && (data.detail || data.error)) || ('HTTP ' + xhr.status)));
        };
        xhr.onerror = function () { reject(new Error('网络错误，请确认服务已启动')); };
        xhr.send(fd);
      });
    }
  };
})(window);
