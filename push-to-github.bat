@echo off
chcp 65001 >nul
REM ============================================================
REM  EasyEssay 推送更新 / 发布新版本（双击运行）
REM
REM  说明：仓库已经推上去了，这个脚本用于**以后**同步改动、
REM        以及打 tag 触发三平台自动构建。
REM
REM  关于 SSH 还是 HTTPS：两者都能推，但有些网络会**只拦 HTTPS**
REM  （表现为 https 推送超时/连接被重置，而 ssh 正常）。
REM  所以这里优先用 SSH。切换方式见下面「如果推送失败」。
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
  echo   [x] 还没设置远端。执行一次（换成你的仓库地址）：
  echo       git remote add origin git@github.com:你的用户名/仓库名.git
  echo.
  pause
  exit /b 1
)

echo   开始推送……
echo.
git push -u origin %BRANCH%
if errorlevel 1 (
  echo.
  echo   [x] 推送失败。按顺序排查：
  echo       1) 网络：有些网络只拦 HTTPS、不拦 SSH。把远端换成 SSH 再试：
  echo            git remote set-url origin git@github.com:你的用户名/仓库名.git
  echo            ssh -T git@github.com      ^(看到 "Hi 你的用户名!" 就说明密钥已配好^)
  echo          还没有密钥就先在这里执行： ssh-keygen -t ed25519  然后
  echo          把  %USERPROFILE%\.ssh\id_ed25519.pub  的内容贴到
  echo          GitHub → Settings → SSH and GPG keys → New SSH key
  echo       2) 想用 HTTPS：确认远端是 https:// 开头，且凭据没失效
  echo          ^(Windows「凭据管理器」里删掉 github.com 相关条目后重试^)
  echo       3) 也可以在这个窗口里手动执行： git push -u origin %BRANCH%
  echo.
  pause
  exit /b 1
)

echo.
echo   [OK] 推送成功。
echo.

REM ---- 顺便问一下要不要发新版本 -------------------------------------
set /p VER=要发布新版本吗？输入版本号（例如 1.0.1），直接回车跳过：
if "%VER%"=="" goto done

echo.
echo   打 tag v%VER% 并推送（会触发三平台自动构建，几分钟后到 Releases 页面下载）……
git tag -a v%VER% -m "EasyEssay v%VER%"
git push origin v%VER%
if errorlevel 1 (
  echo.
  echo   [x] tag 推送失败。如果提示 tag 已存在，先删掉再打：
  echo       git tag -d v%VER%
  echo       git push origin :refs/tags/v%VER%
  echo.
  pause
  exit /b 1
)
echo.
echo   [OK] 已触发构建。打开 Releases 页面看进度：
echo        https://github.com/你的用户名/仓库名/actions
echo.

:done
echo.
pause
