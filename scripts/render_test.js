/* 无头渲染自测：用 jsdom 真实执行导出的离线 HTML，
   验证双栏结构、术语高亮、公式片段与视图切换是否正常。
   用法： node scripts/render_test.js samples/demo-plonk/xxx.html        */
const fs = require('fs');

// jsdom 可能装在别处（例如隔离的 node 工作区），允许用 EE_JSDOM 指定绝对路径
let JSDOM, VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = require('jsdom'));
} catch (e) {
  const p = process.env.EE_JSDOM;
  if (!p) {
    console.error('未找到 jsdom。请先 `npm install jsdom`，或用 EE_JSDOM=<jsdom 绝对路径> 指定。');
    process.exit(3);
  }
  ({ JSDOM, VirtualConsole } = require(p));
}

const file = process.argv[2];
if (!file) {
  console.error('用法: node scripts/render_test.js <导出.html>');
  process.exit(2);
}

const html = fs.readFileSync(file, 'utf8');
const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => errors.push('jsdomError: ' + (e.message || e)));
vc.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  virtualConsole: vc
});

setTimeout(() => {
  const d = dom.window.document;
  const rows = d.querySelectorAll('.wrap .row[data-id]');
  const en = d.querySelectorAll('.wrap .row[data-id] > .en');
  const zh = d.querySelectorAll('.wrap .row[data-id] > .zh');
  const pbreak = d.querySelectorAll('.wrap .row.pbreak');
  const pgmarks = d.querySelectorAll('.wrap .pgmark');
  const gutters = Array.from(rows).filter(n => /^p\.\d+/.test(n.dataset.pg || '')).length;
  const terms = d.querySelectorAll('.term, .term-en');
  const pending = d.querySelectorAll('.bcell.zh .empty');
  const rebuilt = d.querySelectorAll('.bcell.en[data-rebuilt="1"]');
  const mathish = Array.from(en).filter(n => /\$[^$]+\$/.test(n.textContent)).length;

  // 数据里若有「重建原文」，渲染就必须体现出来（en 与原文不同才算重建）
  let withEn = 0;
  try {
    const data = JSON.parse(d.getElementById('ee-doc-data').textContent);
    const raw = {};
    (data.paragraphs || []).forEach(p => { raw[p.id] = p.text; });
    withEn = Object.keys(data.translations || {}).filter(k => {
      const t = data.translations[k];
      return t && typeof t.en === 'string' && t.en.trim() && t.en !== raw[k];
    }).length;
  } catch (e) { /* 忽略 */ }

  const problems = [];
  if (!rows.length) problems.push('没有渲染出任何段落行');
  if (rows.length !== en.length || rows.length !== zh.length) {
    problems.push('左右栏数量不一致: rows=' + rows.length + ' en=' + en.length + ' zh=' + zh.length);
  }
  if (zh.length && !zh[0].textContent.trim()) problems.push('第一个译栏为空');
  if (terms.length === 0) problems.push('术语高亮没有生效');
  if (mathish === 0) problems.push('没有识别到任何公式片段');
  if (pending.length > 0) problems.push('存在 ' + pending.length + ' 个「待翻译」单元格');
  if (gutters !== rows.length) problems.push('页码栏缺失：' + gutters + '/' + rows.length);
  if (!d.querySelector('.paper-head')) problems.push('缺少论文头部信息块');
  if (!d.querySelector('.colhead')) problems.push('缺少两栏栏头');
  if (!d.querySelector('.row.tiny:last-of-type')) problems.push('缺少页尾');
  if (withEn > 0 && rebuilt.length === 0) {
    problems.push('数据里有 ' + withEn + ' 段「重建原文」，但前端没有用上');
  }

  // 视图切换 + 「原始抽取」切换
  let viewOk = false, rawOk = false;
  try {
    const api = dom.window.EasyEssay.__api;
    api.setViewMode('en');
    viewOk = d.querySelector('#ee-reader').classList.contains('view-en');
    api.setViewMode('all');
    if (rebuilt.length) {
      d.querySelector('[data-act="toggle-raw"]').click();
      rawOk = d.querySelector('#ee-reader').classList.contains('show-raw');
      api.toggleRaw(false);          // 切回默认（重建原文）视图再打印样例
    } else { rawOk = true; }
  } catch (e) { problems.push('视图/原文切换异常: ' + e.message); }

  console.log('段落行数      :', rows.length);
  console.log('英文栏 / 译栏  :', en.length, '/', zh.length);
  console.log('术语高亮节点   :', terms.length);
  console.log('含公式的英文段 :', mathish);
  console.log('待翻译单元格   :', pending.length);
  console.log('重建原文段落   :', rebuilt.length, '（数据中 ' + withEn + ' 段）');
  console.log('页码栏 / 分页行 :', gutters + '/' + rows.length, '/', pbreak.length, '（段内换页点 ' + pgmarks.length + '）');
  console.log('表格 / 公式框   :', d.querySelectorAll('.tblbox table').length, '/', d.querySelectorAll('.eq').length);
  console.log('视图切换       :', viewOk ? 'OK' : 'FAIL');
  console.log('原文/重建切换  :', rawOk ? 'OK' : 'FAIL');
  console.log('第1段英文       :', (en[0] ? en[0].textContent.trim().slice(0, 60) : ''));
  console.log('第1段中文       :', (zh[0] ? zh[0].textContent.trim().slice(0, 60) : ''));
  const withMath = Array.from(en).find(n => /\$[^$]+\$/.test(n.textContent));
  console.log('公式片段样例   :', withMath ? withMath.textContent.trim().slice(0, 90) : '（无）');
  if (rebuilt.length) {
    const demo = rebuilt[0];
    console.log('重建原文样例   :', demo.textContent.trim().slice(0, 140));
  }
  if (errors.length) console.log('运行期错误     :\n  ' + errors.join('\n  '));

  if (problems.length) {
    console.log('\n❌ 问题：\n  - ' + problems.join('\n  - '));
    process.exit(1);
  }
  console.log('\n✅ 渲染自测通过');
  process.exit(0);
}, 1200);
