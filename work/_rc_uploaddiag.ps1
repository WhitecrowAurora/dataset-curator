$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
"PY_UPLOAD=" + ((Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*upload_artist_shards*' } | ForEach-Object { $_.ProcessId }) -join ',')
foreach ($f in @('K:\anlia\_artist\upload.log', 'K:\anlia\_artist\upload.err')) {
    if (Test-Path $f) { "FILE $f size=" + (Get-Item $f).Length + " mtime=" + (Get-Item $f).LastWriteTime.ToString('HH:mm:ss') }
}
"--- RUN FOREGROUND ---"
$env:HF_ENDPOINT = 'https://huggingface.co'
& 'C:\py312\python.exe' -u 'C:\Users\anlia\AppData\Local\Temp\upload_artist_shards.py' 2>&1 | Select-Object -First 12
