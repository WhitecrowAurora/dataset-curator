#!/usr/bin/env bash
# 便携入口（Linux / macOS），与 tagger.cmd 对应。
# 优先用 install.sh 建出来的 env/linux，其次系统 python3。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 切到包根：configs/tiers.yaml 与 prompt/*.json 都按相对路径解析，在别的目录
# 跑 ./tagger.sh 会读不到它们（tiers 直接报错，预设下拉静默变空）。
# 代价是相对路径参数（--out out）相对包根，而不是调用时所在的目录。
cd "$ROOT"

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
