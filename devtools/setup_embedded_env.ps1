<#
.SYNOPSIS
    在仓库内搭一套可携带的嵌入式 Python 环境：env\python。

.DESCRIPTION
    做法和训练器一致：官方 embeddable 版 Python 解压进仓库，不碰系统环境、
    不装注册表、不需要管理员权限。所有人拿到同一份 env\python，版本与
    依赖完全一致；只有 torch / vllm 这类大包各自按需装进来。

    为什么不用 venv：
      venv 依赖本机那份 Python（版本各不相同），且拷给别人后路径写死在
      pyvenv.cfg 里。嵌入式版是一棵自带的树，压缩带走、解压即用。

    脚本是幂等的：
      已存在 env\python 时不会重装，只在缺东西时补；
      加 -Force 才会删掉重建（换 Python 版本时用）。

.PARAMETER PythonZip
    指定 embeddable 包。不给就挑 env\initialization 里版本号最大的那个。

.PARAMETER WheelDir
    离线安装：从本地 wheel 目录装，不访问 PyPI。
    准备离线包：python -m pip download -r requirements-embedded.txt -d env\initialization\wheels

.PARAMETER IndexUrl
    指定 pip 索引，例如 https://pypi.org/simple。
    默认用本机 pip 配置，很多机器上是国内镜像；镜像滞后时会出现
    "Could not find a version that satisfies the requirement <包>==<新版本>"，
    而列出的可用版本停在某个旧版本——那不是清单写错了，是源太旧。

.PARAMETER Force
    删掉 env\python 从头重建。

.PARAMETER TorchIndex
    torch/torchvision 从哪个索引装。留空按机器自动选：
    有 nvidia-smi 用 cu126，没有用 cpu。想装别的 CUDA 版本时显式传，
    例如 https://download.pytorch.org/whl/cu124。

.PARAMETER SkipTag
    跳过打标运行时（torch/transformers/timm/onnxruntime/datasets，约 2.5 GB）。
    只做打包、不跑打标时可以加，能省十分钟。

.PARAMETER NoLlama
    跳过 llama.cpp（llama-server）的自动安装，约 570MB。
    想自己控制版本、或内网下不了 GitHub 时用；之后仍可手动：
      env\python\python.exe -m tagger.cli llama install
    它只是 NL 引擎，跳过不影响打 tag、打包与回传。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File devtools\setup_embedded_env.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File devtools\setup_embedded_env.ps1 -Force -WheelDir env\initialization\wheels

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File devtools\setup_embedded_env.ps1 -SkipTag
#>
[CmdletBinding()]
param(
    [string]$PythonZip = "",
    [string]$WheelDir = "",
    [string]$IndexUrl = "",
    [string]$TorchIndex = "",
    [switch]$Force,
    [switch]$SkipTag,
    [switch]$NoLlama
)

$ErrorActionPreference = 'Stop'

# PS 5.1 的一个坑：在 Stop 模式下，原生命令往 stderr 写字会被当成终止错误。
# pip 几乎必然往 stderr 打警告（版本提示、缓存警告），直接 & 调用会让脚本
# 在"明明成功"的地方中断。所以所有外部命令都走这个包装：临时把 EAP 降回
# Continue，只认退出码。
function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$Quiet
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($Quiet) {
            & $Exe @Arguments | Out-Null
        } else {
            & $Exe @Arguments | Out-Host
        }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
}

$repo = Split-Path -Parent $PSScriptRoot
$envRoot = Join-Path $repo 'env'
$pyDir = Join-Path $envRoot 'python'
$initDir = Join-Path $envRoot 'initialization'
$sitePackages = Join-Path $pyDir 'Lib\site-packages'
$reqFile = Join-Path $repo 'requirements-embedded.txt'
$tagReqFile = Join-Path $repo 'requirements-tag.txt'

Write-Host "[env] 仓库: $repo"

# ---------------------------------------------------------------- 1. Python
if (-not $PythonZip) {
    if (-not (Test-Path $initDir)) {
        throw "找不到 $initDir。把 python-3.x.y-embed-amd64.zip 放进去，或用 -PythonZip 指定路径。"
    }
    $PythonZip = Get-ChildItem $initDir -Filter 'python-*-embed-amd64.zip' |
        Sort-Object Name | Select-Object -Last 1 -ExpandProperty FullName
}
if (-not $PythonZip -or -not (Test-Path $PythonZip)) {
    throw "embeddable 包不存在: $PythonZip"
}

