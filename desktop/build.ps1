# 把 WPF 壳子发布到 dist\desktop，产物直接双击可用。
#
# 默认发布"依赖框架"版本（几 MB），要求机器上有 .NET 8+ 桌面运行时——
# 本机已有，且 RollForward 让它也能跑在更高版本上。要发给没装运行时的机器
# 时加 -SelfContained，体积约 150MB，但拷贝即用。
param(
    [switch]$SelfContained,
    [string]$Runtime = "win-x64"
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
$proj = Join-Path $here "Tagger.Shell\Tagger.Shell.csproj"
$out  = Join-Path $root "dist\desktop"

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

$args = @(
    "publish", $proj,
    "-c", "Release",
    "-r", $Runtime,
    "-o", $out,
    "-p:PublishSingleFile=false"
)

if ($SelfContained) {
    $args += @("--self-contained", "true", "-p:PublishTrimmed=false")
} else {
    $args += @("--self-contained", "false")
}

Write-Host "[build] dotnet $($args -join ' ')" -ForegroundColor DarkGray
& dotnet @args
if ($LASTEXITCODE -ne 0) { throw "dotnet publish 失败（退出码 $LASTEXITCODE）" }

$exe = Join-Path $out "Tagger.Shell.exe"
if (-not (Test-Path $exe)) { throw "发布成功但没找到 $exe" }

# 从 dist\desktop 往上两级就是仓库根，壳子靠这个自己找 Python。
Write-Host ""
Write-Host "[build] 完成：$exe" -ForegroundColor Green
Write-Host "[build] 双击即可启动；它会在仓库里找 env\python\python.exe 并起本地服务。" -ForegroundColor DarkGray
