$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
"RUNNING=" + ((Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*upload_artist_shards*' } | ForEach-Object { $_.ProcessId }) -join ',')
foreach ($f in @('K:\anlia\_artist\upload.log', 'K:\anlia\_artist\upload.err')) {
    if (Test-Path $f) {
        "== $f size=" + (Get-Item $f).Length
        Get-Content $f -Tail 5
    }
}
