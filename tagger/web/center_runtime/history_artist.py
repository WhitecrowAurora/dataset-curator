"""补注入本地旧产物，先计算全部结果，再备份并原子替换每个文件。"""
import hashlib
import io
import json
import shutil
import time
from pathlib import Path

from ...artist_reinject import rows, apply, id_of, payload, ArtistPrefixConflict
from ...storage.atomic import write_bytes
from ...text_encoding.settings import TextEncoding
from ...text_encoding.tokenizer import TrainingText

class ReviewRequired(ValueError):
    def __init__(self, stats):
        self.report = {'artist_reinject': stats}
        super().__init__(f"已保留成功补注入结果；{stats['review_count']} 条画师前缀需核对，尚未回传。"
                         '核对后可使用强制重新注入，无需启动模型')



def fit(text, prefix, value, tags=''):
    settings = (value.get('training_text') or {}).get('settings')
    counter = TrainingText(TextEncoding(**settings) if settings else None)
    text = counter.fit(text,tags,required_prefix=prefix)
    value['training_text'] = counter.metadata(text,tags)
    return text


def rewrite(files, expected, table, annotation, *, force, cancel):
    files=list(files)
    known = set(expected)
    converted = []
    stats = dict(changed_tag=0,changed_nl=0)
    issues = []
    from ...verification.local import rows as local_rows
    tags_by_id = {r['image_id']:r['tag'] for r in local_rows(list(map(Path,files)),expected) if r.get('tag')}
    if annotation.get('artist_inject_tags'):
        tags_by_id = {iid:apply(tag,table['mapping'].get(id_of(iid),[]),'tag',source=table['source'],force=force)[0]
                      for iid,tag in tags_by_id.items()}
    def tags_of(iid):
        value=payload(tags_by_id.get(iid,''))
        return value.get('tag','') if isinstance(value,dict) else value or ''
    def convert(values):
        selected = [v for v in values if v.get('image_id') in known]
        new, report = rows(selected,table,annotation,force=force,fit=fit,tags_by_id=tags_by_id,issues=issues)
        for key in stats: stats[key] += report[key]
        iterator = iter(new)
        return [next(iterator) if v.get('image_id') in known else v for v in values]
    for path in map(Path,files):
        if cancel.is_set(): raise ValueError('画师补注入已取消')
        original = path.read_bytes()
        part = next((p for p in ('tag','nl') if path.stem.endswith('-'+p) and not path.name.startswith('artifacts-')),None)
        def convert_file(values):
            if not part: return convert(values)
            result=[]
            for v in values:
                bare = isinstance(v,dict) and (part not in v or
                       (part=='tag' and isinstance(v.get('tag'),str) and not isinstance(payload(v['tag']),dict)))
                if not bare:
                    result.extend(convert([v]));continue
                iid=v.get('image_id',path.stem[:-len('-'+part)])
                if iid not in known or not annotation.get('artist_inject_tags' if part=='tag' else 'artist_inject'):
                    result.append(v);continue
                names = table['mapping'].get(id_of(iid),[])
                try:
                    value,changed=apply(v,names,part,source=table['source'],force=force,review=True,
                                        fit=lambda text,prefix,data:fit(text,prefix,data,tags_of(iid)))
                except ArtistPrefixConflict as exc:
                    issues.append(dict(image_id=iid, part=part, artists=names, existing_prefix=exc.prefix, reason=str(exc)))
                    result.append(v);continue
                stats['changed_'+part]+=changed
                result.append(value)
            return result
        if path.suffix == '.parquet':
            import pyarrow as pa
            import pyarrow.parquet as pq
            source = pq.read_table(io.BytesIO(original))
            before = source.to_pylist(); after = convert(before)
            if before == after: continue
            buffer = io.BytesIO(); pq.write_table(pa.Table.from_pylist(after,schema=source.schema),buffer)
            encoded = buffer.getvalue()
        elif path.suffix == '.jsonl':
            before = [json.loads(line) for line in original.decode('utf-8').splitlines() if line.strip()]
            after = convert_file(before)
            if before == after: continue
            encoded = ''.join(json.dumps(v,ensure_ascii=False)+'\n' for v in after).encode('utf-8')
        else:
            before = json.loads(original)
            if isinstance(before,list): after=convert_file(before)
            elif isinstance(before,dict) and isinstance(before.get('rows'),list):
                after={**before,'rows':convert(before['rows'])}
                if 'resolutions' in after:
                    decisions=[]
                    for d in after['resolutions']:
                        converted_decision=convert([{'image_id':d['image_id'],d['part']:d.get('value','')}])[0]
                        decisions.append({**d,'value':converted_decision.get(d['part'],'')})
                    after['resolutions']=decisions
                if 'sha256' in after:
                    after['sha256']=hashlib.sha256(json.dumps(after['rows'],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            else: after=convert_file([before])[0]
            if before==after: continue
            encoded=json.dumps(after,ensure_ascii=False).encode('utf-8')
        converted.append((path,original,encoded))
    # 原始文件全部留存；中断后重试同一注入也不会叠加前缀。
    stamp=str(time.time_ns())
    for path,original,encoded in converted:
        if cancel.is_set(): raise ValueError('画师补注入已取消，已完成的修改保留')
        if path.read_bytes()!=original: raise ValueError('补注入期间产物发生变化，请停止打标后重试')
        backup=path.parent/'.delete'/'artist-reinject'/stamp/path.name
        backup.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,backup)
        write_bytes(path,encoded)
    review = list({(v['image_id'],v['part']):v for v in issues}.values())
    return {**stats, 'source': table['source'], 'review_count':len(review), 'review_samples':review[:100]}
