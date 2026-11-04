# 把 WPF 壳子发布到 dist\desktop，产物直接双击可用。
#
# 四种组合：
#   powershell -File desktop\build.ps1                          依赖框架、多文件（默认，几 MB）
#   powershell -File desktop\build.ps1 -SingleFile              依赖框架、单文件（约 2 MB）
#   powershell -File desktop\build.ps1 -SelfContained           自带运行时、多文件（约 150 MB）
#   powershell -File desktop\build.ps1 -SingleFile -SelfContained 自带运行时、单文件（约 65 MB）
#
# 依赖框架版要求目标机器装了 .NET 8+ 桌面运行时；装了更高版本也能跑（RollForward）。
# 单文件版只产出一个 exe，可以随便挪——包括直接放进仓库根目录。
param(
    [switch]$SelfContained,
    [switch]$SingleFile,
    [string]$Runtime = "win-x64"
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
$proj = Join-Path $here "Tagger.Shell\Tagger.Shell.csproj"
# 单文件和多文件分开放，免得单文件目录里混着上一轮的多文件产物，看不出该双击哪个。
$out  = Join-Path $root $(if ($SingleFile) { "dist\desktop-single" } else { "dist\desktop" })

if (-not (Test-Path $proj)) {
    throw "找不到项目文件：$proj"
}

# 前端也得构建，否则壳子打开的是"请先 npm run build"的提示页。
$dist = Join-Path $root "tagger\web\ui\dist\index.html"
if (-not (Test-Path $dist)) {
    Write-Host "[build] 前端还没构建，先跑 npm run build" -ForegroundColor Yellow
    $ui = Join-Path $root "tagger\web\ui"
    Push-Location $ui
    try {
        if (-not (Test-Path (Join-Path $ui "node_modules"))) { npm install }
        npm run build
    }
    finally { Pop-Location }
}

# 注意别用 $args：那是 PowerShell 的自动变量，覆盖它会让人以为能传参进来。
$publishArgs = @(
    "publish", $proj,
    "-c", "Release",
    "-r", $Runtime,
    "-o", $out
)

if ($SingleFile) {
    $publishArgs += @(
        "-p:PublishSingleFile=true",
        # WebView2Loader.dll 是原生库，不这样处理会在运行时找不到它。
        "-p:IncludeNativeLibrariesForSelfExtract=true",
        # 符号嵌进 exe：既保住异常堆栈里的行号，又不额外留一个 .pdb 在旁边。
        "-p:DebugType=embedded"
    )
    $publishArgs += "-p:PublishTrimmed=false"
    if ($SelfContained) {
        # 自包含单文件动辄 150MB，压缩后能砍到一半多；代价是首次启动要解压几秒。
        $publishArgs += "-p:EnableCompressionInSingleFile=true"
    }
} else {
    $publishArgs += "-p:PublishSingleFile=false"
}

if ($SelfContained) {
    $publishArgs += @("--self-contained", "true", "-p:PublishTrimmed=false")
} else {
    $publishArgs += @("--self-contained", "false")
}

Write-Host "[build] dotnet $($publishArgs -join ' ')" -ForegroundColor DarkGray
& dotnet @publishArgs
if ($LASTEXITCODE -ne 0) { throw "dotnet publish 失败（退出码 $LASTEXITCODE）" }

$exe = Join-Path $out "Tagger.Shell.exe"
if (-not (Test-Path $exe)) { throw "发布成功但没找到 $exe" }

# 从 dist\desktop 往上两级就是仓库根，壳子靠这个自己找 Python。
Write-Host ""
Write-Host "[build] 完成：$exe" -ForegroundColor Green
Write-Host "[build] 双击即可启动；它会在仓库里找 env\python\python.exe 并起本地服务。" -ForegroundColor DarkGray
if ($SingleFile) {
    $files = @(Get-ChildItem $out -File)
    Write-Host "[build] 目录里共 $($files.Count) 个文件：$($files.Name -join ', ')" -ForegroundColor DarkGray
}
if (-not $SelfContained) {
    Write-Host "[build] 依赖框架版：目标机器需要 .NET 8+ 桌面运行时（缺失时程序会提示并跳到下载页）。" -ForegroundColor DarkGray
}
