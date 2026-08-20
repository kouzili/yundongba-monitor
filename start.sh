#!/bin/bash
# 韵动吧场地监控 - 启动脚本
# 用法: bash start.sh [--port 5100]
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -x .venv/bin/python3 ]; then
    echo "🔧 首次运行，创建虚拟环境…"
    python3 -m venv .venv
    .venv/bin/pip install -q -r requirements.txt
fi

# 杀掉旧进程
pkill -f "python3.*app\.py" 2>/dev/null || true

echo "🚀 启动管理后台…"
.venv/bin/python3 app.py "$@" &
PID=$!
sleep 2

if kill -0 "$PID" 2>/dev/null; then
    echo "✅ 服务已启动 (PID: $PID)"
    echo "   停止: kill $PID"
else
    echo "❌ 启动失败"
    exit 1
fi
