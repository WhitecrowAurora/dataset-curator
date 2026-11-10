$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
$log = 'K:\anlia\_artist\shard_build.log'
$err = 'K:\anlia\_artist\shard_build.err'
$cmd = 'C:\py312\python.exe C:\Users\anlia\AppData\Local\Temp\shard_artists.py > "' + $log + '" 2> "' + $err + '"'
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
"RETURN=" + $r.ReturnValue + " PID=" + $r.ProcessId
Start-Sleep -Seconds 50
"ALIVE=" + [bool](Get-Process -Id $r.ProcessId -ErrorAction SilentlyContinue)
"FILES=" + (Get-ChildItem 'K:\anlia\_artist\shards' -Filter '*.parquet' -ErrorAction SilentlyContinue).Count
"--- LOG ---"
if (Test-Path $log) { Get-Content $log -Tail 6 }
"--- ERR ---"
if (Test-Path $err) { Get-Content $err -Tail 4 }
