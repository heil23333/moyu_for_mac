#!/bin/bash
# 摸鱼 PDF 阅读器 - 快速启动脚本
# 双击即可启动，支持拖拽 PDF 文件到终端窗口

cd "$(dirname "$0")"

VENV_DIR="/Users/li/.workbuddy/binaries/python/envs/moyu-pdf"
PYTHON="$VENV_DIR/bin/python3"
SCRIPT="moyu_pdf.py"

if [ ! -f "$PYTHON" ]; then
    echo "❌ 虚拟环境不存在，请先运行："
    echo "   /Users/li/.workbuddy/binaries/python/versions/3.13.12/bin/python3 -m venv $VENV_DIR"
    echo "   $VENV_DIR/bin/pip install PySide6 pymupdf"
    exit 1
fi

if [ -n "$1" ] && [ -f "$1" ]; then
    exec "$PYTHON" "$SCRIPT" "$1"
else
    exec "$PYTHON" "$SCRIPT"
fi
