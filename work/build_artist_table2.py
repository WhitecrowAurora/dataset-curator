"""补齐 image_metadata_new.json（26 GB）：字节级流式解析 + Range 断点续传。

v1 的问题：httpx 流中途被服务器 reset，整份文件白跑。这里按"已解析到第几个
字节"记进度，断了就带 Range 从那里接着读；JSON 对象边界由字节状态机判定，
不依赖字符解码，所以不会在 UTF-8 中间截断。

用法：python work/build_artist_table2.py            # 只补大文件并合并旧表
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import time

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

REPO = "MatchaCuratio/NewBie-Training-XML-Caption"
BASE = f"https://huggingface.co/datasets/{REPO}/resolve/main/train_dataest%2A2/"
TARGET = "image_metadata_new.json"
OUT = pathlib.Path("work/danbooru_artists.parquet")
STATS = pathlib.Path("work/danbooru_artists_stats.json")

_PATH_ID = re.compile(r"/(\d+)\.(?:jpg|jpeg|png|webp)$", re.I)
_ARTISTS = re.compile(r"<artists>\s*(.*?)\s*</artists>", re.S | re.I)
_SPLIT = re.compile(r"[\n,，、;]+")


def token() -> str:
    p = pathlib.Path.home() / ".cache" / "huggingface" / "token"
    return p.read_text().strip() if p.is_file() else ""


def artist_list(text: str) -> list[str]:
    out: list[str] = []
    for m in _ARTISTS.finditer(text or ""):
        for name in _SPLIT.split(m.group(1)):
            name = name.strip().strip('"').strip()
            if name and name.lower() not in ("null", "none", "unknown"):
                out.append(name)
    seen, uniq = set(), []
    for n in out:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


class ObjStream:
    """把字节流切成一个个完整 JSON 对象，并记住"已消费到的绝对字节偏移"。"""

    def __init__(self, start_offset: int = 0):
        self.base = start_offset      # buf[0] 的绝对偏移
        self.buf = b""
        self.pos = 0                  # 下一个待扫描字节
        self.start = -1
        self.depth = 0
        self.in_str = False
        self.esc = False
        self.done_offset = start_offset

    def feed(self, chunk: bytes) -> list[dict]:
        self.buf += chunk
        out: list[dict] = []
        i = self.pos
        buf = self.buf
        n = len(buf)
        while i < n:
            c = buf[i]
            if self.start < 0:
                if c == 0x7B:                     # {
                    self.start, self.depth = i, 1
                    self.in_str, self.esc = False, False
                i += 1
                continue
            if self.in_str:
                if self.esc:
                    self.esc = False
                elif c == 0x5C:
                    self.esc = True
                elif c == 0x22:
                    self.in_str = False
            elif c == 0x22:
                self.in_str = True
            elif c == 0x7B:
                self.depth += 1
            elif c == 0x7D:
                self.depth -= 1
                if self.depth == 0:
                    try:
                        out.append(json.loads(buf[self.start:i + 1].decode("utf-8", "replace")))
                    except ValueError:
                        pass
                    self.done_offset = self.base + i + 1
                    self.start = -1
            i += 1
        self.pos = i
        # 只丢掉已解析完的部分，保留未完成的对象
        cut = self.start if self.start >= 0 else self.pos
        if cut > (4 << 20):
            self.buf = buf[cut:]
            self.base += cut
            self.pos = self.pos - cut if self.start < 0 else 0
            self.start = -1 if self.start < 0 else 0
            if self.start == 0:
                self.pos = len(self.buf)
        return out


def harvest(rec: dict, table: dict, source: str) -> bool:
    path = rec.get("path") or rec.get("image_path") or ""
    m = _PATH_ID.search(str(path))
    if not m:
        return False
    pid = int(m.group(1))
    arts: list[str] = []
    for v in rec.values():
        if isinstance(v, str) and "<artists>" in v:
            arts = artist_list(v)
            break
    entry = table.get(pid)
    if entry is None:
        table[pid] = (list(arts), [source])
    else:
        have, srcs = entry
        for a in arts:
            if a not in have:
                have.append(a)
        if source not in srcs:
            srcs.append(source)
    return bool(arts)


def main() -> int:
    tok = token()
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    table: dict[int, tuple[list[str], list[str]]] = {}

    if OUT.is_file():                                  # 合并 v1 的成果
        old = pq.read_table(OUT).to_pylist()
        for r in old:
            table[r["id"]] = (list(r["artists"]), list(r["sources"]))
        print(f"[merge] 旧表 {len(table)} 条", flush=True)

    stream = ObjStream(0)
    recs = withs = 0
    attempts = 0
    t0 = time.time()
    last_log = t0
    while attempts < 30:
        off = stream.done_offset
        try:
            with httpx.stream("GET", BASE + TARGET,
                              headers={**headers, "Range": f"bytes={off}-"} if off else headers,
                              timeout=120.0, follow_redirects=True) as resp:
                if off and resp.status_code != 206:
                    print(f"[warn] 服务器没按 Range 返回（{resp.status_code}），从 0 重来",
                          flush=True)
                    stream = ObjStream(0)
                    continue
                resp.raise_for_status()
                for chunk in resp.iter_bytes(1 << 20):
                    for rec in stream.feed(chunk):
                        recs += 1
                        if harvest(rec, table, "danbooru2024"):
                            withs += 1
                    now = time.time()
                    if now - last_log > 60:
                        last_log = now
                        gb = stream.done_offset / 2**30
                        print(f"  {gb:.1f}GB recs={recs} with_artist={withs} "
                              f"ids={len(table)} {int(now-t0)}s", flush=True)
            break                                       # 正常读完
        except Exception as exc:                        # noqa: BLE001
            attempts += 1
            print(f"[retry {attempts}] {type(exc).__name__} {exc} "
                  f"@ {stream.done_offset/2**30:.2f}GB", flush=True)
            time.sleep(min(30, 3 * attempts))

    print(f"[done] recs={recs} with_artist={withs} ids={len(table)}", flush=True)

    schema = pa.schema([
        pa.field("id", pa.int64()),
        pa.field("artists", pa.list_(pa.string())),
        pa.field("sources", pa.list_(pa.string())),
    ]).with_metadata({
        b"builder": b"tagger/work/build_artist_table2.py",
        b"built_at": time.strftime("%Y-%m-%dT%H:%M:%S").encode(),
        b"repo": REPO.encode(),
        b"excluded": b"e621",
    })
    rows = with_art = 0
    with pq.ParquetWriter(OUT, schema, compression="zstd") as writer:
        ids, arts, srcs = [], [], []
        for pid, (a, s) in table.items():
            ids.append(pid)
            arts.append(a)
            srcs.append(s)
            rows += 1
            with_art += 1 if a else 0
            if len(ids) >= 200_000:
                writer.write_table(pa.Table.from_arrays(
                    [pa.array(ids, pa.int64()), pa.array(arts, pa.list_(pa.string())),
                     pa.array(srcs, pa.list_(pa.string()))], schema=schema))
                ids, arts, srcs = [], [], []
        if ids:
            writer.write_table(pa.Table.from_arrays(
                [pa.array(ids, pa.int64()), pa.array(arts, pa.list_(pa.string())),
                 pa.array(srcs, pa.list_(pa.string()))], schema=schema))

    old = json.loads(STATS.read_text(encoding="utf-8")) if STATS.is_file() else {}
    old.update({
        "ids": rows,
        "ids_with_artist": with_art,
        "coverage": round(with_art / rows, 4) if rows else 0,
        "parquet_bytes": OUT.stat().st_size,
        "seconds": round(time.time() - t0) + int(old.get("seconds") or 0),
        "big_file": {"records": recs, "with_artist": withs,
                     "bytes": stream.done_offset, "retries": attempts},
    })
    STATS.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: old[k] for k in ("ids", "ids_with_artist", "coverage",
                                          "parquet_bytes", "seconds")},
                     ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
