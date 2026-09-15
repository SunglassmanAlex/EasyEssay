/* 首页（上传页）冒烟自测：jsdom 驱动真实页面，确认"首次使用填 Key"流程可用。
 *
 * 这是应用的入口页，且刚改过交互（去掉了登录，改成弹层填 API Key），所以留一条冒烟测试。
 * 覆盖：
 *   - 未配置 Key 时自动弹出「开始使用」，里面有 Key 输入框与"去申请"链接
 *   - 已配置 Key 时不弹层
 *   - 保存 Key 会 POST /api/settings，并随后自动测试连接
 *   - 页面无脚本错误
 *
 * 用法：EE_JSDOM=<jsdom 绝对路径> node scripts/ui_smoke.js
 */
const fs = require('fs');
const path = require('path');

let JSDOM, VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = require('jsdom'));
} catch (e) {
  const p = process.env.EE_JSDOM;
  if (!p) { console.error('未找到 jsdom，请用 EE_JSDOM=<路径> 指定'); process.exit(3); }
  ({ JSDOM, VirtualConsole } = require(path.resolve(p)));
}

const ROOT = path.resolve(__dirname, '..');
const PASS = [], FAIL = [];
const check = (n, ok, d) => {
  (ok ? PASS : FAIL).push(n);
  console.log((ok ? '  ✅ ' : '  ❌ ') + n + (d ? '  —— ' + d : ''));
};

function load({ hasKey }) {
  const routes = {
    '/api/settings': { api_key_set: hasKey, model: 'deepseek-chat', base_url: '', target_lang: '简体中文',
                       temperature: 1, translate_batch_size: 8, context_paragraphs: 2, fix_formula: true,
                       restore_original: true, system_prompt: 'x', ask_system_prompt: 'y',
                       default_prompts: { translate: 'x', ask: 'y' }, ocr_engines: [] },
    '/api/health': { ok: true, engines: [] },
    '/api/docs': [],
    '/api/settings/test': { ok: true, reply: 'pong' }
  };
  const calls = [];
  const vc = new VirtualConsole();
  const errors = [];
  vc.on('jsdomError', e => { if (!/Not implemented: navigation/.test(e.message || '')) errors.push(e.message); });
  vc.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));
  // jsdom 不加载 <script src>，而 app.js 会操作 DOM（必须在文档解析后执行），
  // 所以把三个脚本**内联进 HTML**，让它们按原本的文档顺序运行。
  let html = fs.readFileSync(path.join(ROOT, 'web', 'index.html'), 'utf8');
  ['js/api.js', 'js/md.js', 'js/app.js'].forEach(f => {
    const src = fs.readFileSync(path.join(ROOT, 'web', f), 'utf8');
    const tag = new RegExp('<script src="/static/' + f + '"></script>');
    if (!tag.test(html)) throw new Error('index.html 里找不到脚本标签：' + f);
    html = html.replace(tag, '<script>\n' + src + '\n</script>');
  });
  const dom = new JSDOM(html, {
    runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://127.0.0.1:8765/',
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = (url, opt) => {
        const body = opt && opt.body ? JSON.parse(opt.body) : null;
        calls.push({ url: String(url), method: (opt && opt.method) || 'GET', body });
        const data = routes[String(url)] !== undefined ? routes[String(url)] : {};
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve(data),
          text: () => Promise.resolve(JSON.stringify(data))
        });
      };
    }
  });
  return new Promise(r => setTimeout(() => r({ w: dom.window, d: dom.window.document, calls, errors }), 400));
}

(async function main() {
  console.log('\n== 首页 · 未配置 API Key ==');
  {
    const { w, d, calls, errors } = await load({ hasKey: false });
    const welcome = d.getElementById('welcome');
    check('自动弹出「开始使用」', welcome && !welcome.hidden);
    check('有 Key 输入框', !!d.getElementById('w-key'));
    check('有"去申请"链接', /platform\.deepseek\.com/.test(d.documentElement.innerHTML));
    check('顶栏提示尚未配置 Key', /尚未配置 API Key/.test(d.getElementById('engine-note').textContent));
    check('页面无脚本错误', errors.length === 0, errors.join(' | '));

    // 保存 Key
    d.getElementById('w-key').value = 'sk-test-key-123456';
    d.getElementById('w-save').click();
    await new Promise(r => setTimeout(r, 150));
    const post = calls.find(c => c.method === 'POST' && c.url === '/api/settings');
    check('保存会 POST /api/settings', !!post, JSON.stringify(calls.map(c => c.method + ' ' + c.url)));
    check('提交内容包含 api_key', !!post && post.body && post.body.api_key === 'sk-test-key-123456');
    check('保存后自动测试连接',
      calls.some(c => c.url === '/api/settings/test'),
      JSON.stringify(calls.map(c => c.method + ' ' + c.url)));
    check('保存后弹层不再挡住操作', /连接正常|已保存/.test(d.getElementById('w-status').textContent)
      || !d.getElementById('w-status').hidden);
  }

  console.log('\n== 首页 · 已配置 API Key ==');
  {
    const { d, errors } = await load({ hasKey: true });
    const welcome = d.getElementById('welcome');
    check('不再弹出「开始使用」', welcome.hidden);
    check('顶栏显示模型名', /deepseek/.test(d.getElementById('engine-note').textContent),
      d.getElementById('engine-note').textContent);
    check('页面无脚本错误', errors.length === 0, errors.join(' | '));
  }

  console.log(`\n== 结果：${PASS.length} 项通过，${FAIL.length} 项失败 ==`);
  if (FAIL.length) { console.log('失败项：' + FAIL.join('、')); process.exit(1); }
})().catch(e => { console.error('测试脚本异常：', e); process.exit(2); });
