"""把标准输出/错误切成 UTF-8。

**为什么需要这个**：Windows 控制台（cmd / PowerShell / CI runner）的默认编码可能是
CP936、CP1252 之类，而本项目到处要打印中文、`✅`、`❌`、`⟦?⟧` 这些字符。
不切的话，`print()` 会直接抛 `UnicodeEncodeError` 让脚本崩掉 ——
CI 上第一次发布就栽在这（"Process completed with exit code 1"，
本地 Git Bash 是 UTF-8 所以复现不出来）。

排查手法：用 `PYTHONIOENCODING=cp1252 python scripts/http_test.py` 复现，
比在 CI 上猜快得多。

调用点：程序入口（`easyessay.py`）和各命令行脚本的**最前面**，早于任何 print。
"""
from __future__ import annotations

import sys


def force_utf8() -> bool:
    """尽力把 stdout/stderr 切到 UTF-8；切不动就算了（不能因此让程序起不来）。"""
    ok = False
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            # errors="replace"：万一终端真的渲染不了某些字符，也不要崩
            reconfigure(encoding="utf-8", errors="replace")
            ok = True
        except Exception:  # noqa: BLE001
            pass
    return ok