$py = Join-Path $pyDir 'python.exe'
if ($Force -or -not (Test-Path $py)) {
    if (Test-Path $pyDir) {
        Write-Host "[env] 删除旧环境: $pyDir"
        Remove-Item $pyDir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $pyDir | Out-Null
    Write-Host "[env] 解压 $(Split-Path $PythonZip -Leaf)"
    Expand-Archive -Path $PythonZip -DestinationPath $pyDir -Force
} else {
    Write-Host "[env] 已存在 $pyDir，跳过解压（换版本请加 -Force）"
}

# ---------------------------------------------------------------- 2. _pth
# embeddable 版默认只把 .zip 和当前目录放进 sys.path，且 site 是注释掉的。
# 不改这里，pip 装的东西根本不会被导入。
$pth = Get-ChildItem $pyDir -Filter '*._pth' | Select-Object -First 1
if (-not $pth) { throw "embedded python 里没有 ._pth 文件，包可能有误" }
$zipName = "$($pth.BaseName).zip"
@(
    $zipName
    '.'
    'Lib'
    'Lib\site-packages'
    ''
    'import site'
) | Set-Content -Path $pth.FullName -Encoding ascii
Write-Host "[env] 写入 $($pth.Name)（启用 Lib\site-packages 与 site）"

New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null

# ---------------------------------------------------------------- 3. pip
if (-not (Test-Path (Join-Path $sitePackages 'pip\__init__.py'))) {
    # 用 ensurepip 里那份 wheel（纯 Python，跨版本可用），解压即算装好，
    # 不需要联网也不需要先有 pip —— 这是个先有鸡还是先有蛋的问题。
    $wheel = Get-ChildItem $initDir -Filter 'pip-*.whl' -ErrorAction SilentlyContinue |
        Sort-Object Name | Select-Object -Last 1
    if (-not $wheel) {
        throw "env\initialization 下没有 pip-*.whl。拷一个过来（任何 Python 的 Lib\ensurepip\_bundled 里都有）。"
    }
    Write-Host "[env] 引导 pip: $($wheel.Name)"
    $code = Invoke-Native $py @('-m', 'zipfile', '-e', $wheel.FullName, $sitePackages)
    if ($code -ne 0) { throw "pip 解压失败（退出码 $code）" }
}

# ---------------------------------------------------------------- 4. 仓库路径
# 让 env\python 里任何位置都能 import tagger，不必 pip install -e（那要 setuptools）。
# 路径按当前仓库位置写，所以换台机器解压后要重跑本脚本一次。
# 必须 UTF-8（无 BOM）：站点模块按 UTF-8 解 .pth，而仓库目录带中文时
# ascii 会把路径写成问号，结果是"装了但 import 不到 tagger"。
[System.IO.File]::WriteAllText(
    (Join-Path $sitePackages '_tagger_repo.pth'),
    $repo + [Environment]::NewLine,
    (New-Object System.Text.UTF8Encoding($false)))
Write-Host "[env] 写入 _tagger_repo.pth -> $repo"

# ---------------------------------------------------------------- 5. 依赖
if (Test-Path $reqFile) {
    # -q 只压掉"Requirement already satisfied"和进度条，错误仍然会打出来。
    # 重装一次刷 16 行 already satisfied，会让人以为每次都真装了一遍。
    $pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check',
        '--no-warn-script-location', '-q', '-r', $reqFile)
    if ($WheelDir) {
        $resolved = Resolve-Path $WheelDir -ErrorAction SilentlyContinue
        if (-not $resolved) { throw "离线 wheel 目录不存在: $WheelDir" }
        $pipArgs += @('--no-index', '--find-links', $resolved.Path)
        Write-Host "[env] 离线安装，来源 $($resolved.Path)"
    } elseif ($IndexUrl) {
        # 只在这里加：--no-index 与 --index-url 同时给会互相打架。
        $pipArgs += @('--index-url', $IndexUrl)
        Write-Host "[env] pip 索引: $IndexUrl"
    }
    Write-Host "[env] 安装 requirements-embedded.txt"
    $code = Invoke-Native $py $pipArgs
    if ($code -ne 0) {
        # 这两种失败的下一步完全相反，不能合并成一句"网络受限"：
        #   镜像滞后 → 换源；真没网 → 离线包。照着错的那条走会白折腾。
        throw @"
pip install 失败（退出码 $code）。

若上面是 "No matching distribution found" / "Could not find a version"，
且 pip 列出的可用版本停在某个旧版本：那是 pip 用的镜像滞后了，不是清单写错。
  查当前源      env\python\python.exe -m pip config list
  换官方源重跑  install.bat -IndexUrl https://pypi.org/simple

若上面是 ConnectionError / timeout / 代理报错：那是网络问题。
  install.bat -WheelDir env\initialization\wheels
"@
    }
} else {
    Write-Warning "没找到 $reqFile，跳过依赖安装"
}

