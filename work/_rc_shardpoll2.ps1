$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
"PY_ALIVE=" + [bool](Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*shard_artists.py*' })
"COUNT=" + (Get-ChildItem 'K:\anlia\_artist\shards' -Filter '*.parquet' -ErrorAction SilentlyContinue).Count
"STATS=" + (Test-Path 'K:\anlia\_artist\shards_stats.json')
if (Test-Path 'K:\anlia\_artist\shards_stats.json') {
    Get-Content 'K:\anlia\_artist\shards_stats.json' -Raw
}
"--- LOG ---"
if (Test-Path 'K:\anlia\_artist\shard_build.log') { Get-Content 'K:\anlia\_artist\shard_build.log' -Tail 5 }
