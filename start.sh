#!/usr/bin/env bash
# Ayabot 启动脚本 (Linux / macOS)
# 使用方式: bash start.sh
# 按 Ctrl+C 完全退出，不残留任何进程

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV="$SCRIPT_DIR/.venv"

# ── 检查虚拟环境 ──
if [ ! -f "$VENV/bin/python" ]; then
    echo "[ERROR] 虚拟环境不存在: .venv"
    echo "        请运行: python3 -m venv .venv"
    echo "               source .venv/bin/activate"
    echo "               pip install -r requirements.txt"
    exit 1
fi

# ── 激活虚拟环境 ──
source "$VENV/bin/activate"

# ── 检查依赖是否已安装 ──
if ! python -c "import fastapi" 2>/dev/null; then
    echo "[INFO] 检测到依赖未安装，正在安装..."
    pip install -r requirements.txt
fi

# ── 清理函数 ──
_cleanup() {
    # 防止重复调用
    if [ "${_CLEANUP_DONE:-0}" = "1" ]; then return; fi
    _CLEANUP_DONE=1

    local ec=$?
    echo ""
    echo "[Ayabot] 正在关闭 WebUI 及所有房间 Bot..."
    # 杀掉 python 子进程（uvicorn + Bot 进程树），确保不残留
    # pgrep 找当前会话下的 python 进程，排除本身
    local pids
    pids=$(pgrep -P $$ 2>/dev/null || true)
    if [ -n "$pids" ]; then
        # 先 SIGTERM 优雅停止
        echo "$pids" | xargs kill 2>/dev/null || true
        sleep 1
        # 再 SIGKILL 确保死透
        echo "$pids" | xargs kill -9 2>/dev/null || true
    fi
    deactivate 2>/dev/null || true
    echo "[Ayabot] 已完全退出."
    exit "$ec"
}

# ── 信号处理 ──
# Ctrl+C → 前台 python 收 SIGINT（自行关闭），python 退出后走 EXIT 清理
# SIGTERM → 直接走 cleanup 杀掉所有子进程后退出
trap _cleanup SIGTERM EXIT

echo "[INFO] 启动 Ayabot WebUI..."
echo "       浏览器打开 http://localhost:19810"
echo "       初始账号: ayabot / 123456"
echo "       按 Ctrl+C 完全停止（不残留进程）"
echo ""

# ── 前台运行 WebUI ──
# uvicorn 直接接收键盘中断，收到 SIGINT 后自行关闭 HTTP 服务及所有 Bot 进程
python web_serve.py
