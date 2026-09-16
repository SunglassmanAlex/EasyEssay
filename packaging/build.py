#!/usr/bin/env python
"""把 EasyEssay 打包成"下载就能双击用"的可执行文件。

用法（先装 PyInstaller）：
    pip install -r requirements-build.txt
    python packaging/build.py              # 打包当前平台
    python packaging/build.py --zip        # 顺便打成 zip，便于上传到 GitHub Release

产出：
    dist/EasyEssay(.exe)                  单文件可执行程序
    dist/EasyEssay-<平台>.zip              压缩包（含说明）
    dist/README-使用说明.txt

打包要点（踩过的坑都写在注释里）：
  * web/ 是运行时需要的静态资源，必须 --add-data 打进去；
    app/config.py 在 frozen 模式下会从 sys._MEIPASS 找它。
  * 数据目录（data/）会放在可执行文件**同级目录**，不是临时解包目录，
    否则用户一退出就丢文档。config.py 同样做了这个判断。
  * uvicorn 的动态导入（事件循环 / 协议实现）PyInstaller 静态分析不到，
    必须显式 --hidden-import，否则启动时报 "no module named uvicorn.loops.auto"。
  * uvicorn 的导入字符串 "app.main:app" 在打包后不可靠 → app/main.py 里
    frozen 模式改成直接把 app 对象传给 uvicorn.run()。
"""
from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# Windows 控制台（含 CI runner）默认不是 UTF-8，不切的话下面打印中文/符号会
# UnicodeEncodeError 直接崩 —— 本地 Git Bash 是 UTF-8，所以只在 CI 上暴露。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
NAME = "EasyEssay"

HIDDEN_IMPORTS = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "app.main",
    "app.cli",
    # 原生桌面窗口（装了 pywebview 才有）。它的 GUI 后端是运行时动态选的，
    # PyInstaller 静态分析不到，不显式声明的话打包后窗口起不来、
    # 只能悄悄退回浏览器模式 —— 用户会以为"这个 exe 不是 app"。
    "webview",
    "webview.platforms.winforms",     # Windows：WinForms + WebView2
    "webview.platforms.gtk",          # Linux
    "webview.platforms.cocoa",        # macOS
    "clr",                            # pythonnet（Windows 后端依赖）
]

