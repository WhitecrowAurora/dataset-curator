"""冲突与决策：同一个 image_id 被打了两遍、内容还不一样时，留下来裁决。

**什么算冲突**
`merge` 早就会把它们挑出来（`MergeStats.duplicates`），但只记了"哪两个文件"，
没留下两份内容——而人要做决定，靠的恰恰是内容本身。这里补的就是这一步：
扫描时把**每一份候选**的正文、来源、时间、模型都摊开存下来，页面才有的比。

只收"内容不同"的组合。内容相同的是幂等重复（重跑没清目录），取一份就行，
不需要人来点按钮——把它们塞进待办只会让人以为真的出了问题。

**决策存哪、按什么匹配**
决策落在独立文件里（默认 `work/conflicts/decisions.json`），键是 `image_id|kind`，
值是**选中那份内容的哈希**而不是下标。理由：重扫一次候选顺序就可能变（多了一个
文件、路径排序变了），按下标存会把决定悄悄套到另一份内容上；按哈希匹配则要么命中
要么明确失效。

**策略**
`manual` 不替人做决定（默认）；`newest` 保留 `ts` 最新；`longest` 保留正文最长
（tag 按 tag 个数）。三种都只**建议**，落到 decisions 文件里，合并时才生效。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .storage.collections import SpillList, SpillMap
from .storage.conflicts import ConflictItems, Decisions

CONFLICT_DIR = "work/conflicts"
INDEX_NAME = "conflicts.json"
DECISIONS_NAME = "decisions.json"


def _digest(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


@dataclass
class Candidate:
    """一份候选内容。页面要展示的都在这里。"""

    value: str
    source: str
    ts: float = 0.0
    model: str = ""
    chars: int = 0

    @property
    def digest(self) -> str:
        return _digest(self.value)

    def as_dict(self) -> dict:
        return {"value": self.value, "source": self.source, "ts": self.ts,
                "model": self.model, "chars": self.chars,
                "digest": self.digest}


@dataclass
class Conflict:
    image_id: str
    kind: str                      # tag | nl
    candidates: list[Candidate] = field(default_factory=list)
    decided: str = ""              # 命中的 digest；空 = 还没人定

    @property
    def key(self) -> str:
        return f"{self.image_id}|{self.kind}"

    def as_dict(self) -> dict:
        return {"image_id": self.image_id, "kind": self.kind,
                "key": self.key, "decided": self.decided,
                "candidates": [c.as_dict() for c in self.candidates]}


# ------------------------------------------------------------------ 读产物


def _iter_payloads(roots: Iterable[str | Path], *,
                   stats: ScanResult) -> Iterator[tuple[str, str, str, float, str, str]]:
    """遍历产物，产出 (image_id, kind, 正文, ts, model, 来源)。

    取值口径与 `merge` 一致：tag 取 payload 的 `tag`，nl 取 `caption`。
    两种分片（parquet / jsonl）与逐图 json 都认——冲突恰恰最常出现在
    "新旧两代产物放同一目录"的时候，只认一种等于看不见。

    分片里的 tag/nl 是 JSON 字符串（见 artifacts.py），逐图 json 是整个
    payload 文件；两边都在这里归一成"正文 + 元信息"。
    """
    from .artifacts import find_artifacts, iter_artifact_rows, parse_payload
    from .merge import _iter_json_files, _read_one, _tuple_from_payload

    for root in roots:
        base = Path(root)
        if not base.exists():
            stats.unreadable += 1
            continue
        for path in _iter_json_files(base):
            try:
                image_id, kind, value, _image_path, prov = _read_one(path)
            except ValueError:
                stats.unreadable += 1
                continue
            if not image_id or not value:
                continue
            ts, model = _meta_of(path)
            yield image_id, kind, value, ts, model, str(path)

        for path in find_artifacts(base):
            try:
                for idx, row in enumerate(iter_artifact_rows(path)):
                    fallback = str(row.get("image_id") or "")
                    for kind in ("tag", "nl"):
                        obj = parse_payload(kind, str(row.get(kind) or ""))
                        if obj is None:
                            continue
                        image_id, _, value, _, _ = _tuple_from_payload(obj, fallback, kind)
                        if not image_id or not value:
                            continue
                        yield (image_id, kind, value, float(obj.get("ts") or 0.0),
                               _model_of(obj), f"{path}#{idx}")
            except (ValueError, OSError):
                # 坏片不该让整次扫描失败：别的文件里还有得比。
                stats.unreadable += 1
                continue


def _meta_of(path: Path) -> tuple[float, str]:
    try:
        st = path.stat()
        return st.st_mtime, ""
    except OSError:
        return 0.0, ""


def _model_of(payload: dict) -> str:
    prov = payload.get("provenance")
    if isinstance(prov, dict):
        for key in ("model_id", "model", "engine"):
            if prov.get(key):
                return str(prov[key])
    for key in ("tag_backend", "model_id", "model"):
        if payload.get(key):
            return str(payload[key])
    return ""


# ------------------------------------------------------------------ 扫描


@dataclass
class ScanResult:
    items: list[Conflict] = field(default_factory=list)
    scanned: int = 0            # 参与比较的记录条数
    idempotent: int = 0         # 内容相同、取一份即可的重复
    unreadable: int = 0
    roots: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    def apply_decisions(self, picks: dict[str, str]) -> None:
        if isinstance(self.items, ConflictItems):
            self.items.picks = picks
            return
        for index, item in enumerate(self.items):
            pick = picks.get(item.key, "")
            item.decided = pick if any(c.digest == pick for c in item.candidates) else ""
            if isinstance(self.items, SpillList):
                self.items.data[index] = item

    @property
    def decided_count(self) -> int:
        if isinstance(self.items, ConflictItems):
            return self.items.decided_count()
        return sum(bool(item.decided) for item in self.items)

    def as_dict(self, *, offset: int = 0, limit: int = 200) -> dict:
        page = self.items[offset:offset + limit] if limit else self.items[offset:]
        return {
            "scanned": self.scanned, "idempotent": self.idempotent,
            "unreadable": self.unreadable, "roots": self.roots,
            "elapsed_s": round(self.elapsed_s, 3),
            "total": len(self.items), "offset": offset, "limit": limit,
            "items": [c.as_dict() for c in page],
        }

    def export(self, path: Path) -> None:
        import os
        import tempfile
        from .storage.atomic import sync_directory

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                            dir=path.parent, suffix=".tmp", delete=False) as fh:
                tmp = Path(fh.name)
                meta = self.as_dict(limit=1)
                meta.pop("items")
                meta["limit"] = 0
                fh.write(json.dumps(meta, ensure_ascii=False)[:-1] + ', "items": [\n')
                for index, item in enumerate(self.items):
                    if index:
                        fh.write(",\n")
                    json.dump(item.as_dict(), fh, ensure_ascii=False)
                fh.write("\n]}\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            sync_directory(path.parent)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)


def scan(roots: Iterable[str | Path], *,
         decisions: dict[str, str] | None = None,
         on_progress: Callable[[int], None] | None = None) -> ScanResult:
    """扫一遍产物，收集所有"同 id 不同内容"的组合。

    `decisions` 传进来就把已定的标在条目上（页面据此显示"已批准"）。
    每个 ID 的第一份内容用于比较，只有不同内容的组合进入返回的冲突表。
    """
    started = time.time()
    root_list = [str(r) for r in roots]
    first = SpillMap(encode=asdict, decode=lambda v: Candidate(**v))
    conflicts = SpillMap(encode=lambda values: [asdict(v) for v in values],
                         decode=lambda values: [Candidate(**v) for v in values])
    result = ScanResult(roots=root_list)
    result.items = SpillList(encode=asdict, decode=lambda v: Conflict(
        v["image_id"], v["kind"], [Candidate(**c) for c in v["candidates"]], v["decided"]))

    for image_id, kind, value, ts, model, source in _iter_payloads(root_list, stats=result):
        result.scanned += 1
        if on_progress is not None and result.scanned % 500 == 0:
            on_progress(result.scanned)
        key = (image_id, kind)
        cand = Candidate(value=value, source=source, ts=ts, model=model,
                         chars=len(value))
        bucket = conflicts.get(key)
        if bucket is None:
            prev = first.get(key)
            if prev is None:
                first[key] = cand
                continue
            # 第一次撞上：把先前那份也搬进候选表，从此这条进入"待裁决"。
            if prev.value == value:
                result.idempotent += 1
                continue
            bucket = [prev]
        else:
            if any(c.value == value for c in bucket):
                result.idempotent += 1
                continue
        bucket.append(cand)
        conflicts[key] = bucket

    for (image_id, kind), cands in conflicts.items():
        result.items.append(Conflict(image_id=image_id, kind=kind,
                                     candidates=cands))
    result.apply_decisions(decisions or {})
    result.items.sort(key=lambda c: (c.image_id, c.kind))
    first.close()
    conflicts.close()
    result.elapsed_s = time.time() - started
    return result


# ------------------------------------------------------------------ 决策


def strategy_pick(cands: list[Candidate], strategy: str) -> str:
    """按策略挑一份，返回它的 digest。挑不出来返回空串（交给人）。"""
    if not cands:
        return ""
    if strategy == "newest":
        return max(cands, key=lambda c: (c.ts, c.chars)).digest
    if strategy == "longest":
        return max(cands, key=lambda c: (c.chars, c.ts)).digest
    return ""


def dir_of(state_dir: str | Path | None = None) -> Path:
    return Path(state_dir or CONFLICT_DIR)


def save_index(result: ScanResult, state_dir: str | Path | None = None) -> Path:
    import sqlite3
    import uuid
    from .storage.atomic import write_json

    state = dir_of(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    # 不覆盖读者正在打开的索引；先提交新快照，再切换小指针文件。
    out = state / f"index-{uuid.uuid4().hex}.sqlite3"
    db = sqlite3.connect(out)
    try:
        db.executescript("""CREATE TABLE items(position INTEGER PRIMARY KEY,key TEXT UNIQUE,value TEXT);
            CREATE TABLE candidates(position INTEGER,digest TEXT,PRIMARY KEY(position,digest));
            CREATE TABLE metadata(value TEXT);""")
        for position, item in enumerate(result.items):
            db.execute("INSERT INTO items VALUES(?,?,?)", (position, item.key, json.dumps(asdict(item), ensure_ascii=False)))
            db.executemany("INSERT INTO candidates VALUES(?,?)", ((position, c.digest) for c in item.candidates))
        meta = {k: getattr(result, k) for k in ("scanned", "idempotent", "unreadable", "roots", "elapsed_s")}
        db.execute("INSERT INTO metadata VALUES(?)", (json.dumps(meta, ensure_ascii=False),))
        db.commit()
    finally:
        db.close()
    with out.open("r+b") as fh:
        import os
        os.fsync(fh.fileno())
    write_json(state / "index.json", {"file": out.name}, backup=True)
    result.items = ConflictItems(out, picks_for_merge(state))
    return out


def load_index(state_dir: str | Path | None = None) -> ScanResult | None:
    import sqlite3
    from .storage.atomic import read_json

    state = dir_of(state_dir)
    for pointer_path in (state / "index.json", state / "index.json.bak"):
        pointer = read_json(pointer_path)
        if not isinstance(pointer, dict) or not pointer.get("file"):
            continue
        path = state / Path(pointer["file"]).name
        if not path.is_file():
            continue
        try:
            db = sqlite3.connect(path)
            try:
                meta = json.loads(db.execute("SELECT value FROM metadata").fetchone()[0])
            finally:
                db.close()
        except (sqlite3.DatabaseError, ValueError, TypeError):
            continue
        return ScanResult(items=ConflictItems(path, picks_for_merge(state)), **meta)
    src = dir_of(state_dir) / INDEX_NAME
    if not src.is_file():
        return None
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    res = ScanResult(scanned=int(data.get("scanned") or 0),
                     idempotent=int(data.get("idempotent") or 0),
                     unreadable=int(data.get("unreadable") or 0),
                     roots=list(data.get("roots") or []),
                     elapsed_s=float(data.get("elapsed_s") or 0.0))
    for item in data.get("items") or []:
        # digest 是算出来的属性，不在 __init__ 参数里，落盘时会写进 json。
        # 这里把它剔掉再还原，否则重新读回来直接 TypeError。
        cands = [{k: c[k] for k in ("value", "source", "ts", "model", "chars")
                  if k in c}
                 for c in (item.get("candidates") or [])]
        res.items.append(Conflict(
            image_id=str(item.get("image_id") or ""),
            kind=str(item.get("kind") or ""),
            candidates=[Candidate(**c) for c in cands]))
    res.apply_decisions(picks_for_merge(state_dir))
    return res


def load_decisions(state_dir: str | Path | None = None):
    return Decisions(dir_of(state_dir))


def save_decisions(decisions: dict[str, dict],
                   state_dir: str | Path | None = None) -> Path:
    store = load_decisions(state_dir)
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM decisions")
        db.executemany("INSERT INTO decisions VALUES(?,?,?,?)", (
            (k, v.get("pick", ""), v.get("strategy", "manual"), v.get("at", 0))
            for k, v in decisions.items()))
    return store.path


def decide(image_id: str, kind: str, pick: str, *, strategy: str = "manual",
           state_dir: str | Path | None = None) -> dict:
    """批准某一条：把 (id,kind) → 选中内容的哈希 记下来。"""
    decisions = load_decisions(state_dir)
    with decisions.connection() as db:
        key = f"{image_id}|{kind}"
        if not pick:
            db.execute("DELETE FROM decisions WHERE key=?", (key,))
        else:
            db.execute("INSERT OR REPLACE INTO decisions VALUES(?,?,?,?)", (key, pick, strategy, time.time()))
    return decisions.get(key, {})


def auto_decide(result: ScanResult, strategy: str, *,
                state_dir: str | Path | None = None,
                only_undecided: bool = True,
                overwrite: bool = False) -> dict:
    """按策略批量批准。返回 {"applied": n, "skipped": n}。

    `manual` 不做事——它是"什么都不自动做"的代名词，不是一种策略。
    默认不动**人工批过**的条目：一键刷掉人拍过的板，是这类功能最容易招骂的地方。
    """
    if strategy not in ("newest", "longest"):
        return {"applied": 0, "skipped": 0, "reason": "manual 策略不自动决策"}
    decisions = load_decisions(state_dir)
    with decisions.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if overwrite:
            only_undecided = False
        applied = skipped = 0
        for item in result.items:
            existing = db.execute("SELECT pick FROM decisions WHERE key=?", (item.key,)).fetchone()
            valid = existing and any(c.digest == existing[0] for c in item.candidates)
            if only_undecided and valid:
                skipped += 1
                continue
            pick = strategy_pick(item.candidates, strategy)
            if not pick:
                skipped += 1
                continue
            db.execute("INSERT OR REPLACE INTO decisions VALUES(?,?,?,?)", (item.key, pick, strategy, time.time()))
            item.decided = pick
            applied += 1
    result.apply_decisions(picks_for_merge(state_dir))
    return {"applied": applied, "skipped": skipped}


def picks_for_merge(state_dir: str | Path | None = None) -> dict[str, str]:
    """交给 merge 的 {id|kind: digest}。没定过的一条都不带。"""
    return Decisions(dir_of(state_dir), picks=True)
