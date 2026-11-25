"""按分片暂存画师表并流式写工单，避免整批映射占住调度事务。"""
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


@contextmanager
def artist_tables(store, items):
    from annotation.tables import prepare
    with TemporaryDirectory(prefix='offline-document-', dir=store.dir) as folder:
        tables = {}
        for index, (repo, pkg) in enumerate(items):
            table = prepare(store, repo, pkg)
            if table:
                path = Path(folder) / str(index)
                with path.open('w', encoding='utf-8') as output:
                    json.dump(table, output, ensure_ascii=False, separators=(',', ':'))
                tables[repo, pkg] = {'source': table['source'], 'path': path}
        yield tables


def write(path, data, tables):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        output.write(json.dumps({k: v for k, v in data.items() if k != 'tasks'},
                                ensure_ascii=False, separators=(',', ':'))[:-1])
        output.write(',"tasks":[')
        for index, task in enumerate(data['tasks']):
            if index:output.write(',')
            encoded = json.dumps(task, ensure_ascii=False, separators=(',', ':'))
            table = tables.get((task['repo'], task['package']))
            if task.get('annotation', {}).get('artist_table'):
                output.write(encoded[:-1] + ',"artist_data":')
                with table['path'].open(encoding='utf-8') as source:
                    shutil.copyfileobj(source, output, 65536)
                output.write('}')
            else:
                output.write(encoded)
        output.write(']}')
        output.flush();os.fsync(output.fileno())


def read(path):
    def compact(value):
        if 'mapping' in value and isinstance(value['mapping'], dict):
            pool = {}
            for key, names in value['mapping'].items():
                value['mapping'][key] = pool.setdefault(tuple(names), names)
        return value
    with path.open(encoding='utf-8') as source:
        return json.load(source, object_hook=compact)
