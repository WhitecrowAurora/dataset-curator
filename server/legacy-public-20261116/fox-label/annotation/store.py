import json
import re

DEFAULTS = dict(enabled=False, directory='artist', revision='', artist_inject=True, artist_inject_tags=True)


def ensure_schema(c):
    c.execute('CREATE TABLE IF NOT EXISTS repo_artist (repo TEXT PRIMARY KEY,config_json TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS artist_backups (repo TEXT NOT NULL,pkg TEXT NOT NULL,source_sha256 TEXT NOT NULL,'
              'created_at REAL NOT NULL,rows_json TEXT NOT NULL,conflicts_json TEXT NOT NULL)')


def config(c, repo):
    row = c.execute('SELECT config_json FROM repo_artist WHERE repo=?', (repo,)).fetchone()
    return {**DEFAULTS, **json.loads(row[0])} if row else dict(DEFAULTS)


def validate(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('画师表配置格式不正确')
    result = {**DEFAULTS, **value}
    for key in ('enabled', 'artist_inject', 'artist_inject_tags'):
        if type(result[key]) is not bool:
            raise ValueError('画师注入开关必须为布尔值')
    directory = result['directory']
    if not isinstance(directory, str) or not re.fullmatch(r'[\w.-]+(?:/[\w.-]+)*', directory) or '..' in directory.split('/'):
        raise ValueError('画师表目录应为仓库内的相对目录，如 artist')
    revision = result['revision']
    if not isinstance(revision, str) or revision and not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('画师表版本必须为固定的 HF commit；留空在保存时获取当前版本')
    return result


def spec(repo, cfg):
    return f"hf://datasets/{repo}/{cfg['directory']}?revision={cfg['revision']}"


def options(c, repo, parts):
    cfg = config(c, repo)
    if not cfg['enabled']:
        return {}
    result = {'artist_table': spec(repo, cfg)}
    for part, field in (('nl', 'artist_inject'), ('tag', 'artist_inject_tags')):
        if part in parts:
            result[field] = cfg[field]
    return result
