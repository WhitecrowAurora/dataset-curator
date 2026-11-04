#!/usr/bin/env bash
# 便携入口（Linux / macOS），与 tagger.cmd 对应。
# 优先用 install.sh 建出来的 env/linux，其次系统 python3。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for PY in "$ROOT/env/linux/bin/python" "$ROOT/.venv/bin/python"; do
    if [ -x "$PY" ]; then
        exec "$PY" -m tagger.cli "$@"
    fi
done

if command -v python3 >/dev/null 2>&1; then
    exec python3 -m tagger.cli "$@"
fi

echo "[错误] 没有可用的环境：先跑 ./install.sh，或手动 python3 -m tagger.cli" >&2
exit 1
