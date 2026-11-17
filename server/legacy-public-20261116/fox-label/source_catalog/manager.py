"""串行后台导入，不占调度写锁下载原片。"""
import json
import logging
import shutil
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from . import importer, network, retry, store as catalogs


class Manager:
    def __init__(self, store, dispatch, token_reader):
        self.store, self.dispatch, self.token_reader = store, dispatch, token_reader
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="source-catalog")
        stale = Path(store.dir) / 'source-work'
        if stale.is_dir():
            for child in stale.glob('catalog-*'):
                if child.is_dir() and child.resolve().parent == stale.resolve():
                    shutil.rmtree(child)
        with self.transaction() as c:
            c.execute("UPDATE source_jobs SET state='queued' WHERE state='running'")
            c.execute("DELETE FROM source_requests WHERE NOT EXISTS (SELECT 1 FROM packages p WHERE"
                      " p.repo=source_requests.repo AND p.id=source_requests.pkg AND p.sha256=source_requests.source_sha256)")
            # 只准备现有租约涉及的片，不扫描未派工的整个仓库。
            for p in c.execute("SELECT DISTINCT p.* FROM packages p JOIN leases l ON l.repo=p.repo AND l.pkg=p.id"
                               " WHERE l.status='open'").fetchall():
                catalogs.require(c, p['repo'], p['id'], p['sha256'], p['size'])
        self.thread.start()

    def transaction(self):
        from contextlib import contextmanager
        @contextmanager
        def tx():
            with self.store._write_lock:
                c = self.store.conn()
                try:
                    with self.dispatch._Tx(c):
                        yield c
                finally:
                    c.close()
        return tx()

    def run(self):
        while not self.stop.is_set():
            try:
                with self.transaction() as c:
                    row = c.execute("SELECT * FROM source_jobs WHERE state='queued' ORDER BY updated_at LIMIT 1").fetchone()
                    job = dict(row) if row else None
                    if job:
                        c.execute("UPDATE source_jobs SET state='running',downloaded=0,total=0,error='' WHERE repo=?", (job["repo"],))
            except Exception:
                logging.exception('读取清单任务失败')
                self.stop.wait(5)
                continue
            if not job:
                self.wake.wait(5)
                self.wake.clear()
                continue
            try:
                self.process(job)
            except InterruptedError:
                return
            except Exception as exc:
                # 网络异常可能带签名 URL，面板仅展示固定错误类别。
                error = retry.describe(exc)
                try:
                    with self.transaction() as c:
                        c.execute("UPDATE source_jobs SET state='failed',error=?,updated_at=? WHERE repo=? AND generation=?",
                                  (error[:500], time.time(), job["repo"], job["generation"]))
                except Exception:
                    logging.exception('保存清单失败状态失败')
                logging.warning("source catalog %s: %s", job["repo"], error)

    def process(self, job):
        repo = job["repo"]
        with self.transaction() as c:
            cfg = catalogs.config(c, repo)
            kind = c.execute("SELECT repo_type FROM repos WHERE repo=?", (repo,)).fetchone()[0]
            packages = [dict(r) for r in c.execute("SELECT * FROM packages WHERE repo=? ORDER BY id", (repo,))]
            requested = {r["pkg"] for r in c.execute("SELECT * FROM source_requests WHERE repo=?", (repo,))}
            cached = {r['pkg'] for r in c.execute("SELECT s.pkg FROM source_lists s JOIN packages p ON p.repo=s.repo AND p.id=s.pkg"
                                                 " AND p.sha256=s.source_sha256 AND p.size=s.source_bytes WHERE s.repo=? AND s.generation=?",
                                                 (repo, cfg['generation']))}
            packages = [p for p in packages if p['id'] not in cached]
            for name in cached:
                c.execute("DELETE FROM source_requests WHERE repo=? AND pkg=? AND source_sha256 IN"
                          " (SELECT source_sha256 FROM source_lists WHERE repo=? AND pkg=? AND generation=?)",
                          (repo, name, repo, name, cfg['generation']))
        if not cfg["enabled"] or cfg["generation"] != job["generation"]:
            return
        if not packages:
            with self.transaction() as c:
                c.execute("UPDATE source_jobs SET state='ready' WHERE repo=? AND generation=?", (repo, job['generation']))
                for package in c.execute('SELECT * FROM packages WHERE repo=?', (repo,)).fetchall():
                    from .revalidation import refresh
                    refresh(c, self.dispatch, repo, dict(package))
            return
        token = self.token_reader(self.store)
        def retry_notice(message):
            with self.transaction() as c:
                c.execute("UPDATE source_jobs SET error=?,updated_at=? WHERE repo=? AND generation=?",
                          (message, time.time(), repo, job['generation']))
        revision, tree = network.metadata(repo, kind, cfg["revision"], token, self.stop, retry_notice)
        with self.transaction() as c:
            c.execute("UPDATE source_jobs SET revision=?,error='' WHERE repo=? AND generation=?", (revision, repo, job["generation"]))
            cached_manifest = c.execute('SELECT mapping FROM source_manifests WHERE repo=? AND generation=? AND revision=?',
                                        (repo, job['generation'], revision)).fetchone()
            cfg['skip_manifest'] = bool(cached_manifest and not cached_manifest['mapping'] and cfg['source'] == 'auto')
        # 只保留长期清单；临时文件属于此任务，失败和正常退出都清理。
        parent = Path(self.store.dir) / "source-work"
        parent.mkdir(exist_ok=True)
        last = [0.0]
        def progress(size, total):
            if time.monotonic() - last[0] < 2 and size != total:
                return
            last[0] = time.monotonic()
            with self.transaction() as c:
                current = catalogs.config(c, repo)
                if current['generation'] != job['generation'] or not current['enabled']:
                    raise ValueError('清单配置已变化')
                c.execute("UPDATE source_jobs SET downloaded=?,total=?,updated_at=?,error='' WHERE repo=? AND generation=?",
                          (size, total, time.time(), repo, job["generation"]))
        with TemporaryDirectory(prefix="catalog-", dir=parent) as folder:
            def progress_package(name):
                with self.transaction() as c:
                    c.execute("UPDATE source_jobs SET package=?,downloaded=0,total=0 WHERE repo=? AND generation=?", (name, repo, job['generation']))
            progress.package = progress_package
            progress.retry = retry_notice
            def mapping(value):
                with self.transaction() as c:
                    c.execute('INSERT OR REPLACE INTO source_manifests VALUES(?,?,?,?)', (repo, job['generation'], revision, int(value)))
            progress.manifest_mapping = mapping
            for result in importer.build(cfg, repo, kind, revision, tree, packages, requested, Path(folder), token, progress, self.stop):
                package = result["package"]
                with self.transaction() as c:
                    current = catalogs.config(c, repo)
                    source = c.execute("SELECT sha256,size FROM packages WHERE repo=? AND id=?", (repo, package["id"])).fetchone()
                    if current["generation"] != job["generation"] or not current["enabled"]:
                        return
                    if not source or source["sha256"] != package["sha256"] or source["size"] != package["size"]:
                        continue
                    c.execute("INSERT OR REPLACE INTO source_lists VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                              (repo, package["id"], package["sha256"], package["size"], job["generation"], revision,
                               result["origin"], result["manifest_sha256"], result["ids_sha256"], json.dumps(result["ids"]), time.time()))
                    c.execute("DELETE FROM source_requests WHERE repo=? AND pkg=? AND source_sha256=?", (repo, package["id"], package["sha256"]))
                    c.execute("UPDATE source_jobs SET package=? WHERE repo=? AND generation=?", (package['id'], repo, job['generation']))
                    from .revalidation import refresh
                    refresh(c, self.dispatch, repo, package)
                    c.execute("UPDATE source_jobs SET updated_at=? WHERE repo=? AND generation=?", (time.time(), repo, job['generation']))
        with self.transaction() as c:
            c.execute("UPDATE source_jobs SET state='ready',error='',updated_at=? WHERE repo=? AND generation=?",
                      (time.time(), repo, job["generation"]))
            if c.execute("SELECT 1 FROM source_requests WHERE repo=?", (repo,)).fetchone():
                catalogs.enqueue(c, repo)

    def close(self):
        self.stop.set()
        self.wake.set()
        self.thread.join(timeout=5)
