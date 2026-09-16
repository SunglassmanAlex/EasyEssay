"""在**真实浏览器**里跑一遍导出的 HTML，抓 JS 运行期错误。

## 为什么不能只靠 jsdom

实测踩坑：`(function(){ 'use strict'; undeclaredX = 1; })()` 在 jsdom 里
**不抛异常**（还会照常创建全局变量），而真实浏览器按规范抛 `ReferenceError`。
于是本地 `render_test.js`（jsdom）一路绿灯，用户打开阅读页却看到
「加载失败: prevPageEnd is not defined」—— jsdom 与真浏览器在这个点上行为不同。

所以凡是要"证明页面在浏览器里不报错"，就得用真浏览器跑。
jsdom 那套继续留着（它擅长断言 DOM 结构、快），但它**不能**证明"没有 JS 报错"。

## 做法

在页面 `<head>` 最前面注入一个错误收集器（必须最早，否则页面自己的脚本先跑、
错误发生时还没人监听），跑完把结果写进 `<div id="ee-jsreport">`，
再用 headless Chrome `--dump-dom` 把 DOM 取回来解析。

用法：
    python scripts/browser_check.py <导出.html> [--rows 期望段落数]
没找到浏览器时**跳过**（退出码 0），不让没有浏览器的环境（如 CI）失败。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]

COLLECTOR = """<script>
(function () {
  var errs = [], res = [];
  window.addEventListener('error', function (e) {
    var t = e.target;
    // 资源加载失败（script/link/img）没有 message，别当成 JS 错误
    if (t && t !== window && t.tagName && !e.message) {
      var u = t.src || t.href || '';
      res.push(t.tagName + ' ' + String(u).split('/').slice(-2).join('/'));
      return;
    }
    errs.push((e.message || (e.error && e.error.message) || 'unknown error')
              + ' @' + String(e.filename || '?').split('/').slice(-1)[0]
              + ':' + (e.lineno || 0));
  }, true);
  window.addEventListener('unhandledrejection', function (e) {
    errs.push('unhandledrejection: ' + ((e.reason && e.reason.message) || e.reason));
  });
  // 页面脚本跑完再看 DOM 状态，结论写进一个可供本脚本解析的节点
  setTimeout(function () {
    var d = document;
    var out = {
      errors: errs,
      resources: res,
      rows: d.querySelectorAll('.row[data-id]').length,
      tables: d.querySelectorAll('.tblbox table').length,
      fonts: d.querySelectorAll('.term, .term-en').length,
      pbreak: d.querySelectorAll('.row.pbreak, .pgmark').length,
      // 分页标记行必须**左右两栏都占位**（只在一侧标记会让分界线断掉）。
      // 参照稿的写法就是 `.row.pbreak` 里同时有 .en 与 .zh。
      pbreakOneSided: Array.prototype.filter.call(
        d.querySelectorAll('.row.pbreak'), function (r) {
          return !r.querySelector('.en') || !r.querySelector('.zh');
        }).length,
      failed: (d.querySelector('.load-error') ? d.querySelector('.load-error').textContent : '')
    };
    var box = d.createElement('div');
    box.id = 'ee-jsreport';
    box.textContent = JSON.stringify(out);
    d.body.appendChild(box);
  }, 2500);
})();
</script>
"""


def find_browser() -> str | None:
    for c in CANDIDATES:
        if Path(c).exists():
            return c
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    return None


def run(html_path: Path, expect_rows: int | None, budget_ms: int) -> int:
    browser = find_browser()
    if not browser:
        print("  （跳过：未找到 Chrome/Edge）")
        return 0

    src = html_path.read_text(encoding="utf-8")
    if "<head>" not in src:
        print(f"  ❌ 不是完整的 HTML（找不到 <head>）：{html_path}")
        return 1
    # 收集器必须插在 <head> 最前面，早于页面自己的任何脚本
    patched = src.replace("<head>", "<head>" + COLLECTOR, 1)

    # 检查副本要写在**原文件旁边**：导出页用相对路径取 ./vendor/mathjax/...，
    # 拷到临时目录会让这些资源 404（踩过 —— 那是我测试脚本的锅，不是页面的问题）。
    probe = html_path.with_name(html_path.stem + ".ee-check.html")
    probe.write_text(patched, encoding="utf-8")

    cmd = [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--hide-scrollbars", "--allow-file-access-from-files",
           f"--virtual-time-budget={budget_ms}", "--dump-dom",
           probe.resolve().as_uri()]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 浏览器执行失败：{exc}")
        probe.unlink(missing_ok=True)
        return 1

    m = re.search(r'<div id="ee-jsreport">(.*?)</div>', r.stdout or "", re.S)
    probe.unlink(missing_ok=True)
    if not m:
        print("  ❌ 页面没产出检测结果（脚本可能整体挂了）")
        if r.stderr.strip():
            print("    浏览器 stderr:", r.stderr.strip()[:300])
        return 1

    import html as _html

    data = json.loads(_html.unescape(m.group(1)))
    print(f"  浏览器：{Path(browser).name}")
    print(f"  段落行数 {data['rows']}，表格 {data['tables']}，术语高亮 {data['fonts']}"
          f"，换页标记 {data['pbreak']}")
    if data.get("resources"):
        print(f"  资源加载失败：{data['resources'][:4]}")
    if data.get("failed"):
        print(f"  页面自带错误提示：{data['failed']}")
    if data["errors"]:
        for e in data["errors"][:8]:
            print("  ❌ JS 错误:", e)
        return 1
    if expect_rows is not None and data["rows"] != expect_rows:
        print(f"  ❌ 段落行数不符：期望 {expect_rows}，实际 {data['rows']}")
        return 1
    # ⚠️ 这条断言的方向**在 2026-09-16 反转过**：
    # 早先用户明确要求"去掉换页提示"，所以当时断言 pbreak == 0；
    # 后来用户以参照成品稿为标准，而参照稿**有**分页标记行（.row.pbreak，13 条），
    # 导出页自己的说明文字也一直宣称"每两页之间有一条虚线分页标记行"。
    # 现在的要求是：**要有**，且每一条都必须左右两栏都占位。
    if data.get("pbreakOneSided"):
        print(f"  ❌ 有 {data['pbreakOneSided']} 条分页标记行只占一栏（分界线会断）")
        return 1
    print("  ✅ 真实浏览器里没有 JS 错误")
    return 0


def check_url(browser: str, url: str, budget_ms: int) -> int:
    """检查一个**在线页面**（例如应用的阅读页）。

    在线页面没法注入收集器（HTML 不由我们生成），所以退一步只看 DOM：
    段落行数、真实表格数、以及页面自己显示的错误提示。
    这足以抓住"整页加载失败"（那种情况下行数会是 0 或 1）。
    """
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--hide-scrollbars", f"--virtual-time-budget={budget_ms}", "--dump-dom", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=150)
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 浏览器执行失败：{exc}")
        return 1
    dom = r.stdout or ""
    if not dom.strip():
        print("  ❌ 浏览器没有返回 DOM")
        return 1
    rows = len(re.findall(r'data-id="p\d', dom))
    tables = dom.count("<table")
    empty = re.search(r'class="ee-empty-state"[^>]*>(.{0,160}?)</div>', dom, re.S)
    bad = re.search(r'load-error[^>]*>(.{0,160}?)</', dom, re.S)
    print(f"  URL：{url}")
    print(f"  段落行 {rows}，table {tables}，换页标记 "
          f"{dom.count('row pbreak') + dom.count('pgmark')}")
    if empty:
        print("  ⚠️ 页面提示:", re.sub(r"<[^>]+>", "", empty.group(1)).strip()[:120])
    if bad:
        print("  ❌ 页面显示错误:", re.sub(r"<[^>]+>", "", bad.group(1)).strip()[:160])
        return 1
    if rows == 0:
        print("  ❌ 一行都没渲染出来（整页加载失败）")
        return 1
    print("  ✅ 在线页面渲染正常")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="用真实浏览器检查页面是否报错")
    ap.add_argument("target", help="要检查的 HTML 文件，或 http(s):// 开头的页面地址")
    ap.add_argument("--rows", type=int, default=None, help="期望的段落行数（仅文件模式）")
    ap.add_argument("--budget", type=int, default=8000, help="虚拟时间预算（毫秒）")
    args = ap.parse_args()

    browser = find_browser()
    if not browser:
        print("  （跳过：未找到 Chrome/Edge）")
        return 0
    if args.target.startswith(("http://", "https://")):
        return check_url(browser, args.target, args.budget)
    path = Path(args.target)
    if not path.exists():
        print(f"文件不存在：{path}")
        return 1
    return run(path, args.rows, args.budget)


if __name__ == "__main__":
    raise SystemExit(main())
