"""独立可撤销密钥；不复用网页口令或上游管理员密钥。"""
import hashlib
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class AccessStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS access_keys (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, owner TEXT NOT NULL,
              digest TEXT NOT NULL UNIQUE, role TEXT NOT NULL,
              created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL);
            CREATE TABLE IF NOT EXISTS tool_audit (
              id INTEGER PRIMARY KEY, key_id TEXT NOT NULL, tool TEXT NOT NULL,
              request_id TEXT, status TEXT NOT NULL, created_at REAL NOT NULL);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def create(self, name, role, days, owner):
        if (not isinstance(name, str) or not name.strip() or len(name) > 80
                or any(ord(ch) < 32 for ch in name)):
            raise ValueError('请填写1～80字的密钥名称')
        if role not in ('read', 'operate') or type(days) is not int or not 1 <= days <= 365:
            raise ValueError('权限或有效天数不正确（1～365天）')
        key = 'lxm_' + secrets.token_urlsafe(36)
        now = time.time()
        row = dict(id=secrets.token_hex(8), name=name.strip(), role=role,
                   created_at=now, expires_at=now + days * 86400)
        with self.connection() as c:
            c.execute('INSERT INTO access_keys VALUES(?,?,?,?,?,?,?,NULL)',
                      (row['id'], row['name'], owner, hashlib.sha256(key.encode()).hexdigest(), role,
                       now, row['expires_at']))
        return dict(ok=True, key=key, item=row)

    def authenticate(self, key):
        if not isinstance(key, str) or not re.fullmatch(r'lxm_[A-Za-z0-9_-]{48}', key):
            return None
        with self.connection() as c:
            row = c.execute('SELECT id,name,owner,role FROM access_keys WHERE digest=?'
                            ' AND revoked_at IS NULL AND expires_at>?',
                            (hashlib.sha256(key.encode()).hexdigest(), time.time())).fetchone()
        return dict(row) if row else None

    def view(self):
        with self.connection() as c:
            keys = [dict(r) for r in c.execute('SELECT id,name,owner,role,created_at,expires_at,revoked_at'
                                              ' FROM access_keys ORDER BY created_at DESC LIMIT 100')]
            audit = [dict(r) for r in c.execute('SELECT a.*,k.name FROM tool_audit a'
                                               ' JOIN access_keys k ON k.id=a.key_id ORDER BY a.id DESC LIMIT 100')]
        return dict(ok=True, keys=keys, audit=audit)

    def revoke(self, key_id):
        with self.connection() as c:
            if not c.execute('SELECT 1 FROM access_keys WHERE id=?', (key_id,)).fetchone():
                raise ValueError('密钥不存在')
            c.execute('UPDATE access_keys SET revoked_at=COALESCE(revoked_at,?) WHERE id=?', (time.time(), key_id))
        return dict(ok=True)

    def audit(self, key_id, tool, args, success):
        # Only record operation identity and outcome, never returned credentials,
        # prompt text or user-supplied secrets.
        with self.connection() as c:
            c.execute('INSERT INTO tool_audit(key_id,tool,request_id,status,created_at) VALUES(?,?,?,?,?)',
                      (key_id, tool, str(args.get('request_id') or '')[:128], 'ok' if success else 'failed', time.time()))
