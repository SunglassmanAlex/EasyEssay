#!/usr/bin/env python
"""EasyEssay 启动入口。

三种用法：
  python easyessay.py                # 源码方式启动（会自动开浏览器）
  python easyessay.py --port 9000    # 指定端口
  python easyessay.py --open         # 让同一局域网的别人也能访问

打包成可执行文件时也用它作为入口（见 packaging/build.py）：
  PyInstaller --onefile --name EasyEssay easyessay.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# 源码运行时，确保仓库根目录在 import 路径里（打包后 PyInstaller 已处理）
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.console import force_utf8  # noqa: E402
from app.main import main  # noqa: E402

force_utf8()   # 必须早于任何 print：Windows 控制台默认不是 UTF-8


def _show_fatal(message: str) -> None:
    """启动失败时把原因显示出来。

    打包成 app 后 Windows 上是隐藏控制台的，双击启动若报错会"一闪而过"，
    用户什么也看不到。这里用系统弹窗兜底（非 Windows 就打到 stderr）。
    """
    import sys as _sys
    text = ("EasyEssay 启动失败\n\n" + message +
            "\n\n常见原因：\n"
            "· 依赖没装好：pip install -r requirements.txt\n"
            "· 端口被占用：换一个 --port\n"
            "· 文件被放在只读目录：换到可写目录再运行")
    if _sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, "EasyEssay", 0x10)
            return
        except Exception:
            pass
    print(text, file=_sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:              # noqa: BLE001
        import traceback
        traceback.print_exc()
        _show_fatal("".join(traceback.format_exception_only(type(exc), exc)).strip())
