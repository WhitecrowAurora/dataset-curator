$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
$log = 'K:\anlia\_artist\shard_build.log'
$err = 'K:\anlia\_artist\shard_build.err'
$p = Start-Process -FilePath 'C:\py312\python.exe' `
    -ArgumentList @('C:\Users\anlia\AppData\Local\Temp\shard_artists.py') `
    -RedirectStandardOutput $log -RedirectStandardError $err -WindowStyle Hidden -PassThru
"PID=" + $p.Id
Start-Sleep -Seconds 40
"--- LOG ---"
if (Test-Path $log) { Get-Content $log -Tail 8 }
"--- ERR ---"
if (Test-Path $err) { Get-Content $err -Tail 5 }
