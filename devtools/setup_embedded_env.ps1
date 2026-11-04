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

.PARAMETER Force
    删掉 env\python 从头重建。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File devtools\setup_embedded_env.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File devtools\setup_embedded_env.ps1 -Force -WheelDir env\initialization\wheels
#>
[CmdletBinding()]
param(
    [string]$PythonZip = "",
    [string]$WheelDir = "",
    [switch]$Force
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
Set-Content -Path (Join-Path $sitePackages '_tagger_repo.pth') -Value $repo -Encoding ascii
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
    }
    Write-Host "[env] 安装 requirements-embedded.txt"
    $code = Invoke-Native $py $pipArgs
    if ($code -ne 0) {
        throw "pip install 失败（退出码 $code）。网络受限时用 -WheelDir 走离线包。"
    }
} else {
    Write-Warning "没找到 $reqFile，跳过依赖安装"
}

# ---------------------------------------------------------------- 6. NL 引擎
# 两个引擎的安装方式完全不同，这里只做"能不能用"的判定并给出下一步：
#   llamacpp —— 不需要任何 Python 包，只要一个 llama-server.exe。
#               预编译包 565MB（含 cudart），且必须与 CUDA 版本对得上，
#               所以不在这里自动下；版本选错会得到一堆看不懂的 kernel 报错。
#   vllm     —— 只有 Linux / WSL2 能装，Windows 上这里永远报"不可用"。
Write-Host "[env] NL 引擎:"
$llamaServer = $env:TAGGER_LLAMA_SERVER
if (-not $llamaServer -or -not (Test-Path $llamaServer)) {
    $onPath = Get-Command llama-server -ErrorAction SilentlyContinue
    if ($onPath) { $llamaServer = $onPath.Source } else { $llamaServer = "" }
}
if ($llamaServer) {
    Write-Host "  llamacpp : OK ($llamaServer)"
} else {
    Write-Host "  llamacpp : 未安装（Windows 原生唯一的 GPU 路径）"
    Write-Host "             从 llama.cpp 的 releases 下 llama-*-bin-win-cuda-*.zip"
    Write-Host "             与同版本 cudart-*.zip，解压后设："
    Write-Host "               setx TAGGER_LLAMA_SERVER D:\llama\llama-server.exe"
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
$helpCode = Invoke-Native $py @('-m', 'tagger.cli', '--help') -Quiet
Write-Host "  tagger.cli --help: $(if ($helpCode -eq 0) { 'OK' } else { "失败（退出码 $helpCode）" })"

Write-Host ""
Write-Host "[env] 完成。用法："
Write-Host "       .\tagger.cmd --help"
Write-Host "       env\python\python.exe -m pytest"
Write-Host "  起 NL 服务（Windows 原生）："
Write-Host "       env\python\python.exe -m tagger.cli serve --engine llamacpp"
Write-Host "  装大包（按需，装进同一个环境）："
Write-Host "       env\python\python.exe -m pip install torch torchvision"