# ---------------------------------------------------------------- 5b. 打标运行时
# 默认 tag 后端是 pixai，它要 transformers + torch + timm / safetensors。这几样
# 以前只写在文档里"按需自己装"，结果是任何人按标准流程装完，第一次跑打标就
# 得到 "tag 后端不可用：pip install torch ..."——而用户没有任何理由知道这句
# 话该去哪敲。所以这里默认装，-SkipTag 才跳过。
#
# 分两批装，因为 torch 的 wheel 不在 PyPI：
#   1. torch / torchvision  → PyTorch 自己的索引（cu126 或 cpu）
#   2. 其余（transformers / timm / safetensors）→ 普通索引
# 清单只有 requirements-tag.txt 一份，批怎么分由包名前缀决定。
if ($SkipTag) {
    Write-Host "[env] 跳过打标运行时（-SkipTag）"
} elseif (-not (Test-Path $tagReqFile)) {
    Write-Warning "[env] 没找到 $tagReqFile，跳过打标运行时（打标会失败）"
} else {
    $entries = @(Get-Content $tagReqFile |
        Where-Object { $_ -and -not $_.TrimStart().StartsWith('#') } |
        ForEach-Object { $_.Trim() })
    $torchReqs = @($entries | Where-Object { $_ -match '^(torch|torchvision)\b' })
    $otherReqs = @($entries | Where-Object { $_ -notmatch '^(torch|torchvision)\b' })

    if (-not $TorchIndex) {
        # 有卡就装 CUDA 版：CPU 版跑 tag 反推是 10 张/秒量级，装错了会让人
        # 以为"这软件就这么慢"。没卡只能 CPU，那也是唯一能跑的。
        if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
            $TorchIndex = 'https://download.pytorch.org/whl/cu126'
        } else {
            $TorchIndex = 'https://download.pytorch.org/whl/cpu'
            Write-Host "[env] 没检测到 nvidia-smi，装 CPU 版 torch"
        }
    }

    # 离线模式：只有 wheel 目录里真的有 torch 才按离线装。没有就跳过并警告——
    # 硬失败会让"我只想打个包"的人连 CLI 都装不上。
    $tagOffline = $false
    $wheelPath = ""
    if ($WheelDir) {
        $resolvedWheels = Resolve-Path $WheelDir -ErrorAction SilentlyContinue
        if ($resolvedWheels) {
            $wheelPath = $resolvedWheels.Path
            if (Get-ChildItem $wheelPath -Filter 'torch-*.whl' -ErrorAction SilentlyContinue) {
                $tagOffline = $true
            } else {
                Write-Warning "[env] $wheelPath 里没有 torch 的 wheel，跳过打标运行时（打标会失败）"
            }
        }
    }

    $tagOk = $true
    if ($tagOffline -or -not $WheelDir) {
        Write-Host "[env] 安装打标运行时（torch 来源: $TorchIndex）"
        Write-Host "      首次约 2.5 GB，装过一次之后就是秒过。"
        $pArgs = @('-m', 'pip', 'install', '--disable-pip-version-check',
            '--no-warn-script-location', '-q') + $torchReqs
        if ($tagOffline) {
            $pArgs += @('--no-index', '--find-links', $wheelPath)
        } else {
            $pArgs += @('--index-url', $TorchIndex)
        }
        $code = Invoke-Native $py $pArgs
        if ($code -ne 0) { $tagOk = $false }

        if ($tagOk -and $otherReqs.Count -gt 0) {
            $pArgs = @('-m', 'pip', 'install', '--disable-pip-version-check',
                '--no-warn-script-location', '-q') + $otherReqs
            if ($tagOffline) {
                $pArgs += @('--no-index', '--find-links', $wheelPath)
            } elseif ($IndexUrl) {
                $pArgs += @('--index-url', $IndexUrl)
            }
            $code = Invoke-Native $py $pArgs
            if ($code -ne 0) { $tagOk = $false }
        }
    }

    if ($tagOk) {
        $tagProbe = "import torch, torchvision, transformers, timm, safetensors; print('  tag runtime: torch', torch.__version__, '| cuda', torch.cuda.is_available(), '| transformers', transformers.__version__)"
        $tagCode = Invoke-Native $py @('-c', $tagProbe)
        if ($tagCode -ne 0) { $tagOk = $false }
    }
    # 这三个不是每个后端都要：onnxruntime 只服务 cl-tagger-v2，open_clip 只
    # 服务图文一致度打分，datasets 只在 HF 输入源时用。缺了打标照样跑，所以
    # 只报状态、不判失败——否则一次网络抖动就会让人以为整个环境坏了。
    if ($tagOk) {
        foreach ($opt in @(@('onnxruntime', 'cl-tagger-v2 后端'), @('open_clip', '图文一致度打分'), @('datasets', 'HF 数据集输入'))) {
            $optCode = Invoke-Native $py @('-c', "import $($opt[0])") -Quiet
            if ($optCode -ne 0) {
                Write-Host "  可选缺失：$($opt[0])（$($opt[1])）—— 需要时 pip install $($opt[0])"
            }
        }
    }
    if (-not $tagOk) {
        # 这里**不能 throw**：装不上 torch 只影响打标，打包与 NL 仍然可用。
        # 把它变成硬失败，等于让人为了一个可选依赖连界面都进不去。
        Write-Warning @"
打标运行时没装完（打标任务会失败，打包与 NL 不受影响）。手动重试：
  有 N 卡：
    env\python\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126
    env\python\python.exe -m pip install -r requirements-tag.txt
  没有 N 卡：把上面的 cu126 换成 cpu
  只想打包、不打标：install.bat -SkipTag 可以彻底跳过这一步
"@
    }
}

