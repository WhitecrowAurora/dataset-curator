#!/usr/bin/env bash
# Linux / macOS 的"桌面壳"替代品：起本地服务，再用浏览器看。
#
# 与 Windows 的 Tagger.Shell 行为对齐：
#   - 只监听 127.0.0.1
#   - 等 /api/health 通了才开浏览器（否则会先看到一个连接失败页）
#   - Ctrl+C 或关终端时把服务收掉，不留后台进程
#
# 用法：
#   ./run-web.sh            默认端口 27100
#   PORT=28100 ./run-web.sh 换端口
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-27100}"
HOST="127.0.0.1"
URL="http://$HOST:$PORT/"

# 挑解释器：与 tagger.sh 保持同一套优先级
PY=""
for candidate in "$ROOT/env/linux/bin/python" "$ROOT/.venv/bin/python"; do
    if [ -x "$candidate" ]; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
    if command -v python3 >/dev/null 2>&1; then PY="python3";
    else echo "[错误] 没有可用的 Python：先跑 ./install.sh" >&2; exit 1; fi
fi

open_browser() {
    if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 &
    elif command -v open >/dev/null 2>&1; then open "$URL" >/dev/null 2>&1 &
    else echo "[提示] 没找到 xdg-open，请手动打开 $URL"
    fi
}

echo "[web] 启动本地服务：$URL"
echo "[web] 关闭窗口或按 Ctrl+C 结束"

# 服务放后台，好让本脚本能在它起来之后开浏览器
"$PY" -m tagger.cli web --host "$HOST" --port "$PORT" --no-open &
SERVER_PID=$!

cleanup() {
    if kill -0 "$SERVER_PID" 2>/dev/null; then
        echo ""
        echo "[web] 正在停止服务…"
        kill "$SERVER_PID" 2>/dev/null || true
        # 给 uvicorn 3 秒优雅收尾（正在算的那一块会算完再退出）
        for _ in $(seq 1 15); do
            kill -0 "$SERVER_PID" 2>/dev/null || break
            sleep 0.2
        done
        kill -9 "$SERVER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# 轮询健康检查：最多等 60 秒（首次可能在建缓存）
for _ in $(seq 1 200); do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "[错误] 服务启动后立即退出，看上面的输出" >&2
        exit 1
    fi
    if command -v curl >/dev/null 2>&1; then
        if curl -sf --max-time 1 "${URL}api/health" >/dev/null 2>&1; then
            open_browser
            break
        fi
    else
        # 没有 curl 就退回 bash 的 /dev/tcp
        if (exec 3<>"/dev/tcp/$HOST/$PORT") 2>/dev/null; then
            exec 3>&- 3<&-
            open_browser
            break
        fi
    fi
    sleep 0.3
done

# 前台等它退出，这样 Ctrl+C 能传到本脚本的 trap
wait "$SERVER_PID"
