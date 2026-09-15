@echo off
chcp 65001 >nul
REM ============================================================
REM  把本项目推送到 GitHub（双击运行）
REM  第一次会弹出 GitHub 登录窗口，登录一次之后就记住了
REM ============================================================
setlocal
cd /d "%~dp0"

where git >nul 2>nul
if errorlevel 1 (
  echo.
  echo   [x] 没找到 git。请先安装 Git for Windows： https://git-scm.com/download/win
  echo.
  pause
  exit /b 1
)

for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set BRANCH=%%b
if "%BRANCH%"=="" set BRANCH=main

echo.
echo   项目目录： %CD%
echo   当前分支： %BRANCH%
echo.
echo   远端：
git remote -v
echo.

git remote get-url origin >nul 2>nul
if errorlevel 1 (
  echo   [x] 还没设置远端。请先执行：
  echo       git remote add origin https://github.com/你的用户名/仓库名.git
  echo.
  pause
  exit /b 1
)

echo   开始推送……如果弹出 GitHub 登录窗口，请完成登录。
echo.
git push -u origin %BRANCH%
if errorlevel 1 (
  echo.
  echo   [x] 推送失败。常见原因：
  echo       * 登录窗口被关掉或凭据失效  ^(可在“凭据管理器”里删掉 github.com 重试^)
  echo       * 这个仓库不是你的           ^(确认远端地址^)
  echo       * 网络问题                   ^(开着代理/加速器时先关掉再试^)
  echo.
  echo   也可以在这个窗口里手动执行：  git push -u origin %BRANCH%
) else (
  echo.
  echo   [√] 推送成功。到 GitHub 打开你的仓库看看：
  echo.
  git remote get-url origin
  echo.
  echo   想要别人能下载到打包好的程序，再执行一次（打标签触发自动构建）：
  echo       git tag v1.0.0
  echo       git push origin v1.0.0
)
echo.
pause
