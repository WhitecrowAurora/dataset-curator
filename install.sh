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
#    bash install.sh                 没执行位时（Windows 打的包解压后常见）
#    ./install.sh --force            删掉 env/linux 重建
#    ./install.sh --offline          从 env/initialization/wheels 离线安装
#    ./install.sh --skip-tag         不装打标运行时（只打包 / 跑 NL）
#    ./install.sh --no-llama         不装 llama.cpp（llama-server，约 570MB）
#    ./install.sh --no-tests         装完不跑测试
#    ./install.sh --python python3.12    指定解释器
#    ./install.sh --torch-index cpu  手动指定 torch 索引（cpu / cu126 / cu130 或完整 URL）
#
#  torch 版本默认按显卡自动选：50 系且驱动 580+ → cu130；Turing/Ampere/Ada
#  且驱动够新 → cu126；没卡或驱动太老 → cpu。分档与 install.bat 一致。
# ===========================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# Windows 打出来的 zip 不带 POSIX 执行位，解到 Linux 上三个 .sh 都是 0644。
# 用 bash install.sh 进来时顺手补齐，装完 ./tagger.sh / ./run-web.sh 直接可用。
# 改的是用户自己解压出来的文件，不需要 sudo；只读/noexec 挂载上失败也不该挡安装。
chmod +x "$ROOT"/*.sh 2>/dev/null || true

VENV="$ROOT/env/linux"
REQ="$ROOT/requirements-embedded.txt"
WHEELS="$ROOT/env/initialization/wheels"
FORCE=0
OFFLINE=0
RUN_TESTS=1
SKIP_TAG=0
SKIP_LLAMA=0
PYTHON_BIN="${PYTHON:-}"
TORCH_INDEX="${TORCH_INDEX:-}"

die() { printf '\n [错误] %s\n' "$*" >&2; exit 1; }
note() { printf ' %s\n' "$*"; }

usage() {
    # 直接打印文件头的注释块，省得维护两份用法说明（它们一定会分叉）。
    # 范围跟着文件头走：加一行用法就要同步改这里，否则 --help 会漏最后几行。
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^#//; s/^ //'
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --force|-f)   FORCE=1 ;;
        --offline)    OFFLINE=1 ;;
        --skip-tag)   SKIP_TAG=1 ;;
        --no-llama)   SKIP_LLAMA=1 ;;
        --no-tests)   RUN_TESTS=0 ;;
        --python)     shift || die "--python 后面要跟解释器路径"; PYTHON_BIN="${1:-}" ;;
        --torch-index) shift || die "--torch-index 后面要跟 cpu / cu126 / cu130 或完整 URL"
                      TORCH_INDEX="${1:-}" ;;
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
#
# ★torch 不能直接 `pip install -r requirements-tag.txt`：PyPI 上的 torch 是
#   **CPU 版**，CUDA 版只在 PyTorch 自己的索引里。以前这里就是这么装的，于是
#   装完 install.sh 的 Linux 机器（哪怕插着 A100）拿到的是 CPU 版 torch，跑到
#   打标才发现慢十倍，而且没有任何提示。现在按包名把清单劈成两批，torch /
#   torchvision 走探测出来的索引，其余照旧走普通源。
TAGREQ="$ROOT/requirements-tag.txt"

torch_index_auto() {
    # 分档口径与 Windows（devtools/setup_embedded_env.ps1）和网页依赖页
    # （tagger/web/deps.py 的 torch_plan）保持一致，三处别各写各的。
    #
    # 为什么不解析 nvidia-smi 表头的 "CUDA Version:"：那是**本地化**文本，
    # 中文系统写 "CUDA 版本:"，英文正则一个字符都对不上，结果被当成"读不出
    # 驱动"退到 CPU 版——这正是 2080 Ti 用户反馈的那个坑。改用 --query-gpu
    # 要纯数字，与语言无关。
    command -v nvidia-smi >/dev/null 2>&1 || { echo cpu; return; }

    local drv cc drv_major cc_major cuda_major
    drv="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null \
        | head -n1 | tr -d '[:space:]')"
    cc="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null \
        | head -n1 | tr -d '[:space:]')"

    drv_major="${drv%%.*}"
    case "$drv_major" in ''|*[!0-9]*) drv_major=0 ;; esac
    cc_major="${cc%%.*}"
    case "$cc_major" in ''|*[!0-9]*) cc_major=-1 ;; esac

    if [ "$drv_major" -eq 0 ]; then
        note "      有显卡但没读出驱动版本，先装 CPU 版（打标会慢十倍）" >&2
        echo cpu; return
    fi

    # 驱动号 → 最高 CUDA major（与 deps.py 的 _DRIVER_CUDA_TABLE 同源）
    if   [ "$drv_major" -ge 560 ]; then cuda_major=12
    elif [ "$drv_major" -ge 525 ]; then cuda_major=12
    elif [ "$drv_major" -ge 470 ]; then cuda_major=11
    else cuda_major=0
    fi

    if [ "$cc_major" -ge 12 ]; then
        # 50 系：只有 cu130 有 sm_120 内核。cu130 要求驱动 580+
        if [ "$drv_major" -lt 580 ]; then
            note "      50 系（CC $cc）要 cu130，而驱动 $drv 不够（需 580+）；先用 CPU 版" >&2
            echo cpu; return
        fi
        echo cu130; return
    fi

    if [ "$cuda_major" -lt 12 ]; then
        note "      驱动 $drv 只支持 CUDA 11.x，cu126 版 import 会报驱动版本不足；改装 CPU 版" >&2
        echo cpu; return
    fi
    # Turing / Ampere / Ada 一律 cu126。20 系（CC 7.5）不因为驱动新就升 cu130：
    # CUDA 13 砍的是 7.5 之前，sm_75 内核在，但 7.5 没有 TF32/bf16，换大版本
    # 只有开销，而 20/30/40 系的实测基准全在 cu126 上。
    echo cu126
}

if [ "$SKIP_TAG" -eq 1 ]; then
    note "[3.5/5] 跳过打标运行时（--skip-tag）"
elif [ ! -f "$TAGREQ" ]; then
    note "      没找到 $TAGREQ，跳过打标运行时（打标会失败）"
elif "$PY" -c 'import torch, transformers, timm' >/dev/null 2>&1; then
    note "[3.5/5] 打标运行时已就绪"
else
    note "[3.5/5] 安装打标运行时（约 2.5 GB，仅首次）"
    # 按包名劈清单：torch/torchvision 走 PyTorch 索引，其余走原来源。
    TORCHEQ="$(mktemp)"
    OTHEREQ="$(mktemp)"
    trap 'rm -f "$TORCHEQ" "$OTHEREQ"' EXIT
    grep -E '^(torch|torchvision)([^A-Za-z0-9_.-]|$)' "$TAGREQ" >"$TORCHEQ" || true
    grep -Ev '^(torch|torchvision)([^A-Za-z0-9_.-]|$)' "$TAGREQ" >"$OTHEREQ" || true

    if [ -n "${TORCH_INDEX:-}" ]; then
        case "$TORCH_INDEX" in
            # 允许简写：--torch-index cu126 比整条 URL 好敲，也不容易写错域名
            http://*|https://*) TIDX="$TORCH_INDEX" ;;
            *) TIDX="https://download.pytorch.org/whl/$TORCH_INDEX" ;;
        esac
    else
        TIDX="$(torch_index_auto)"
        case "$TIDX" in
            cpu) TIDX="https://download.pytorch.org/whl/cpu" ;;
            *)   TIDX="https://download.pytorch.org/whl/$TIDX" ;;
        esac
    fi

    if [ "$OFFLINE" -eq 1 ] && [ -d "$WHEELS" ]; then
        note "      torch 来源: 本地 wheel（$WHEELS）"
        "$PY" -m pip install --disable-pip-version-check -q \
            --no-index --find-links "$WHEELS" -r "$TORCHEQ" \
            || note "      torch 没装完；打标会失败，打包/NL 不受影响"
    else
        note "      torch 来源: $TIDX"
        "$PY" -m pip install --disable-pip-version-check -q \
            --index-url "$TIDX" -r "$TORCHEQ" \
            || note "      torch 没装完；打标会失败，打包/NL 不受影响"
    fi
    if [ -s "$OTHEREQ" ]; then
        "$PY" -m pip install --disable-pip-version-check -q -r "$OTHEREQ" \
            || note "      其余依赖没装完；打标会失败，打包/NL 不受影响"
    fi
fi

# --------------------------------------------------------------- NL 引擎
# llama.cpp 是默认引擎，没有 llama-server 服务起不来。安装逻辑在
# tagger/llama_runtime.py（与网页按钮、CLI 同一份）。Linux 上官方发
# ubuntu-x64 包；macOS 没有预编译，status() 会报 unsupported。
# 失败不算致命：只影响 NL，打 tag / 打包 / 回传照样能用。
if [ "$SKIP_LLAMA" -eq 1 ]; then
    note "[3.8/5] 跳过 llama.cpp（--no-llama）"
elif "$PY" -c 'from tagger import llama_runtime as r; raise SystemExit(0 if r.status()["installed"] else 1)' >/dev/null 2>&1; then
    note "[3.8/5] llama.cpp 已就绪"
else
    note "[3.8/5] 安装 llama.cpp（约 570MB，仅首次）"
    "$PY" -m tagger.cli llama install \
        || note "      没装成；NL 打标会失败，打 tag / 打包不受影响。手动重试：$PY -m tagger.cli llama install"
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
# 判据不能只有"用户没给 --no-tests"：`/tests` 在 .gitignore 里，直接 git
# clone 的源码树没有它；`pack-dist.bat -NoTests` 打的包也没有。这两种情况下
# pytest 以退出码 5（收集到 0 个用例）结束，被 set -e 当成"环境坏了"，
# 于是装到最后一步报安装失败——而其实什么都是好的。
if [ "$RUN_TESTS" -ne 1 ]; then
    note "[5/5] 跳过测试（--no-tests）"
elif [ ! -d "$ROOT/tests" ]; then
    note "[5/5] 没有 tests/ 目录（源码树或 -NoTests 包），跳过测试"
else
    note "[5/5] 跑测试（安全网，约 2 秒）"
    rc=0
    "$PY" -m pytest -q || rc=$?
    if [ "$rc" -eq 5 ]; then
        note "      tests/ 里没有用例，跳过"
    elif [ "$rc" -ne 0 ]; then
        die "测试没通过，环境有问题。可先看：$PY -m pytest -x"
    fi
fi

echo
echo " 完成。下一步："
echo "   ./tagger.sh --help"
echo "   $PY -m pytest"
echo
echo " 打标运行时（torch/transformers/timm/onnxruntime/datasets）默认已装；跳过过就重跑 ./install.sh"
echo " llama.cpp（NL 引擎）默认已装；跳过过就重跑 ./install.sh 或："
echo "   $PY -m tagger.cli llama install"
echo " 模型权重是另一件事：打开界面「模型管理」下载对应条目（默认破限版，几 GB）"
echo "   # vLLM 只跑 Linux（本机有 NVIDIA 卡时）："
echo "   $PY -m pip install vllm"
echo
echo " 三个 .sh 的执行位已在开头补齐；若解压到 noexec 挂载点，改用 bash run-web.sh"
echo
