#!/usr/bin/env bash
# ===========================================================================
#  一键安装（Linux / macOS / WSL）
#
#  Windows 用 install.bat。
#
#  为什么 Linux 这边不是"解压一个 embeddable 包"：
#  Python 官方只提供 Windows 的 embeddable ZIP，Linux 没有对应物。可移植的
#  办法是 python-build-standalone，但那要多下一份 30MB 且和系统 glibc 绑定，
#  收益不大。所以这里用 venv —— 它同样把依赖关在仓库里（env/linux），不污染
#  系统 Python，只是解释器本身借用系统的。
#
#  目录约定（和 Windows 那套并存，互不干扰）：
#    env/python  ← Windows 嵌入式环境（Linux 上会被自动忽略）
#    env/linux   ← Linux venv（本脚本创建）
#
#  用法：
#    ./install.sh                    常规安装（已装过就只补缺的）
#    ./install.sh --force            删掉 env/linux 重建
#    ./install.sh --offline          从 env/initialization/wheels 离线安装
#    ./install.sh --skip-tag         不装打标运行时（只打包 / 跑 NL）
#    ./install.sh --no-tests         装完不跑测试
#    ./install.sh --python python3.12    指定解释器
# ===========================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="$ROOT/env/linux"
REQ="$ROOT/requirements-embedded.txt"
WHEELS="$ROOT/env/initialization/wheels"
FORCE=0
OFFLINE=0
RUN_TESTS=1
SKIP_TAG=0
PYTHON_BIN="${PYTHON:-}"

die() { printf '\n [错误] %s\n' "$*" >&2; exit 1; }
note() { printf ' %s\n' "$*"; }

usage() {
    # 直接打印文件头的注释块，省得维护两份用法说明（它们一定会分叉）。
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^#//; s/^ //'
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --force|-f)   FORCE=1 ;;
        --offline)    OFFLINE=1 ;;
        --skip-tag)   SKIP_TAG=1 ;;
        --no-tests)   RUN_TESTS=0 ;;
        --python)     shift || die "--python 后面要跟解释器路径"; PYTHON_BIN="${1:-}" ;;
        -h|--help)    usage ;;
        *)            die "未知参数: $1（--help 看用法）" ;;
    esac
    shift
done

echo
echo " ============================================"
echo "  打标流水线 - 环境安装（Linux/macOS）"
echo "  仓库: $ROOT"
echo " ============================================"
echo

# --------------------------------------------------------------- 解释器
if [ -z "$PYTHON_BIN" ]; then
    for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
        if command -v "$c" >/dev/null 2>&1; then PYTHON_BIN="$c"; break; fi
    done
fi
[ -n "$PYTHON_BIN" ] || die "找不到 python3。装一个：sudo apt install python3 python3-venv"

"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "$PYTHON_BIN 版本太低（需要 >= 3.10，实际 $("$PYTHON_BIN" -V 2>&1)）"

# 仓库可能是从 Windows 拷过来的，env/python 是 Windows 的树。
if [ -f "$ROOT/env/python/python.exe" ]; then
    note "[注意] env/python 是 Windows 的嵌入式环境，Linux 上用不了；"
    note "       本脚本会在 env/linux 另外建一个，两者互不干扰。"
fi

# --------------------------------------------------------------- venv
if [ "$FORCE" -eq 1 ] && [ -d "$VENV" ]; then
    note "[1/5] 删除旧环境 $VENV"
    rm -rf "$VENV"
fi

if [ -x "$VENV/bin/python" ]; then
    note "[1/5] 复用已有环境 $VENV"
else
    note "[1/5] 创建 venv（基于 $("$PYTHON_BIN" -V 2>&1)）"
    mkdir -p "$ROOT/env"
    "$PYTHON_BIN" -m venv "$VENV" || die "创建 venv 失败。Debian/Ubuntu 上先装：sudo apt install python3-venv"
fi
PY="$VENV/bin/python"
# 仓库根写进 .pth：这样从任何目录都能 import tagger，不必 pip install -e。
# 换机器/换目录后重跑本脚本即可（路径是绝对路径）。
SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
printf '%s\n' "$ROOT" > "$SITE/_tagger_repo.pth"

# --------------------------------------------------------------- pip
note "[2/5] 准备 pip"
if ! "$PY" -m pip --version >/dev/null 2>&1; then
    "$PY" -m ensurepip --upgrade >/dev/null 2>&1 || die "ensurepip 失败，装 python3-venv 后重试"