README_TXT = """EasyEssay · 论文翻译助手
================================

怎么用（三步）
--------------
1. 把本目录整个解压到一个**可写**的位置（例如 D:\\\\EasyEssay），不要放在压缩包里直接运行。
2. 双击 EasyEssay（Windows 下是 EasyEssay.exe）。首次运行系统防火墙可能弹窗，选“允许访问”
   （只是让应用连上它自己启动的本地服务，不允许也不影响使用）。
3. 会弹出一个应用窗口 —— 第一次让你填自己的 DeepSeek API Key
   （只保存在本机 data/settings.json，不会上传到任何地方）。

之后就能：上传 PDF → 自动逐段翻译 → 左右对照阅读 → 选中任意句子追问。
顶栏「导出 HTML」可得到可独立打开、可分享的对照阅读页。

窗口还是浏览器？
----------------
默认弹**应用窗口**。想要浏览器标签页（或窗口起不来时兜底），加参数：
  EasyEssay.exe --browser        用浏览器打开
  EasyEssay.exe --no-browser     不开浏览器，只启动服务（自己访问提示的地址）
  EasyEssay.exe --open           让同一局域网的别人也能访问（无密码，慎用）

需要什么
--------
* Windows 10/11、macOS 12+ 或 Linux（x86_64）
* 一个 DeepSeek API Key（https://platform.deepseek.com/api_keys）
* 桌面窗口在 Windows 上依赖系统自带的 WebView2（Win10/11 一般都有）；
  万一没有，程序会自己改用浏览器打开，不影响使用。
* 扫描件需要 OCR，可用 pip 装 requirements-ocr.txt 后改用源码方式运行

数据在哪
--------
程序同级目录的 data/ ：
  data/settings.json        你的设置与 API Key
  data/docs/<文档>/          原文、抽取结果、译文、问答记录
删掉 data/ 就等于恢复出厂设置；换电脑时把这个目录一起拷走即可。

常见问题
--------
* 双击没反应 / 窗口一闪而过：在终端里运行 （Windows: 在地址栏输 cmd 回车，然后输 EasyEssay.exe）
  可以看到具体报错。
* 端口被占用：程序会自动换一个端口，以浏览器实际打开的地址为准；也可 --port 9000 指定。
* 想给同一局域网的别人用：命令行加 --open（注意：这个模式没有密码保护，只在可信网络使用）。
* 想完全离线/自建模型：设置里把 Base URL 换成 https://api.siliconflow.cn/v1 或本地 Ollama 的
  http://127.0.0.1:11434/v1 即可。

本程序按 MIT 协议开源，论文版权归原作者所有。
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 EasyEssay 可执行文件")
    ap.add_argument("--zip", action="store_true", help="顺便打成 zip")
    ap.add_argument("--onedir", action="store_true",
                    help="打包成目录而不是单文件（启动更快，但文件多）")
    ap.add_argument("--console", action="store_true",
                    help="保留控制台窗口（排错用；默认 Windows 下隐藏控制台）")
    ap.add_argument("--with-ocr", action="store_true",
                    help="把 OCR（rapidocr-onnxruntime）打进去，支持扫描版 PDF；"
                         "体积会从 ~62 MB 涨到 ~130 MB")
    args = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("缺少 PyInstaller，请先执行： pip install -r requirements-build.txt")
        return 2

    dist = DIST
    if dist.exists():
        shutil.rmtree(dist, ignore_errors=True)

    # 图标：没有就现场生成（用代码画，不依赖外部素材）
    icon = Path(__file__).resolve().parent / ("icon.ico" if platform.system() == "Windows"
                                             else "icon.png")
    if not icon.exists():
        subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "make_icon.py")],
                       cwd=str(ROOT))

    sep = ";" if platform.system() == "Windows" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", NAME,
        "--paths", str(ROOT),
        "--add-data", f"{ROOT / 'web'}{sep}web",
    ]
    if icon.exists():
        cmd += ["--icon", str(icon)]
    if not args.onedir:
        cmd.append("--onefile")
    if platform.system() == "Windows" and not args.console:
        cmd.append("--noconsole")
    for mod in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", mod]
    # 这些是核心依赖，装了就带上
    for mod in ("pymupdf", "pdfplumber", "pylatexenc", "dotenv", "PIL"):
        cmd += ["--hidden-import", mod]
    # ⚠️ OCR **不能**"装了就带"：本机装过 rapidocr-onnxruntime 之后，
    #    它会把 onnxruntime + opencv + numpy 一起拖进来，exe 从 62 MB 直接涨到 130 MB
    #    （踩过：我只是为了读一张截图临时装了个 OCR，结果打包体积翻倍）。
    #    所以做成显式开关，需要给扫描件做 OCR 的人才加 --with-ocr。
    # 光是"不加 hidden-import"不够：PyInstaller 是**按代码里的 import 语句**静态收集的，
    # app/ocr.py 里那句懒加载 `from rapidocr_onnxruntime import RapidOCR` 照样会被它揪出来，
    # 于是 onnxruntime/opencv/numpy 全被拖进来（实测 62 MB → 124 MB）。
    # 必须显式 --exclude-module 才真的不打包。
    _OCR_MODULES = ("rapidocr_onnxruntime", "onnxruntime", "cv2", "numpy",
                    "pyclipper", "shapely", "flatbuffers")
    ocr_ok = bool(args.with_ocr)
    if ocr_ok:
        for mod in ("rapidocr_onnxruntime", "cv2", "onnxruntime", "numpy"):
            cmd += ["--hidden-import", mod]
    else:
        for mod in _OCR_MODULES:
            cmd += ["--exclude-module", mod]
    # pywebview 自带 js/css 资源（webview/js、webview/lib），必须一并收集，
    # 否则窗口里的 JS 桥接会缺文件
    try:
        import webview  # noqa: F401
        cmd += ["--collect-all", "webview", "--collect-submodules", "webview.platforms"]
    except Exception:  # noqa: BLE001
        print("（未安装 pywebview，本次构建只能通过浏览器打开）")
    cmd.append(str(ROOT / "easyessay.py"))

    print("执行：", " ".join(cmd[:6]), "…")
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("\n打包失败。常见原因：见本脚本顶部注释。")
        return result.returncode

    # 把使用说明放到产物旁边
    (dist / "README-使用说明.txt").write_text(README_TXT, encoding="utf-8")

    binary = dist / (NAME + (".exe" if platform.system() == "Windows" else ""))
    size_mb = binary.stat().st_size / 1048576 if binary.exists() else 0
    print(f"\n✅ 打包完成：{binary}（{size_mb:.1f} MB）")
    print("   OCR：" + ("已包含（扫描版 PDF 可用）" if ocr_ok else
                      "未包含（扫描件请装 requirements-ocr.txt 后用源码运行，"
                      "或加 --with-ocr 重新打包）"))

    if args.zip:
        tag = f"{platform.system().lower()}-{platform.machine().lower()}"
        zip_path = dist / f"{NAME}-{tag}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            if binary.exists():
                z.write(binary, binary.name)
            z.write(dist / "README-使用说明.txt", "README-使用说明.txt")
            samples = ROOT / "samples" / "demo-plonk" / "PLONK-论文前两页-中英对照示例.html"
            if samples.exists():
                z.write(samples, "效果示例-中英对照.html")
        print(f"✅ 已压缩：{zip_path}")
        print("   把这个 zip 上传到 GitHub Releases（或直接发给朋友）即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
