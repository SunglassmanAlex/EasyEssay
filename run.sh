#!/usr/bin/env bash
# EasyEssay 一键启动（macOS / Linux）
set -e
cd "$(dirname "$0")"

# 用哪个 python：默认 PATH 里的 python3；如需指定，先设环境变量 EE_PY
PY="${EE_PY:-python3}"

mkdir -p data
[ -f .env ] || cp .env.example .env 2>/dev/null || true

echo
echo "  EasyEssay 论文翻译助手"
echo "  本机使用： ./run.sh             （只绑 127.0.0.1）"
echo "  给朋友用： ./run.sh --open      （监听局域网，会打印可访问地址）"
echo "  停止服务：Ctrl+C"
echo

exec "$PY" easyessay.py --port 8765 "$@"
