"""按 768 分发分片的**实际成员**切画师表，一个 tar 配一个 parquet。

为什么不按 manifest 的字节数推算边界：那个是"累加到 1750MB 就封口"的估算，
只要有一次计数差一行，后面每一片的画师都会错位，而错位是静默的。
这里直接读 tar 成员名（只读 header，不读图片数据），以分片自己的内容为准。

产物：K:\\anlia\\_artist\\shards\\shard-XXXXXX.artist.parquet
    列 id:int64 / artists:list<str> / sources:list<str>
    文件级 metadata: shard / rows / matched / built_at / source（供加载时配对校验）
"""
from __future__ import annotations

import json
import tarfile
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(r"K:\anlia\danbooru2024_768")
TABLE = Path(r"K:\anlia\_artist\danbooru_artists.parquet")
OUT = Path(r"K:\anlia\_artist\shards")
OUT.mkdir(parents=True, exist_ok=True)

BASE = pa.schema([
    pa.field("id", pa.int64()),
    pa.field("artists", pa.list_(pa.string())),
    pa.field("sources", pa.list_(pa.string())),
])


def load_table() -> dict[int, tuple[list[str], list[str]]]:
    t = pq.read_table(TABLE, columns=["id", "artists", "sources"])
    ids = t.column("id").to_pylist()
    arts = t.column("artists").to_pylist()
    srcs = t.column("sources").to_pylist()
    return {i: (a, s) for i, a, s in zip(ids, arts, srcs)}


def member_ids(tar: Path) -> list[int]:
    out: list[int] = []
    with tarfile.open(tar) as tf:
        for m in tf:
            if not m.isfile():
                continue
            stem = m.name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if stem.isdigit():
                out.append(int(stem))
    return out


def main() -> int:
    t0 = time.time()
    table = load_table()
    print(f"[load] 画师表 {len(table)} 条", flush=True)

    shards = sorted(ROOT.glob("shard-*.tar"))
    print(f"[scan] {len(shards)} 个分片", flush=True)

    total = matched = 0
    done = 0
    for tar in shards:
        ids = member_ids(tar)
        row_id, row_art, row_src = [], [], []
        hit = 0
        for i in ids:
            arts, srcs = table.get(i, ([], []))
            row_id.append(i)
            row_art.append(arts)
            row_src.append(srcs)
            if arts:
                hit += 1
        meta = {
            b"shard": tar.name.encode(),
            b"rows": str(len(ids)).encode(),
            b"matched": str(hit).encode(),
            b"built_at": time.strftime("%Y-%m-%dT%H:%M:%S").encode(),
            b"source": b"danbooru_artists.parquet (NewBie-Training-XML-Caption)",
        }
        tbl = pa.Table.from_arrays(
            [pa.array(row_id, pa.int64()),
             pa.array(row_art, pa.list_(pa.string())),
             pa.array(row_src, pa.list_(pa.string()))],
            schema=BASE).replace_schema_metadata(meta)
        pq.write_table(tbl, OUT / (tar.stem + ".artist.parquet"), compression="zstd")

        total += len(ids)
        matched += hit
        done += 1
        if done % 50 == 0 or done == len(shards):
            print(f"  {done}/{len(shards)} rows={total} matched={matched} "
                  f"({matched/max(1,total):.1%}) {int(time.time()-t0)}s", flush=True)

    stats = {
        "shards": len(shards),
        "rows": total,
        "matched": matched,
        "coverage": round(matched / total, 4) if total else 0,
        "out_dir": str(OUT),
        "bytes": sum(f.stat().st_size for f in OUT.glob("*.parquet")),
        "seconds": round(time.time() - t0),
    }
    (OUT.parent / "shards_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
