"""已有标注的画师补注入：格式与打标侧共用，不调用模型。"""
import json
import re
from . import artist_templates as templates


class ArtistPrefixConflict(ValueError):
    def __init__(self, body):
        found = template_prefix(body)
        self.prefix = body[:found[0]].rstrip('，, ') if found else body[:160]
        super().__init__('旧画师前缀与仓库画师表不一致或格式不完整；请核对后使用强制重新注入')


def payload(raw):
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return raw


def id_of(image_id):
    text = str(image_id or '').strip()
    runs = re.findall(r'\d+', re.split(r'[\\/]', text)[-1]) or re.findall(r'\d+', text)
    return str(int(max(runs, key=len))) if runs else None


def strip_prefix(body, names):
    for language in ('en', 'zh'):
        prefix = templates.render(names, language)
        if prefix and (body == prefix or body.startswith(prefix + ', ') or body.startswith(prefix + '，')):
            return body[len(prefix):].lstrip('，, ')
    tag = templates.tag_of(names[0]) if names else ''
    return body[len(tag):].lstrip() if tag and body.startswith(tag + ' ') else body


def template_prefix(body):
    """只识别既有注入句式的完整画师段，不从普通正文猜作者。"""
    patterns = (
        r'^In the style of (?P<names>@[^@,，\n]+(?:, @[^@,，\n]+)*(?: and @[^@,，\n]+)?)(?:,\s*|，)',
        r'^使用 (?P<names>@[^@、，\n]+(?:、@[^@、，\n]+)*) 的风格(?:，|, |$)',
    )
    for pattern in patterns:
        match = re.match(pattern, body)
        if match:
            return match.end(), re.split(r', @| and @|、@', match['names'][1:])
    return None


def apply(raw, names, part, *, source, force=False, fit=None, review=False):
    value = payload(raw)
    if isinstance(value, dict) and value.get('ok') is False:
        return raw, False
    key = 'tag' if part == 'tag' else 'caption' if not isinstance(value, dict) or 'caption' in value else 'nl'
    text = value.get(key) if isinstance(value, dict) else value
    if not isinstance(text, str) or not text.strip() or not names:
        return raw, False
    names = list(dict.fromkeys(n.strip() for n in names if isinstance(n, str) and n.strip()))
    if not names:
        return raw, False
    result = dict(value) if isinstance(value, dict) else {key: text, 'ok': True}
    old = result.get('artists') or ([result['artist']] if result.get('artist') else [])
    old = [old] if isinstance(old, str) else list(old)
    lang = templates.route_lang(result.get('caption_language'))
    body = text.strip()
    if body.startswith('使用 @'):
        lang='zh'
    if part == 'tag':
        tags = [t.strip() for t in body.split(',') if t.strip()]
        artist_tag = templates.tag_of(names[0])
        old_tags = {templates.tag_of(n) for n in old}
        if not force and old_tags and artist_tag not in old_tags and any(t in old_tags for t in tags):
            return raw, False
        tags = [t for t in tags if t != artist_tag and (not force or t not in old_tags)]
        result['tags'] = [artist_tag, *tags]
        result[key] = ', '.join(result['tags'])
    else:
        prefix = templates.render(names, lang)
        clean = strip_prefix(body, names)
        budget = result.get('training_text') or {}
        fitted = not fit or (type(budget.get('tokens')) is int
                             and budget['tokens'] <= min(budget.get('limit',512),512))
        if (clean != body and not clean.startswith(('In the style of @', '使用 @'))
                and strip_prefix(clean,names) == clean
                and result.get('artist_injected') == 'template'
                and result.get('artist_table_source') == source
                and templates.pick(old) == templates.pick(names) and fitted):
            return raw, False
        while True:
            clean = strip_prefix(body, names)
            if clean != body:
                body = clean
                continue
            clean = strip_prefix(body, old)
            if clean != body:
                if not force:
                    if review:
                        raise ArtistPrefixConflict(body)
                    return raw, False
                body = clean
                continue
            found = template_prefix(body)
            if found and (force or all(templates.tag_of(n) in templates.pick(names) for n in found[1])):
                body = body[found[0]:].lstrip()
                continue
            if body.startswith(('In the style of @', '使用 @')):
                raise ArtistPrefixConflict(body)
            break
        result[key] = templates.insert(body, prefix, lang=lang)
        if fit:
            result[key] = fit(result[key], prefix, result)
        result['artist_injected'] = 'template'
        result['chars'] = len(result[key])
    result.update(artist=names[0], artist_tag=templates.tag_of(names[0]), artist_source='table',
                  artist_table_source=source)
    if len(names) > 1:
        result['artists'] = names
    else:
        result.pop('artists', None)
    if result == value:
        return raw, False
    return (json.dumps(result, ensure_ascii=False) if isinstance(raw, str) else result), True


def rows(values, table, annotation, *, force=False, fit=None, tags_by_id=None, issues=None, review=None):
    result, stats = [], dict(changed_tag=0, changed_nl=0, matched=0, missing=0)
    for value in values:
        row = dict(value)
        names = table['mapping'].get(id_of(row.get('image_id')), [])
        stats['matched' if names else 'missing'] += 1
        for part, field in (('tag', 'artist_inject_tags'), ('nl', 'artist_inject')):
            if annotation.get(field) and row.get(part):
                tag_value = payload(row.get('tag') or (tags_by_id or {}).get(row.get('image_id'), ''))
                tags = tag_value.get('tag', '') if isinstance(tag_value, dict) else tag_value
                fitting = (lambda text, prefix, data: fit(text, prefix, data, tags or '')) if fit else None
                try:
                    row[part], changed = apply(row[part], names, part, source=table['source'], force=force, fit=fitting,
                                               review=issues is not None if review is None else review)
                except ArtistPrefixConflict as exc:
                    if issues is None:
                        raise
                    issues.append(dict(image_id=row['image_id'], part=part, artists=names, existing_prefix=exc.prefix, reason=str(exc)))
                    changed = False
                stats['changed_' + part] += int(changed)
        result.append(row)
    return result, stats
