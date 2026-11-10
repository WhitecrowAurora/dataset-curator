$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
$log = 'K:\anlia\_artist\shard_build.log'
$err = 'K:\anlia\_artist\shard_build.err'
"ALIVE=" + [bool](Get-Process -Id 165676 -ErrorAction SilentlyContinue)
"OUT_FILES=" + (Get-ChildItem 'K:\anlia\_artist\shards' -Filter '*.parquet' -ErrorAction SilentlyContinue).Count
"--- LOG ---"
if (Test-Path $log) { Get-Content $log -Tail 6 }
"--- ERR ---"
if (Test-Path $err) { Get-Content $err -Tail 4 }