fi
# 老 pip 遇到新 wheel 标签会报错，但离线环境下升级会失败——所以升级是尽力而为。
"$PY" -m pip install --disable-pip-version-check -q --upgrade pip >/dev/null 2>&1 \
    || note "      pip 升级跳过（离线或受限）"

# --------------------------------------------------------------- 依赖
[ -f "$REQ" ] || die "找不到 $REQ"

install_deps() {
    local mode="$1"
    if [ "$mode" = "offline" ]; then
        [ -d "$WHEELS" ] || die "离线安装需要 wheel 目录：$WHEELS（在能联网的机器上执行：python -m pip download -r requirements-embedded.txt -d env/initialization/wheels）"
        note "[3/5] 从本地 wheel 安装（$WHEELS）"
        "$PY" -m pip install --disable-pip-version-check --no-index --find-links "$WHEELS" -r "$REQ"
    else
        note "[3/5] 从 PyPI 安装 requirements-embedded.txt"
        "$PY" -m pip install --disable-pip-version-check -r "$REQ"
    fi
}

if [ "$OFFLINE" -eq 1 ]; then
    install_deps offline
else
    # pip 的输出很长，成功时没必要刷屏；失败时才把末尾贴出来。
    LOG="$(mktemp)"
    if install_deps online >"$LOG" 2>&1; then
        note "      安装完成"
        rm -f "$LOG"
    else
        tail -n 20 "$LOG" || true
        rm -f "$LOG"
        # 联网失败时如果本地有 wheel，自动改走离线，省得人再猜一遍。
        if [ -d "$WHEELS" ]; then
            note "      联网安装失败，改走本地 wheel"
            install_deps offline
        else
            die "联网安装失败。检查网络；或在能联网的机器上执行 pip download 后用 --offline"
        fi
    fi
fi

# --------------------------------------------------------------- 打标运行时
# 默认 tag 后端是 pixai，要 torch / transformers / timm。缺了它第一次跑打标
# 就报 "tag 后端不可用"，而那句话以前只能让人自己去猜该装什么。
# 失败不算致命：打包与 NL 不碰 torch，不该因为一个依赖就连 CLI 都用不了。
TAGREQ="$ROOT/requirements-tag.txt"
if [ "$SKIP_TAG" -eq 1 ]; then
    note "[3.5/5] 跳过打标运行时（--skip-tag）"
elif [ ! -f "$TAGREQ" ]; then
    note "      没找到 $TAGREQ，跳过打标运行时（打标会失败）"
elif "$PY" -c 'import torch, transformers, timm' >/dev/null 2>&1; then
    note "[3.5/5] 打标运行时已就绪"
else
    note "[3.5/5] 安装打标运行时（约 2.5 GB，仅首次）"
    if [ "$OFFLINE" -eq 1 ] && [ -d "$WHEELS" ]; then
        "$PY" -m pip install --disable-pip-version-check -q \
            --no-index --find-links "$WHEELS" -r "$TAGREQ" \
            || note "      没装完；打标会失败，打包/NL 不受影响"
    else
        "$PY" -m pip install --disable-pip-version-check -q -r "$TAGREQ" \
            || note "      没装完；打标会失败，打包/NL 不受影响"
    fi
fi

# --------------------------------------------------------------- 自检
note "[4/5] 自检"
"$PY" - <<'PY'
import sys
import httpx, yaml, PIL, pyarrow
print("      python ", sys.version.split()[0])
print("      httpx  ", httpx.__version__)
print("      pyarrow", pyarrow.__version__)
print("      pillow ", PIL.__version__)
PY
"$PY" -m tagger.cli --help >/dev/null || die "tagger.cli 起不来，检查 $SITE/_tagger_repo.pth 是否指向仓库根"

# --------------------------------------------------------------- 测试
if [ "$RUN_TESTS" -eq 1 ]; then
    note "[5/5] 跑测试（安全网，约 2 秒）"
    "$PY" -m pytest -q || die "测试没通过，环境有问题。可先看：$PY -m pytest -x"
else
    note "[5/5] 跳过测试（--no-tests）"
fi

echo
echo " 完成。下一步："
echo "   ./tagger.sh --help"
echo "   $PY -m pytest"
echo
echo " 打标运行时（torch/transformers/timm/onnxruntime/datasets）默认已装；跳过过就重跑 ./install.sh"
echo "   # vLLM 只跑 Linux（本机有 NVIDIA 卡时）："
echo "   $PY -m pip install vllm"
echo
echo " 提示：文件从 Windows 拷过来时可能没有执行位，先 chmod +x install.sh tagger.sh"
echo