# ---------------------------------------------------------------- 6. NL 引擎
# 两个引擎的安装方式完全不同：
#   llamacpp —— 不需要任何 Python 包，只要一个 llama-server.exe。它是
#               **默认引擎**，没有它连服务都起不来。安装逻辑全在
#               tagger/llama_runtime.py（与网页「下载并安装」按钮、CLI
#               `tagger llama install` 是同一份实现、同一个落点），这里只是
#               在装完依赖后调它一次，免得"装完 install.bat 还要再点一次按钮"。
#               它自己会去 GitHub releases 挑 win-cuda / win-vulkan / win-cpu
#               并连带 cudart 一起下（约 570MB）。
#   vllm     —— 只有 Linux / WSL2 能装，Windows 上这里永远报"不可用"。
#
# 为什么失败不 throw：装不上 llama-server 只影响 NL 打标。硬失败会让
# "只想打 tag / 只想打包"的人连环境都装不完——和上面 tag 运行时同一个道理。
Write-Host "[env] NL 引擎:"
$llamaOk = $false
$llamaReason = ""
if ($NoLlama) {
    Write-Host "  llamacpp : 跳过（-NoLlama）"
    $llamaReason = 'skipped'
} else {
    # 先探一次：已经装过（或 PATH 里有）就不重下 570MB。
    # status() 是纯本地查询（不联网、不跑 nvidia-smi），几十毫秒。
    $envPy = Join-Path $pyDir 'python.exe'
    $statusJson = ""
    try {
        $statusJson = & $envPy -c "import json,sys;from tagger import llama_runtime as r;print(json.dumps(r.status()))" 2>$null
    } catch { $statusJson = "" }
    $st = $null
    if ($statusJson) { try { $st = $statusJson | ConvertFrom-Json } catch { $st = $null } }

    if ($st -and $st.installed) {
        Write-Host "  llamacpp : OK ($($st.binary))"
        if ($st.version) { Write-Host "             构建 $($st.version)" }
        $llamaOk = $true
    } elseif ($st -and -not $st.supported) {
        Write-Host "  llamacpp : 当前平台（$($st.platform)）没有官方预编译包"
        Write-Host "             需要自己编译，然后设 TAGGER_LLAMA_SERVER 指向 llama-server"
        $llamaReason = 'unsupported'
    } else {
        Write-Host "  llamacpp : 未安装，开始下载（约 570MB，含 cudart）…"
        # llama install 是同步的：内部起线程并轮询到结束，出错时返回非 0。
        $code = Invoke-Native $py @('-m', 'tagger.cli', 'llama', 'install')
        if ($code -eq 0) {
            Write-Host "  llamacpp : OK"
            $llamaOk = $true
        } else {
            $llamaReason = 'download-failed'
            Write-Host "  llamacpp : 自动安装失败（退出码 $code）"
            Write-Host "             GitHub 不通时先设 HTTPS_PROXY，或手动下 zip 后："
            Write-Host "               env\python\python.exe -m tagger.cli llama install --zip D:\llama.zip"
        }
    }
}
$vllmCmd = Get-Command vllm -ErrorAction SilentlyContinue
if ($vllmCmd -and $env:OS -ne 'Windows_NT') {
    Write-Host "  vllm     : OK ($($vllmCmd.Source))"
} else {
    Write-Host "  vllm     : 不可用（只支持 Linux / WSL2）"
}

