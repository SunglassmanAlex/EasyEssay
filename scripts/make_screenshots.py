#!/usr/bin/env python
"""用 headless Chrome 给应用页面截图（README / 文档用）。

为什么要有这个脚本：README 里放真实效果图对"下载即用"的项目很重要，
而截图必须是**可复现**的 —— 改完样式重跑一次就行，不用手工截。

用法：
    python scripts/make_screenshots.py                 # 截离线样例 + 本机应用
    python scripts/make_screenshots.py --port 9000     # 指定应用端口

依赖：本机装 Chrome 或 Edge（自动探测）；离线样例与导出页都不需要联网。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Windows 控制台（含 CI runner）默认不是 UTF-8，不切的话下面打印中文/符号会
# UnicodeEncodeError 直接崩 —— 本地 Git Bash 是 UTF-8，所以只在 CI 上暴露。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"

CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]


def find_browser() -> str | None:
    for c in CANDIDATES:
        if Path(c).exists():
            return c
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    return None


def shoot(browser: str, url: str, out: Path, size: str, wait_ms: int = 9000) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
           "--force-device-scale-factor=1", f"--window-size={size}",
           f"--virtual-time-budget={wait_ms}",
           f"--screenshot={out}", url]
    r = subprocess.run(cmd, capture_output=True, text=True)
    ok = out.exists() and out.stat().st_size > 5000
    print(("  ✅ " if ok else "  ❌ ") + f"{out.name}  ({out.stat().st_size // 1024 if out.exists() else 0} KB)")
    if not ok:
        print("     ", (r.stderr or r.stdout or "").strip()[:200])
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="给应用页面截图")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--doc", default="", help="阅读页要截哪篇文档（默认取文档库第一篇）")
    args = ap.parse_args()

    browser = find_browser()
    if not browser:
        print("没找到 Chrome / Edge，无法截图。")
        return 2
    print(f"浏览器：{browser}\n输出目录：{OUT}\n")

    base = f"http://{args.host}:{args.port}"
    ok = []

    # 1) 离线样例（双击就能看的对照页，不依赖服务）
    sample = ROOT / "samples" / "demo-plonk" / "PLONK-论文前两页-中英对照示例.html"
    if sample.exists():
        print("离线样例页：")
        ok.append(shoot(browser, sample.as_uri(), OUT / "demo-reading.png", "1500,2400"))
    else:
        print(f"（跳过离线样例：{sample} 不存在；可用 scripts/make_demo.py 生成）")

    # 2) 应用首页与阅读页（需要服务在跑）
    if not _server_alive(base):
        print(f"\n（跳过应用页面：{base} 没有响应，先启动 easyessay.py）")
    else:
        print("\n应用首页：")
        ok.append(shoot(browser, base + "/", OUT / "app-home.png", "1500,1250"))

        doc_id = args.doc or _first_doc(base)
        if doc_id:
            print(f"\n阅读页（doc={doc_id}）：")
            ok.append(shoot(browser, f"{base}/reader?doc={doc_id}",
                            OUT / "app-reader.png", "1500,1600", wait_ms=12000))
        else:
            print("\n（跳过阅读页：文档库是空的）")

    print(f"\n完成：{sum(ok)}/{len(ok)} 张截图写入 {OUT}")
    print("这些图直接用在 README 里（docs/screenshots/*.png）。")
    return 0 if all(ok) else 1


def _server_alive(base: str) -> bool:
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(base + "/api/health", timeout=3) as r:
            return json.loads(r.read().decode("utf-8")).get("ok") is True
    except Exception:
        return False


def _first_doc(base: str) -> str:
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(base + "/api/docs", timeout=5) as r:
            docs = json.loads(r.read().decode("utf-8"))
        for d in docs:
            if (d.get("stats") or {}).get("translated"):
                return d["id"]
        return docs[0]["id"] if docs else ""
    except Exception:
        return ""


if __name__ == "__main__":
    raise SystemExit(main())
