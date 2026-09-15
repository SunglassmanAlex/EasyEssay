@echo off
REM EasyEssay 一键启动（Windows）
setlocal
cd /d "%~dp0"

REM 用哪个 python：默认用 PATH 里的 python；想指定就先把 EE_PY 设成解释器路径
if not defined EE_PY set EE_PY=python
set PY=%EE_PY%

if not exist "data" mkdir data
if not exist ".env" if exist ".env.example" copy /y ".env.example" ".env" >nul

echo.
echo   EasyEssay 论文翻译助手
echo   本机使用： run.bat            （只绑 127.0.0.1）
echo   给朋友用： run.bat --open     （监听局域网，会打印可访问地址）
echo   停止服务：在本窗口按 Ctrl+C
echo.

"%PY%" easyessay.py --port 8765 %*
endlocal
