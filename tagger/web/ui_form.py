"""新建任务页的「上次填的那套」。

存两份，用途不同
----------------
`form`  页面 state 的快照（camelCase，键名与 useState 同名）。**改一项存一项**，
        防抖 700ms 写盘——所以中途关掉程序也不丢。
`spec`  提交成功时的 JobSpec（snake_case）。它是"行为"那一层的形状：字段语义
        跨版本稳定，所以旧记录读回来还能对上；也是给 CLI 的 resume 同一套口径。

恢复顺序是 form 覆盖默认值、spec 再补 form 里没有的键。两份都缺就照默认值开。

为什么不直接存 JobSpec 一份就够
----------------------------
JobSpec 必须等用户点「开始打标」才存在，而"填一项记一项"要求输入过程中就能写。
从页面 state 现造一个 JobSpec 得复制 submit 里那一百多行分支（含校验、含异步
探测），一份逻辑两处维护迟早对不上。所以存 state 本身就是最不容易漂的做法。

凭据不落盘
----------
`api_key` / `hf_token` 有专门的存档（`configs/nl_api.json`），没理由再抄一份明文
到另一个文件里。两份都剔。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# 测试要能重定向（与 resume / history 同一套做法）
_env = os.environ.get("TAGGER_UI_FORM", "").strip()
FORM_PATH = Path(_env) if _env else Path("configs") / "ui-form.json"

# 小写是 spec 的键，驼峰是 form 的键，两种都拦。
_SECRETS = {"api_key", "hf_token", "apiKey", "hfToken", "hf_token_"}

# form 里只收"能被 JSON 原样带回来"的标量 / 数组 / 数字字典。
# 嵌套太深的对象一律丢：表单里没有这种东西，留着只会在恢复时炸类型。
_MAX_STR = 4096


def _clean_scalar(v: Any) -> Any:
    if isinstance(v, str):
        return v[:_MAX_STR]
    if isinstance(v, (bool, int, float)):
        return v
    return None


def _clean_form(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or k in _SECRETS:
            continue
        if v is None:
            out[k] = None
            continue
        if isinstance(v, list):
            if all(isinstance(x, (str, int, float, bool)) for x in v):
                out[k] = [x[:_MAX_STR] if isinstance(x, str) else x for x in v]
            continue
        if isinstance(v, dict):
            # catThresholds 是 {类别: "0.4"}（字符串），guardPolicy 是
            # {键: 数字}，两种都得留。
            if all(isinstance(x, (int, float, bool, str)) for x in v.values()):
                out[k] = dict(v)
            continue
        scalar = _clean_scalar(v)
        if scalar is not None:
            out[k] = scalar
    return out


def _clean_spec(raw: dict[str, Any]) -> dict[str, Any]:
    """spec 只认 JobSpec 里存在的键——页面传脏数据进来不会被原样写盘。"""
    from .jobs import JobSpec

    known = set(JobSpec.__dataclass_fields__)
    return {k: v for k, v in (raw or {}).items()
            if k in known and k not in _SECRETS}


def load() -> dict[str, Any]:
    """读上次填的。文件不在 / 坏了都返回空——记不住不该让页面开不了。"""
    try:
        raw = json.loads(FORM_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"form": {}, "spec": {}}
    if not isinstance(raw, dict):
        return {"form": {}, "spec": {}}
    form = raw.get("form")
    spec = raw.get("spec")
    return {
        "form": _clean_form(form) if isinstance(form, dict) else {},
        "spec": _clean_spec(spec) if isinstance(spec, dict) else {},
    }


def save(*, form: dict[str, Any] | None = None,
         spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """只更新传进来的那一半：填一项时不必把整份 spec 重写一遍。"""
    current = load()
    if isinstance(form, dict):
        current["form"] = _clean_form(form)
    if isinstance(spec, dict):
        current["spec"] = _clean_spec(spec)
    try:
        FORM_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = FORM_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(tmp, FORM_PATH)
        try:
            FORM_PATH.chmod(0o600)      # 里面有本机路径，不必让其他用户读
        except OSError:
            pass
    except OSError:
        pass                             # 写不了就只是"这次没记住"，不报错
    return current
