$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
$py = 'C:\py312\python.exe'
$code = @'
import tarfile, random
from pathlib import Path
import pyarrow.parquet as pq

ROOT = Path(r"K:\anlia\danbooru2024_768")
SH = Path(r"K:\anlia\_artist\shards")
GLOBAL = pq.read_table(r"K:\anlia\_artist\danbooru_artists.parquet", columns=["id", "artists"])
gmap = dict(zip(GLOBAL.column("id").to_pylist(), GLOBAL.column("artists").to_pylist()))

def member_ids(tar):
    out = []
    with tarfile.open(tar) as tf:
        for m in tf:
            if m.isfile():
                stem = m.name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                if stem.isdigit():
                    out.append(int(stem))
    return out

for name in ("shard-000000.tar", "shard-000300.tar", "shard-000843.tar"):
    tar = ROOT / name
    par = SH / (tar.stem + ".artist.parquet")
    ids = member_ids(tar)
    tbl = pq.read_table(par)
    pids = tbl.column("id").to_pylist()
    arts = tbl.column("artists").to_pylist()
    meta = pq.read_schema(par).metadata or {}
    same_set = set(ids) == set(pids) and len(ids) == len(pids)
    print("=" * 60)
    print(name, "tar_ids=%d parquet_rows=%d same_set=%s" % (len(ids), len(pids), same_set))
    print("meta:", {k.decode(): v.decode() for k, v in meta.items()})
    random.seed(7)
    for i in random.sample(range(len(ids)), 4):
        pid, art = ids[i], arts[i]
        agree = art == gmap.get(pid, [])
        print("  %-9d shard=%-16s global=%-16s agree=%s"
              % (pid, art, gmap.get(pid, []), agree))
'@
$tmp = Join-Path $env:TEMP 'verify_shards.py'
[IO.File]::WriteAllText($tmp, $code, (New-Object Text.UTF8Encoding($false)))
& $py $tmp
