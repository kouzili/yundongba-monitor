#!/bin/bash
# 韵动吧网球场监控 - 启动脚本
# 用法: bash start.sh

cd "$(dirname "$0")"

# 杀掉旧进程
pkill -f "python3.*app.py" 2>/dev/null

echo "🚀 启动管理后台..."
.venv/bin/python3 app.py --port 5100 &
PID=$!
sleep 2

if kill -0 $PID 2>/dev/null; then
    echo "✅ 服务已启动 (PID: $PID)"
    echo "   打开浏览器: http://localhost:5100"
    echo "   停止命令: kill $PID"
else
    echo "❌ 启动失败，查看日志:"
    cat /tmp/tennis.log 2>/dev/null
fi
