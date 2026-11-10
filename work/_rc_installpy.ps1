$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

if (Test-Path 'C:\py312\python.exe') {
    "ALREADY=" + (& 'C:\py312\python.exe' -c "import sys;print(sys.version.split()[0])" 2>&1)
    exit 0
}

$exe = Join-Path $env:TEMP 'py312_installer.exe'
if (-not (Test-Path $exe)) {
    "DOWNLOAD..."
    try {
        Invoke-WebRequest -Uri 'https://mirrors.huaweicloud.com/python/3.12.10/python-3.12.10-amd64.exe' `
            -OutFile $exe -TimeoutSec 600 -UseBasicParsing
    } catch {
        "DL_ERR=" + $_.Exception.Message
        exit 1
    }
}
"EXE_MB=" + [Math]::Round((Get-Item $exe).Length / 1MB, 1)

$p = Start-Process -FilePath $exe -Wait -PassThru -ArgumentList @(
    '/quiet', 'InstallAllUsers=0', 'TargetDir=C:\py312', 'Include_pip=1',
    'Include_test=0', 'Include_launcher=0', 'Include_doc=0', 'Shortcuts=0',
    'PrependPath=0', 'AssociateFiles=0', 'CompileAll=0'
)
"INSTALL_EXIT=" + $p.ExitCode
"PY=" + (& 'C:\py312\python.exe' -c "import sys;print(sys.version.split()[0], sys.executable)" 2>&1)
