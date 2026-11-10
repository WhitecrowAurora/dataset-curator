"""新建任务页的「配置方案」。

与 `ui_form` 的分工
------------------
`ui_form` 是**唯一那一份**"上次填的"，自动写、没有名字、也不可选。
这里存的是**起了名字的快照**，用户显式点"保存"才有，用来在几种口径之间来回切
（比如"只打 tag / 中文 caption 30% / 黄金集长描述"）。

两份刻意分开：混在一起的话，一次误切换就会把"上次填的"覆盖掉，而那份是
崩溃重启后唯一能救回来的东西。

自动保存
--------
打开就每隔一会儿把当前表单写进一个名为「自动保存 + 时间戳」的方案里。命名规则
是用户定的：开启那一刻取时间戳，之后**原地覆盖**（不再改名），否则下拉列表会被
几百条同义项刷爆。每次重新开启会新起一条，旧的留作普通快照，同类型最多留
`MAX_AUTO` 条。

凭据不落盘：与 `ui_form` 共用同一套清洗（剔 `api_key` / `hf_token`）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .ui_form import _clean_form, _clean_spec

# 测试要能重定向（与 resume / history 同一套做法）
_env = os.environ.get("TAGGER_UI_PRESETS", "").strip()
PRESET_PATH = Path(_env) if _env else Path("configs") / "ui-presets.json"

NAME_MAX = 60
MAX_ITEMS = 60
MAX_AUTO = 10
AUTO_PREFIX = "自动保存 "


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_raw() -> dict[str, Any]:
    """读盘。文件不在 / 坏了都当空——记不住方案不该让页面开不了。"""
    try:
        raw = json.loads(PRESET_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    items = raw.get("items")
    clean: list[dict[str, Any]] = []
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            name = it.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            clean.append({
                "name": name.strip()[:NAME_MAX],
                "ts": it.get("ts") if isinstance(it.get("ts"), str) else "",
                "auto": bool(it.get("auto")),
                "form": _clean_form(it["form"]) if isinstance(it.get("form"), dict) else {},
                "spec": _clean_spec(it["spec"]) if isinstance(it.get("spec"), dict) else {},
            })
    auto_name = raw.get("auto_name")
    return {
        "active": raw.get("active") if isinstance(raw.get("active"), str) else None,
        "auto": bool(raw.get("auto")),
        "auto_name": auto_name.strip()[:NAME_MAX] if isinstance(auto_name, str) and auto_name.strip() else None,
        "items": clean[:MAX_ITEMS],
    }


def _write(data: dict[str, Any]) -> None:
    try:
        PRESET_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PRESET_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(tmp, PRESET_PATH)
        try:
            PRESET_PATH.chmod(0o600)     # 里面有本机路径，不必让其他用户读
        except OSError:
            pass
    except OSError:
        pass                              # 写不了就只是"这次没记住"，不报错


def _meta(item: dict[str, Any]) -> dict[str, Any]:
    return {"name": item["name"], "ts": item.get("ts", ""), "auto": bool(item.get("auto"))}


def meta(data: dict[str, Any] | None = None, *, active: str | None = None) -> dict[str, Any]:
    """给顶栏下拉用的轻量结构：只有名字/时间/是否自动，不带 form。"""
    data = data if data is not None else _load_raw()
    names = {it["name"] for it in data["items"]}
    act = active if active is not None else data.get("active")
    return {
        "items": [_meta(it) for it in data["items"]],
        "active": act if act in names else None,
        "auto": bool(data.get("auto")) and bool(data.get("auto_name")) and data["auto_name"] in names,
        "auto_name": data.get("auto_name"),
    }


def _find(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    for it in data["items"]:
        if it["name"] == name:
            return it
    return None


def payload(name: str) -> dict[str, Any] | None:
    it = _find(_load_raw(), name)
    if it is None:
        return None
    return dict(_meta(it), form=it["form"], spec=it["spec"])


def save(name: str, form: dict[str, Any], spec: dict[str, Any] | None = None,
         *, activate: bool = True) -> dict[str, Any]:
    """存一份具名快照。同名就地覆盖（当作"更新这份方案"）。"""
    clean_name = (name or "").strip()[:NAME_MAX]
    if not clean_name:
        raise ValueError("方案名不能为空")
    data = _load_raw()
    if len(data["items"]) >= MAX_ITEMS:
        # 只挤掉最旧的**普通**快照，自动保存那几条留给 set_auto 自己回收
        for idx in range(len(data["items"])):
            if not data["items"][idx]["auto"]:
                data["items"].pop(idx)
                break
    old = _find(data, clean_name)
    entry = {
        "name": clean_name,
        "ts": _now(),
        "auto": bool(old.get("auto")) if old else False,
        "form": _clean_form(form or {}),
        "spec": _clean_spec(spec or {}),
    }
    if old is None:
        data["items"].append(entry)
    else:
        data["items"][data["items"].index(old)] = entry
    if activate:
        data["active"] = clean_name
    _write(data)
    return meta(data)


def remove(name: str) -> dict[str, Any]:
    data = _load_raw()
    data["items"] = [it for it in data["items"] if it["name"] != name]
    if data.get("auto_name") == name:
        data["auto"], data["auto_name"] = False, None
    if data.get("active") == name:
        data["active"] = None
    _write(data)
    return meta(data)


def apply(name: str) -> dict[str, Any] | None:
    """切换方案：把 active 指过去并把快照原样回传（一次往返，少一个竞态）。"""
    data = _load_raw()
    it = _find(data, name)
    if it is None:
        return None
    data["active"] = it["name"]
    _write(data)
    return dict(_meta(it), form=it["form"], spec=it["spec"], state=meta(data))


def _drop_extra_auto(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    autos = [i for i, it in enumerate(items) if it["auto"]]
    if len(autos) <= MAX_AUTO:
        return items
    for idx in reversed(autos[:len(autos) - MAX_AUTO]):
        items.pop(idx)
    return items


def set_auto(enabled: bool, *, form: dict[str, Any] | None = None,
             spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """开关自动保存；`form` 给了就顺手写一次当前内容。

    开启时新起一条「自动保存 <时间戳>」并设为 active；关闭只清开关，快照留着
    （关掉之后它仍然是一份可以切回去的普通方案）。
    """
    data = _load_raw()
    if enabled:
        if not data.get("auto") or not data.get("auto_name") or _find(data, data["auto_name"]) is None:
            name = AUTO_PREFIX + _now()
            while _find(data, name) is not None:     # 同一秒内连点两次
                name += "."
            for it in data["items"]:
                it["auto"] = False
            _drop_extra_auto(data["items"])
            data["items"].append({"name": name, "ts": _now(), "auto": True,
                                  "form": {}, "spec": {}})
            data["auto_name"] = name
            data["active"] = name
        data["auto"] = True
        if form is not None or spec is not None:
            _write_into_auto(data, form, spec)
    else:
        data["auto"] = False
        if data.get("auto_name"):
            it = _find(data, data["auto_name"])
            if it is not None:
                it["auto"] = False           # 关掉后它就是普通快照
    _write(data)
    return meta(data)


def _write_into_auto(data: dict[str, Any], form: dict[str, Any] | None,
                     spec: dict[str, Any] | None) -> bool:
    name = data.get("auto_name")
    it = _find(data, name) if name else None
    if it is None:
        return False
    if form is not None:
        it["form"] = _clean_form(form)
    if spec is not None:
        it["spec"] = _clean_spec(spec)
    it["ts"] = _now()
    it["auto"] = True
    return True


def autosave(form: dict[str, Any] | None = None,
             spec: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """自动保存的唯一入口：没开开关就是空操作（不该在后台偷偷攒文件）。"""
    data = _load_raw()
    if not data.get("auto") or not data.get("auto_name"):
        return None
    if not _write_into_auto(data, form, spec):
        return None
    _write(data)
    return meta(data)
