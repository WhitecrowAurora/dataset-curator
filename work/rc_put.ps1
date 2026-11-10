# Upload a local file to the remote worker (reuses password from rc.ps1, never printed).
# Usage: powershell -File work/rc_put.ps1 -Local <path> -Remote <remote path>
param(
    [Parameter(Mandatory = $true)][string]$Local,
    [Parameter(Mandatory = $true)][string]$Remote
)
$ErrorActionPreference = 'Stop'

$target = 'anlia@121.225.25.33'
$hostFile = 'S:\tools\rc.host'
if (Test-Path $hostFile) {
    $t = (Get-Content $hostFile -Raw -Encoding UTF8) -replace '\s', ''
    if ($t) { $target = $t }
}

$m = Select-String -Path 'S:\tools\rc.ps1' -Pattern "^\`$pw = '(.+)'" | Select-Object -First 1
if (-not $m) { throw 'password line not found in rc.ps1' }
$pw = $m.Matches[0].Groups[1].Value

$ap = Join-Path $env:TEMP 'askpass_put.cmd'
Set-Content -Path $ap -Value "@echo off`r`necho %SSH_PW%" -Encoding ascii
$env:SSH_PW = $pw
$env:SSH_ASKPASS = $ap
$env:SSH_ASKPASS_REQUIRE = 'force'
$env:DISPLAY = 'nul'

$sw = [Diagnostics.Stopwatch]::StartNew()
& scp -o ConnectTimeout=20 -o PreferredAuthentications=password -o PubkeyAuthentication=no -o StrictHostKeyChecking=accept-new $Local "${target}:$Remote"
if ($LASTEXITCODE -ne 0) { throw "scp failed with exit code $LASTEXITCODE" }
"OK {0:N1}s {1:N1}MB" -f $sw.Elapsed.TotalSeconds, ((Get-Item $Local).Length / 1MB)
