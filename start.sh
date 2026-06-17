#!/usr/bin/env bash
# Ayabot 启动脚本 (Linux / macOS)
# 使用方式: bash start.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── 检查虚拟环境 ──
if [ ! -f ".venv/bin/python" ]; then
    echo "[ERROR] 虚拟环境不存在: .venv"
    echo "        请运行: python3 -m venv .venv"
    echo "               source .venv/bin/activate"
    echo "               pip install -r requirements.txt"
    exit 1
fi

# ── 激活虚拟环境 ──
source .venv/bin/activate

# ── 检查依赖是否已安装 ──
if ! python -c "import fastapi" 2>/dev/null; then
    echo "[INFO] 检测到依赖未安装，正在安装..."
    pip install -r requirements.txt
fi

echo "[INFO] 启动 Ayabot WebUI..."
echo "       浏览器打开 http://localhost:19810"
echo "       初始账号: ayabot / 123456"
echo ""

# ── 启动 Web 管理后台 ──
python web_serve.py

# ── 退出时取消激活虚拟环境 ──
deactivate 2>/dev/null || true