# ---------------------------------------------------------------- 7. 自检
Write-Host "[env] 自检:"
$probe = "import sys, httpx, yaml, PIL, pyarrow; print('  python', sys.version.split()[0]); print('  httpx', httpx.__version__); print('  pyarrow', pyarrow.__version__); print('  pillow', PIL.__version__)"
$null = Invoke-Native $py @('-c', $probe)
# web 栈单独探：它不在上面那条主链里，缺了要在这里就暴露，而不是等用户
# 双击壳子、点开界面才看到 "缺 web 依赖"（那是这个包最常见的首次报错）。
$webProbe = "import fastapi, uvicorn, websockets; print('  web', fastapi.__version__, uvicorn.__version__, websockets.__version__)"
$webCode = Invoke-Native $py @('-c', $webProbe)
if ($webCode -ne 0) {
    Write-Warning "  web deps 缺失：env\python\python.exe -m pip install -r requirements-embedded.txt"
}
$helpCode = Invoke-Native $py @('-m', 'tagger.cli', '--help') -Quiet
Write-Host "  tagger.cli --help: $(if ($helpCode -eq 0) { 'OK' } else { "失败（退出码 $helpCode）" })"

# 模型权重单独探：装完环境不等于能打标。tiers.yaml 默认指向 abliterated
# 破限版，而那份 GGUF 是要另外下的（几 GB）。不在这里说清楚，用户点下
# 「启动服务」拿到的会是"GGUF 权重不存在"，但原因和他刚才跑的 install 无关。
Write-Host "[env] 模型权重:"
try {
    $wJson = & $envPy -c "import json;from tagger.config import load_tier_table, resolve_profile;from tagger.hardware import query_gpus;t=load_tier_table();g=query_gpus();p=resolve_profile(t,g[0]) if g else None;print(json.dumps({'key':p.model.key if p else '','gguf':str(p.model.gguf or '') if p else '','mmproj':str(p.model.mmproj or '') if p else ''}))" 2>$null
    $w = $null
    if ($wJson) { try { $w = $wJson | ConvertFrom-Json } catch { $w = $null } }
    if ($w -and $w.gguf) {
        $ggufPath = $w.gguf
        if (-not [System.IO.Path]::IsPathRooted($ggufPath)) { $ggufPath = Join-Path $repo $ggufPath }
        if (Test-Path $ggufPath) {
            Write-Host "  $($w.key) : OK"
        } else {
            Write-Host "  $($w.key) : 权重未下载"
            Write-Host "             打开界面 →「模型管理」→ 点该条目的「下载」"
            Write-Host "             （破限版从 mradermacher 的 GGUF 仓库取，约 3~6GB）"
        }
    }
} catch {
    Write-Host "  （无法解析分档表，跳过）"
}

Write-Host ""
Write-Host "[env] 完成。用法："
Write-Host "       .\tagger.cmd --help"
Write-Host "       env\python\python.exe -m pytest"
Write-Host "  起 NL 服务（Windows 原生）："
Write-Host "       env\python\python.exe -m tagger.cli serve --engine llamacpp"
if (-not $llamaOk) {
    Write-Host "  装 llama-server（网页上也有按钮）："
    Write-Host "       env\python\python.exe -m tagger.cli llama install"
}
if ($SkipTag) {
    Write-Host "  注意：本次用了 -SkipTag，打标运行时没装，跑打标前重跑 install.bat"
}
