"""本地历史产物发现与明确来源关联；本机路径不发往中心。"""
import hashlib
import json
from pathlib import Path

from ...artifacts import shard_stem
from ...storage.atomic import read_json, write_json


class Inventory:
    def __init__(self, center):
        self.center = center
        self.path = center.dir / "history-inventory.json"

    def bindings(self):
        return (read_json(self.path) or {}).get("bindings", [])

    def bind(self, repo, package, source, out_dir):
        if self.center.runtime.active or self.center.history.running:
            raise ValueError("请先退出托管再关联本地目录")
        source, output = Path(source).resolve(), Path(out_dir).resolve()
        if not source.is_file() or not str(source).lower().endswith((".tar", ".tar.gz", ".tgz", ".zip")):
            raise ValueError("请选择原始图片分片文件（tar 或 zip）")
        if not output.is_dir() or not repo.strip() or not package.strip():
            raise ValueError("请选择产物目录并填写源仓库、源分片路径")
        identity = hashlib.sha256((repo.strip() + "\n" + package.strip()).encode()).hexdigest()
        row = {"id": identity, "repo": repo.strip(), "package": package.strip(),
               "source": str(source), "out_dir": str(output)}
        rows = [r for r in self.bindings() if r["id"] != identity] + [row]
        stem = shard_stem(row["package"])
        if any(r["id"] != identity and Path(r["out_dir"]).resolve() == output
               and shard_stem(r["package"]) == stem for r in rows):
            raise ValueError("同一目录已有同名源分片关联，请分别使用各自的产物目录")
        write_json(self.path, {**(read_json(self.path) or {}), "bindings": rows})
        return self.scan()

    def remove(self, key):
        if self.center.runtime.active or self.center.history.running:
            raise ValueError("请先退出托管再修改关联")
        write_json(self.path, {**(read_json(self.path) or {}), "bindings": [r for r in self.bindings() if r["id"] != key]})

    def local(self, key):
        row = next((r for r in self.bindings() if r["id"] == key), None)
        if not row:
            raise ValueError("没有这个本地历史分片关联")
        return row

    def scan(self):
        rows = []
        for row in self.bindings():
            output = Path(row["out_dir"])
            from ...verification.local import paths
            files = paths(output, row['package'])
            ledgers = [p for name in ('failures.jsonl', 'skipped.jsonl', 'excused.jsonl')
                       if (p := output / name).is_file()]
            source = Path(row["source"])
            status = "ready" if source.is_file() and files else "missing_local_files"
            fingerprint = hashlib.sha256(json.dumps(
                [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in [source, *files, *ledgers] if p.is_file()],
                ensure_ascii=False).encode()).hexdigest()
            rows.append({**row, "status": status, "files": [str(p) for p in files], "fingerprint": fingerprint})
        return rows

    def public(self):
        return [{k: r[k] for k in ("id", "repo", "package", "status", "fingerprint")}
                for r in self.scan()]

    def unlinked(self, rows=None):
        from ...center_runtime.snapshot import superseded
        from ...verification.local import is_delivery
        data = read_json(self.path) or {}
        roots = {Path(self.center.config()["out_dir"]), *(Path(p) for p in data.get('search_dirs', []))}
        linked = {str(Path(f).resolve()) for r in (rows if rows is not None else self.scan()) for f in r["files"]}
        return [{'name': p.name, 'path': str(p), 'reason': '缺少本地原分片或来源关联'}
                for p in sorted({p.resolve() for root in roots for pattern in ('artifacts-*.jsonl','artifacts-*.json','artifacts-*.parquet')
                                 for p in root.rglob(pattern)}) if not is_delivery(p) and not superseded(p) and str(p) not in linked] + data.get('discovery_issues', [])
