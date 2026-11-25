"""逐条读取结果和索引，避免把总表载入内存。"""
import json


def text(value, part):
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        if isinstance(decoded, dict):
            value = decoded
        else:
            return value
    if isinstance(value, dict):
        return str(value.get('tag' if part == 'tag' else 'caption') or value.get(part) or '')
    return ''


def json_array(source):
    decoder=json.JSONDecoder();buffer='';eof=False
    def fill():
        nonlocal buffer,eof
        chunk=source.read(65536);buffer+=chunk;eof=not chunk
    def whitespace():
        nonlocal buffer
        while True:
            buffer=buffer.lstrip()
            if buffer or eof:return
            fill()
    whitespace()
    if not buffer.startswith('['):raise ValueError('JSON 文件应为结果记录数组；逐行结果请使用 JSONL')
    buffer=buffer[1:];first=True
    while True:
        whitespace()
        if buffer.startswith(']'):
            buffer=buffer[1:];whitespace()
            if buffer:raise ValueError('JSON 数组后还有额外内容')
            return
        if not first:
            if not buffer.startswith(','):raise ValueError('JSON 数组缺少逗号或结束符')
            buffer=buffer[1:];whitespace()
        while True:
            try:value,end=decoder.raw_decode(buffer);break
            except ValueError as exc:
                if eof:raise ValueError('JSON 数组不完整或格式不正确') from exc
                fill()
        if not isinstance(value,dict):raise ValueError('结果记录应为 JSON 对象')
        yield value;buffer=buffer[end:];first=False


def rows(path):
    if path.suffix.lower()=='.parquet':
        import pyarrow.parquet as pq
        for batch in pq.ParquetFile(path).iter_batches(batch_size=4096):yield from batch.to_pylist()
    else:
        with path.open(encoding='utf-8-sig') as source:
            if path.suffix.lower()=='.json':yield from json_array(source);return
            for number,line in enumerate(source,1):
                if line.strip():
                    try:yield json.loads(line)
                    except ValueError as exc:raise ValueError('结果第 '+str(number)+' 行 JSON 不正确') from exc
