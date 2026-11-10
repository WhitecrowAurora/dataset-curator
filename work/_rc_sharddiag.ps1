$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
"PY_PROCS=" + ((Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    ForEach-Object { $_.ProcessId.ToString() + ': ' + $_.CommandLine }) -join ' || ')
$new = Get-ChildItem 'K:\anlia\_artist\shards' -Filter '*.parquet' -ErrorAction SilentlyContinue |
       Sort-Object LastWriteTime -Descending | Select-Object -First 3
"NEWEST=" + (($new | ForEach-Object { $_.Name + ' ' + $_.LastWriteTime.ToString('HH:mm:ss') }) -join ' | ')
"COUNT=" + (Get-ChildItem 'K:\anlia\_artist\shards' -Filter '*.parquet').Count
foreach ($f in @('K:\anlia\_artist\shard_build.log', 'K:\anlia\_artist\shard_build.err')) {
    if (Test-Path $f) { "$f size=" + (Get-Item $f).Length + " mtime=" + (Get-Item $f).LastWriteTime.ToString('HH:mm:ss') }
}
"MEM=" + [Math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB, 1) + "GB free"
