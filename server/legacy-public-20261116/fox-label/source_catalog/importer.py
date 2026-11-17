"""事务外构建清单；总表暂存于独立 SQLite，发布按分片进行。"""
import itertools
import json
import sqlite3
from pathlib import Path

from verification.history import ids_digest
from . import archive, network, readers


def package_entry(package, tree):
    entry = tree.get(package["id"])
    if not entry or not package['sha256'] or entry["size"] != package["size"] or (entry.get("lfs") or {}).get("oid") != package["sha256"]:
        raise ValueError("固定仓库版本与中心分片 SHA256／大小不一致：" + package["id"])
    return entry


def manifest(cfg, repo, kind, revision, tree, folder, token, progress, canceled):
    """导入独立暂存库；自动模式的总表无分片映射时返回 None。"""
    entry = tree.get(cfg["path"])
    if not entry:
        if cfg["source"] == "auto":
            return None
        raise ValueError("仓库未找到清单：" + cfg["path"])
    target = folder / Path(entry["path"]).name
    digest = network.download(repo, kind, revision, entry, target, token, progress, canceled)
    iterator = readers.rows(target, cfg["rows_key"])
    try:
        first = next(iterator, None)
        if first is None:
            raise ValueError("仓库总表为空")
        if not readers.package(first, cfg):
            if cfg["source"] == "auto":
                if hasattr(progress, 'manifest_mapping'):
                    progress.manifest_mapping(False)
                return None
            raise ValueError("总表缺少当前分片字段：" + cfg["package_field"])
        db = sqlite3.connect(folder / "manifest.sqlite3")
        try:
            db.execute("CREATE TABLE images(pkg TEXT NOT NULL,iid TEXT NOT NULL,PRIMARY KEY(pkg,iid)) WITHOUT ROWID")
            db.execute("BEGIN")
            buffer = []
            for row in itertools.chain([first], iterator):
                if canceled.is_set():
                    raise InterruptedError("清单任务已停止")
                pkg, iid = readers.package(row, cfg), readers.identity(row, cfg)
                if not pkg:
                    raise ValueError("总表存在缺少分片字段的图片")
                buffer.append((pkg, iid))
                if len(buffer) >= 4096:
                    db.executemany("INSERT INTO images VALUES(?,?)", buffer)
                    buffer.clear()
            db.executemany("INSERT INTO images VALUES(?,?)", buffer)
            db.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("仓库总表存在重复分片图片 ID") from exc
        finally:
            db.close()
        return folder / "manifest.sqlite3", digest
    finally:
        iterator.close()
        target.unlink(missing_ok=True)


def build(cfg, repo, kind, revision, tree, packages, requested, folder, token, progress, canceled):
    templated = '{stem}' in cfg['path'] or '{package}' in cfg['path']
    imported = manifest(cfg, repo, kind, revision, tree, folder, token, progress, canceled) if cfg["source"] in ("auto", "manifest") and not templated and not cfg.get('skip_manifest') else None
    db = sqlite3.connect(imported[0]) if imported else None
    try:
        for package in packages:
            if canceled.is_set():
                raise InterruptedError("清单任务已停止")
            name = package["id"]
            ids, origin, digest = None, "", ""
            if db:
                ids = [r[0] for r in db.execute("SELECT iid FROM images WHERE pkg=? ORDER BY iid", (name,))]
                if ids:
                    package_entry(package, tree)
                    origin, digest = "manifest", imported[1]
                elif cfg["source"] == "manifest":
                    raise ValueError("总表没有当前分片的图片：" + name)
            if not ids and cfg["source"] in ("auto", "index"):
                path = readers.index_path(cfg, name) if cfg["source"] == "index" or templated else None
                entry = tree.get(path) if path else None
                if entry:
                    package_entry(package, tree)
                    target = folder / Path(path).name
                    try:
                        digest = network.download(repo, kind, revision, entry, target, token, progress, canceled)
                        ids = [readers.identity(row, cfg) for row in readers.rows(target, cfg["rows_key"])]
                        origin = "index"
                    finally:
                        target.unlink(missing_ok=True)
                elif cfg["source"] == "index":
                    raise ValueError("仓库未找到分片索引：" + str(path))
            if not ids:
                if name not in requested:
                    continue
                entry = package_entry(package, tree)
                target = folder / Path(name).name
                try:
                    digest = network.download(repo, kind, revision, entry, target, token, progress, canceled)
                    ids = archive.scan(target)
                    origin = "archive"
                finally:
                    target.unlink(missing_ok=True)
            if not ids or len(set(ids)) != len(ids):
                raise ValueError("分片清单为空或图片 ID 重复：" + name)
            yield dict(package=package, ids=ids, ids_sha256=ids_digest(ids), origin=origin, manifest_sha256=digest)
    finally:
        if db:
            db.close()
