$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
$log = 'K:\anlia\_artist\upload.log'
$err = 'K:\anlia\_artist\upload.err'
$cmd = 'C:\py312\python.exe C:\Users\anlia\AppData\Local\Temp\upload_artist_shards.py > "' + $log + '" 2> "' + $err + '"'
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
"PID=" + $r.ProcessId + " RET=" + $r.ReturnValue
Start-Sleep -Seconds 45
"--- LOG ---"
if (Test-Path $log) { Get-Content $log -Tail 8 }
"--- ERR ---"
if (Test-Path $err) { Get-Content $err -Tail 8 }
