$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
$py = 'C:\py312\python.exe'
$code = @'
import tarfile, sys
for name in ("shard-000000.tar", "shard-000100.tar"):
    p = r"K:\anlia\danbooru2024_768\\" + name
    try:
        with tarfile.open(p) as tf:
            names = []
            for i, m in enumerate(tf):
                names.append(m.name)
                if i >= 4:
                    break
        print(name, "first:", names)
    except Exception as exc:
        print(name, "ERR", type(exc).__name__, exc)
'@
$tmp = Join-Path $env:TEMP 'peek_members.py'
[IO.File]::WriteAllText($tmp, $code, (New-Object Text.UTF8Encoding($false)))
& $py $tmp
"SHARD_COUNT=" + (Get-ChildItem 'K:\anlia\danbooru2024_768' -Filter 'shard-*.tar' -File).Count
