"""从 HF 仓库流式抽取「danbooru post id -> 画师」全表，写成 parquet。

为什么要流式：源文件最大一份 28 GB，整份 json.load 不可能。这里按块读、
用 JSONDecoder.raw_decode 逐个对象解析，内存只留"已解析的 id -> 画师"。
e621 按需求排除；gelbooru(loli_*) 保留但 source 单独标。

产物：work/danbooru_artists.parquet
    列 id:int64 / artists:list<str> / sources:list<str>
    + 文件级 metadata（builder/built_at/source 清单）
"""
from __future__ import annotations

import codecs
import json
import pathlib
import re
import sys
import time

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

REPO = "MatchaCuratio/NewBie-Training-XML-Caption"
PREFIX = "train_dataest%2A2/"
BASE = f"https://huggingface.co/datasets/{REPO}/resolve/main/{PREFIX}"
OUT = pathlib.Path("work/danbooru_artists.parquet")
STATS = pathlib.Path("work/danbooru_artists_stats.json")

# (文件名, 字节数, source 标签)
FILES = [
    ("image_metadata_new.json", 28_181_155_297, "danbooru2024"),
    ("xmltags.json", 1_482_123_168, "danbooru2024"),
    ("caption.json", 1_785_355_566, "danbooru2024"),
    ("add_metadata.json", 941_744_887, "danbooru2024"),
    ("dan_add_xml.json", 1_482_123_168, "dan_2025_add"),
    ("dan_add_caption.json", 2_100_481_170, "dan_2025_add"),
    ("loli_xml.json", 99_461_373, "gelbooru"),
    ("loli_caption.json", 95_006_496, "gelbooru"),
]

_PATH_ID = re.compile(r"/(\d+)\.(?:jpg|jpeg|png|webp)$", re.I)
_ARTISTS = re.compile(r"<artists>\s*(.*?)\s*</artists>", re.S | re.I)
_SPLIT = re.compile(r"[\n,，、;]+")


def token() -> str:
    p = pathlib.Path.home() / ".cache" / "huggingface" / "token"
    return p.read_text().strip() if p.is_file() else ""


def iter_records(chunks, counter: dict):
    """流式把一个 JSON 数组拆成对象。counter 里累计已消费字节。"""
    dec = json.JSONDecoder()
    utf8 = codecs.getincrementaldecoder("utf-8")("replace")
    buf, pos = "", 0
    for raw in chunks:
        counter["bytes"] += len(raw)
        buf += utf8.decode(raw)
        while True:
            while pos < len(buf) and buf[pos] in " \r\n\t,[":
                pos += 1
            if pos >= len(buf):
                break
            if buf[pos] == "]":
                return
            try:
                obj, end = dec.raw_decode(buf, pos)
            except ValueError:
                break
            pos = end
            yield obj
        if pos > (1 << 20):
            buf, pos = buf[pos:], 0


def artist_list(text: str) -> list[str]:
    out: list[str] = []
    for m in _ARTISTS.finditer(text or ""):
        for name in _SPLIT.split(m.group(1)):
            name = name.strip().strip('"').strip()
            if name and name.lower() not in ("null", "none", "unknown"):
                out.append(name)
    # 保序去重
    seen, uniq = set(), []
    for n in out:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def main() -> int:
    tok = token()
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    table: dict[int, list] = {}          # id -> [artists_str, sources_str]
    per_file: dict[str, dict] = {}
    started = time.time()

    for name, size, source in FILES:
        counter = {"bytes": 0}
        recs = withs = 0
        last = time.time()
        print(f"[file] {name} ({size/2**30:.1f} GB) source={source}", flush=True)
        try:
            with httpx.stream("GET", BASE + name, headers=headers, timeout=120.0,
                              follow_redirects=True) as resp:
                resp.raise_for_status()
                for rec in iter_records(resp.iter_bytes(1 << 20), counter):
                    recs += 1
                    path = rec.get("path") or rec.get("image_path") or ""
                    m = _PATH_ID.search(str(path))
                    if not m:
                        continue
                    pid = int(m.group(1))
                    arts = []
                    for v in rec.values():
                        if isinstance(v, str) and "<artists>" in v:
                            arts = artist_list(v)
                            break
                    if not arts:
                        entry = table.get(pid)
                        if entry is None:
                            table[pid] = ["", source]
                        elif source not in entry[1]:
                            entry[1] += " " + source
                        continue
                    withs += 1
                    joined = "\n".join(arts)
                    entry = table.get(pid)
                    if entry is None:
                        table[pid] = [joined, source]
                    else:
                        have = entry[0].split("\n") if entry[0] else []
                        merged = have + [a for a in arts if a not in have]
                        entry[0] = "\n".join(merged)
                        if source not in entry[1]:
                            entry[1] += " " + source
                    if recs % 200_000 == 0:
                        gb = counter["bytes"] / 2**30
                        print(f"  {gb:.1f}GB recs={recs} with_artist={withs} "
                              f"ids={len(table)} {time.time()-last:.0f}s", flush=True)
                        last = time.time()
        except Exception as exc:                       # noqa: BLE001
            print(f"  [FAIL] {name}: {type(exc).__name__} {exc}", flush=True)
            per_file[name] = {"records": recs, "with_artist": withs, "error": str(exc)}
            continue
        per_file[name] = {"records": recs, "with_artist": withs,
                          "gb": round(counter["bytes"] / 2**30, 2), "source": source}
        print(f"  [done] {name} recs={recs} with_artist={withs} ids={len(table)}",
              flush=True)

    # ---- 落盘
    OUT.parent.mkdir(parents=True, exist_ok=True)
    schema = pa.schema([
        pa.field("id", pa.int64()),
        pa.field("artists", pa.list_(pa.string())),
        pa.field("sources", pa.list_(pa.string())),
    ])
    schema = schema.with_metadata({
        b"builder": b"tagger/work/build_artist_table.py",
        b"built_at": time.strftime("%Y-%m-%dT%H:%M:%S").encode(),
        b"repo": REPO.encode(),
        b"excluded": b"e621",
    })
    rows = 0
    with_art = 0
    with pq.ParquetWriter(OUT, schema, compression="zstd") as writer:
        batch_ids, batch_art, batch_src = [], [], []
        for pid, (arts, srcs) in table.items():
            batch_ids.append(pid)
            batch_art.append(arts.split("\n") if arts else [])
            batch_src.append(srcs.split(" "))
            if arts:
                with_art += 1
            rows += 1
            if len(batch_ids) >= 200_000:
                writer.write_table(pa.Table.from_arrays(
                    [pa.array(batch_ids, pa.int64()),
                     pa.array(batch_art, pa.list_(pa.string())),
                     pa.array(batch_src, pa.list_(pa.string()))], schema=schema))
                batch_ids, batch_art, batch_src = [], [], []
        if batch_ids:
            writer.write_table(pa.Table.from_arrays(
                [pa.array(batch_ids, pa.int64()),
                 pa.array(batch_art, pa.list_(pa.string())),
                 pa.array(batch_src, pa.list_(pa.string()))], schema=schema))

    stats = {
        "ids": rows,
        "ids_with_artist": with_art,
        "coverage": round(with_art / rows, 4) if rows else 0,
        "parquet_bytes": OUT.stat().st_size,
        "seconds": round(time.time() - started),
        "files": per_file,
    }
    STATS.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
