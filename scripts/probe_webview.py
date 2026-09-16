"""探针：确认当前环境能否真的用 pywebview 开出桌面窗口。

只是一次性验证用，不属于产品代码。
"""
from __future__ import annotations

import sys
import threading
import time

# Windows 控制台（含 CI runner）默认不是 UTF-8，不切的话下面打印中文/符号会
# UnicodeEncodeError 直接崩 —— 本地 Git Bash 是 UTF-8，所以只在 CI 上暴露。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import webview
except Exception as exc:  # noqa: BLE001
    print("IMPORT_FAIL:", exc)
    sys.exit(2)

RESULT: dict[str, object] = {}


class Api:
    """页面加载完成后回调，证明 WebView 真的把页面渲染出来了。"""

    def ready(self, title: str, ua: str) -> None:
        RESULT["title"] = title
        RESULT["ua"] = ua[:60]
        print("JS 回调成功 -> title =", title)
        print("UA =", ua[:60])


HTML = """
<!doctype html><meta charset="utf-8"><title>EasyEssay 探针</title>
<h1>窗口测试</h1>
<script>
  // 桥接注入时机不固定，轮询等它出现再回调
  var tries = 0;
  var t = setInterval(function () {
    tries++;
    if (window.pywebview && window.pywebview.api) {
      clearInterval(t);
      window.pywebview.api.ready(document.title, navigator.userAgent);
    } else if (tries > 40) {
      clearInterval(t);
      document.title = 'BRIDGE_TIMEOUT';
    }
  }, 200);
</script>
"""


def main() -> None:
    api = Api()
    win = webview.create_window("EasyEssay 探针", html=HTML, js_api=api,
                                width=520, height=320)

    def closer() -> None:
        time.sleep(12)
        try:
            win.destroy()
        except Exception as exc:  # noqa: BLE001
            print("destroy 失败:", exc)

    threading.Thread(target=closer, daemon=True).start()
    webview.start()
    try:
        print("GUI 后端:", webview.guilib)
    except Exception:  # noqa: BLE001
        pass
    print("start() 正常返回（窗口已创建并销毁）")
    print("RESULT =", RESULT)
    print("结论:", "✅ 桌面窗口可用" if RESULT.get("title") else "⚠️ 窗口开了但页面没回调")


if __name__ == "__main__":
    main()
