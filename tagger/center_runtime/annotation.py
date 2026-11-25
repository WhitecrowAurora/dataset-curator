"""派单标注选项与固定画师表到既有画师规则的适配。"""
from __future__ import annotations

import json

ENV_KEY = "FOXLABEL_ANNOTATION"
FIELDS = {"artist_inject": "inject_caption", "artist_inject_tags": "inject_tags"}


def options(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - (set(FIELDS) | {'artist_table'}):
        raise ValueError("派单标注选项只支持 NL / TAG 画师注入")
    if any(type(v) is not bool for k, v in value.items() if k in FIELDS):
        raise ValueError("画师注入选项必须为布尔值")
    if 'artist_table' in value:
        import re
        if not isinstance(value['artist_table'], str) or not re.fullmatch(r'hf://datasets/[\w.-]+/[\w.-]+/[\w./-]+\?revision=[0-9a-f]{40}', value['artist_table']):
            raise ValueError('中心画师表必须是固定版本的 HF 数据集目录')
    return dict(value)


def environment(task):
    # 每个分包覆盖一次，空对象也覆盖，避免上一分包的注入选择串入下一包。
    return {ENV_KEY: json.dumps(options(task.get("annotation", {}))),
            'FOXLABEL_ARTIST_TABLE': task.get('artist_local_table', '')}


def apply(ops, raw):
    from dataclasses import replace

    selected = options(json.loads(raw)) if raw else {}
    if not selected:
        return ops
    artist = {**ops.artist, **{FIELDS[key]: value for key, value in selected.items() if key in FIELDS}}
    if 'artist_table' in selected:
        artist['table'] = selected['artist_table']
        artist['skip_when_missing'] = False
        artist['nl_style'] = 'template'
        import os
        if os.environ.get('FOXLABEL_ARTIST_TABLE'):
            artist['table'] = os.environ['FOXLABEL_ARTIST_TABLE']
    if any(selected.get(k) for k in FIELDS):
        artist["enabled"] = True
    return replace(ops, artist=artist)
