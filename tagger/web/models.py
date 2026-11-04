"""模型清单：WebUI「模型管理」页的后端。

三条口径，改之前先读：

1. **只存差异**。`configs/tiers.yaml` 是团队的默认口径，页面只表达"这台机器上
   加/减/改了什么"。保存时跟配置基线比一遍，一样的条目就不写进覆盖文件——
   否则第一次保存就把配置冻结成一份快照，以后别人改配置永远不生效。
2. **不静默删掉在用的模型**。分档表 `tiers[].model` 指向的条目不许隐藏：真
   删掉的话 `resolve_profile` 会在启动服务时才炸，报错还指向别处。
3. **写文件要整体替换**。先写临时文件再 `os.replace`，中途断电不会留半截
   JSON——半截 JSON 会被当成"覆盖为空"，页面上加的模型凭空消失。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# 覆盖文件里允许出现的 VLM 字段。多出来的（比如悄悄塞个 enabled: false）一律丢弃：
# 未知字段进了配置就是"看起来生效、实际没人读"。
_VLM_FIELDS = ("path", "precision", "family", "revision", "quantized_path")
# awq/gptq 是页内量化写回来的格式名。不收它们的话，在模型管理页保存一次
# 就会把 precision 改回 int4——虽然还能跑（quantized_path 还在），但页面上
# 显示的口径就错了。
_PRECISIONS = {"int4", "int8", "bf16", "fp16", "fp32", "auto", "awq", "gptq"}


def _config_paths():
    from ..config import DEFAULT_TIERS, load_model_overlay, load_tier_table, overlay_path

    return DEFAULT_TIERS, load_tier_table, load_model_overlay, overlay_path


def _clean_vlm_spec(raw: dict[str, Any], key: str) -> dict[str, Any]:
    """收敛一条 VLM 模型条目。path 是硬要求，其余给默认值。"""
    spec: dict[str, Any] = {}
    path = str(raw.get("path") or "").strip()
    if not path:
        raise ValueError(f"NL 模型 {key} 缺少权重路径（本地目录或 HF 仓库名）")
    spec["path"] = path
    precision = str(raw.get("precision") or "int4").strip().lower()
    spec["precision"] = precision if precision in _PRECISIONS else "int4"
    spec["family"] = str(raw.get("family") or "qwen3vl").strip() or "qwen3vl"
    revision = str(raw.get("revision") or "").strip()
    spec["revision"] = revision or None
    quantized = str(raw.get("quantized_path") or "").strip()
    spec["quantized_path"] = quantized or None
    return spec


def _vlm_spec_of(raw: dict[str, Any]) -> dict[str, Any]:
    """从分档表的 models 段取同一口径的字段，用来跟提交值比对。"""
    out: dict[str, Any] = {}
    for field in _VLM_FIELDS:
        value = raw.get(field)
        if field == "revision":
            out[field] = (str(value).strip() or None) if value else None
        elif field == "quantized_path":
            out[field] = (str(value).strip() or None) if value else None
        elif field == "precision":
            out[field] = str(value or "int4").strip().lower()
        elif field == "family":
            out[field] = str(value or "qwen3vl").strip() or "qwen3vl"
        else:
            out[field] = str(value or "").strip()
    return out


def _local_state(kind: str, key: str, dirs: dict[str, str]) -> dict[str, Any]:
    """下载目录的现状：在哪、有没有内容、多大、下到哪一步了。

    页面靠这一组字段在「下载 / 进度条 / 删除+打开位置」三个状态之间切换，
    所以判定口径要稳：目录存在且有文件 = 已下载，不看任务历史。
    """
    from . import downloads as dl

    dest = dl.resolve_dir(kind, key, dirs.get(f"{kind}:{key}"))
    exists = dest.is_dir() and any(dest.iterdir())
    state = dl.DOWNLOADS.state_of(kind, key)
    # 目录里有文件 ≠ 下完了：取消 / 报错都会留下半截文件。这个标记让页面
    # 能把"本地已有"和"上次没下完"分开，否则用户会以为可以直接用。
    partial = bool(state) and state.get("status") in ("canceled", "error") and exists
    size = 0
    if exists:
        for path in dest.rglob("*"):
            try:
                if path.is_file():
                    size += path.stat().st_size
            except OSError:
                pass
    try:
        shown = str(dest.relative_to(dl.ROOT)).replace("\\", "/")
    except ValueError:
        shown = str(dest)
    return {
        "local_dir": shown,
        "local_abs": str(dest),
        "exists": exists,
        "partial": partial,
        "local_bytes": size,
        "download": state,
    }


def _custom_dir(kind: str, key: str, raw: Any) -> str:
    """只在"跟我默认位置不一样"时才记进覆盖文件。

    页面每次提交都会把每行的 local_dir 一起带上（它总要显示个位置），照单全收
    等于把当前默认值固化下来——以后默认目录一变，老条目还钉在旧路径上。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    from . import downloads as dl

    given = Path(text)
    try:
        absolute = given if given.is_absolute() else dl.ROOT / given
        if absolute.resolve() == dl.resolve_dir(kind, key, None).resolve():
            return ""
    except OSError:
        pass
    return text


def _download_repo(kind: str, spec_path: str) -> str:
    """这条能不能下载、从哪个仓库下。

    NL 的 `path` 既可能是 HF 仓库名，也可能是本地目录；只有前者能下载。
    tag 的 `repo` 字段本来就是仓库名，直接给。
    """
    text = (spec_path or "").strip()
    if kind == "tag":
        return text
    if not text or "/" not in text and "\\" not in text:
        return ""
    from . import downloads as dl

    if Path(text).is_absolute() or (dl.ROOT / text).exists():
        return ""                    # 已经是本地目录，没有"下载"这回事
    return text


def catalog() -> dict[str, Any]:
    """当前生效的模型清单（配置 + 覆盖）。页面直接照着渲染。"""
    tiers_path, load_table, load_overlay, overlay_path = _config_paths()
    base = load_table(tiers_path, overlay=False)
    effective = load_table(tiers_path)
    overlay = load_overlay()
    notes: dict[str, str] = overlay.get("notes") or {}
    dirs: dict[str, str] = overlay.get("dirs") or {}
    managed_vlm = set((overlay.get("vlm") or {}).keys())
    managed_tag = set((overlay.get("tag") or {}).keys())

    # 模型 → 出现在哪些分档里。删模型前要拿它拦一道，页面上也直接看得见。
    tiers_of: dict[str, list[str]] = {}
    for t in effective.get("tiers") or []:
        if isinstance(t, dict) and t.get("name") and t.get("model"):
            tiers_of.setdefault(str(t["model"]), []).append(str(t["name"]))

    vlm: list[dict[str, Any]] = []
    for key, spec in (effective.get("models") or {}).items():
        fields = _vlm_spec_of(spec if isinstance(spec, dict) else {})
        vlm.append({
            "key": str(key),
            "note": notes.get(f"vlm:{key}", ""),
            "source": "managed" if str(key) in managed_vlm else "config",
            "tiers": tiers_of.get(str(key), []),
            **fields,
            **_local_state("vlm", str(key), dirs),
            "download_repo": _download_repo("vlm", str(fields.get("path") or "")),
        })
    tag: list[dict[str, Any]] = []
    for key, repo in ((effective.get("tagging") or {}).get("models") or {}).items():
        tag.append({
            "backend": str(key),
            "repo": str(repo),
            "note": notes.get(f"tag:{key}", ""),
            "source": "managed" if str(key) in managed_tag else "config",
            **_local_state("tag", str(key), dirs),
            "download_repo": _download_repo("tag", str(repo)),
        })
    # 配置基线原样再给一份：页面据此显示"配置里有、但现在被隐藏了"的条目，
    # 没有它的话删掉的配置模型就只能手写 JSON 才能恢复。
    base_vlm = [
        {
            "key": str(key),
            "note": notes.get(f"vlm:{key}", ""),
            "source": "config",
            "tiers": tiers_of.get(str(key), []),
            **_vlm_spec_of(spec if isinstance(spec, dict) else {}),
        }
        for key, spec in (base.get("models") or {}).items()
    ]
    base_tag = [
        {
            "backend": str(key),
            "repo": str(repo),
            "note": notes.get(f"tag:{key}", ""),
            "source": "config",
        }
        for key, repo in ((base.get("tagging") or {}).get("models") or {}).items()
    ]
    p = overlay_path()
    from . import downloads as dl

    return {
        "vlm": vlm,
        "tag": tag,
        "base_vlm": base_vlm,
        "base_tag": base_tag,
        "overlay": str(p),
        "overlay_exists": p.is_file(),
        # 下载代理。socks_supported=false 时页面直接把 socks5 选项禁掉，
        # 而不是等用户填完点保存才报"需要 socksio"。
        "proxy": str(overlay.get("proxy") or ""),
        "socks_supported": dl.socks_ready(),
    }


def save(payload: dict[str, Any]) -> dict[str, Any]:
    """整表保存。payload 是页面上的"最终想要的样子"。

    返回值就是保存后的 catalog()，页面不用再拉一次。
    """
    tiers_path, load_table, _load_overlay, overlay_path = _config_paths()
    base = load_table(tiers_path, overlay=False)
    base_models = {str(k): (v if isinstance(v, dict) else {})
                   for k, v in (base.get("models") or {}).items()}
    base_tag = {str(k): str(v)
                for k, v in ((base.get("tagging") or {}).get("models") or {}).items()}
    tiers_referencing: dict[str, str] = {}
    for t in base.get("tiers") or []:
        if isinstance(t, dict) and t.get("model") and t.get("name"):
            tiers_referencing.setdefault(str(t["model"]), str(t["name"]))

    raw_vlm = payload.get("vlm")
    raw_tag = payload.get("tag")
    if not isinstance(raw_vlm, list) or not raw_vlm:
        raise ValueError("至少保留一个 NL 模型：分档表没有模型就没法起服务")
    if not isinstance(raw_tag, list):
        raise ValueError("tag 模型清单格式不对（应为数组）")

    overlay_vlm: dict[str, Any] = {}
    overlay_tag: dict[str, Any] = {}
    notes: dict[str, str] = {}
    dirs: dict[str, str] = {}
    seen_vlm: set[str] = set()
    for item in raw_vlm:
        if not isinstance(item, dict):
            raise ValueError("NL 模型条目格式不对（应为对象）")
        key = str(item.get("key") or "").strip()
        if not key:
            raise ValueError("NL 模型必须有名字（key）")
        if key in seen_vlm:
            raise ValueError(f"NL 模型名字重复：{key}")
        seen_vlm.add(key)
        spec = _clean_vlm_spec(item, key)
        # 跟配置基线一致就不写进覆盖文件；配置以后被改过也不会被旧值盖住。
        if base_models.get(key) and _vlm_spec_of(base_models[key]) == spec:
            pass
        else:
            overlay_vlm[key] = spec
        note = str(item.get("note") or "").strip()
        if note:
            notes[f"vlm:{key}"] = note
        local = _custom_dir("vlm", key, item.get("local_dir"))
        if local:
            dirs[f"vlm:{key}"] = local

    hidden = [k for k in base_models if k not in seen_vlm]
    blocked = [f"{k}（分档 {tiers_referencing[k]} 在用）" for k in hidden
               if k in tiers_referencing]
    if blocked:
        raise ValueError(
            "这些模型正被分档引用，不能删：" + "、".join(blocked)
            + "。要让某个分档换模型，请先改 configs/tiers.yaml 的 tiers 段。")

    seen_tag: set[str] = set()
    for item in raw_tag:
        if not isinstance(item, dict):
            raise ValueError("tag 模型条目格式不对（应为对象）")
        backend = str(item.get("backend") or "").strip()
        repo = str(item.get("repo") or "").strip()
        if not backend:
            raise ValueError("tag 模型必须填后端名（pixai / wd-eva02 / …）")
        if backend in seen_tag:
            raise ValueError(f"tag 模型后端起名重复：{backend}")
        seen_tag.add(backend)
        if not repo:
            raise ValueError(f"tag 模型 {backend} 缺少仓库名")
        if base_tag.get(backend) != repo:
            overlay_tag[backend] = repo
        note = str(item.get("note") or "").strip()
        if note:
            notes[f"tag:{backend}"] = note
        local = _custom_dir("tag", backend, item.get("local_dir"))
        if local:
            dirs[f"tag:{backend}"] = local
    tag_hidden = [k for k in base_tag if k not in seen_tag]

    doc = {
        "vlm": overlay_vlm,
        "tag": overlay_tag,
        "notes": notes,
        "dirs": dirs,
        "hidden": hidden,
        "tag_hidden": tag_hidden,
    }
    # 代理不属于这张表，但同一个文件里存着：整表保存时原样带回去，
    # 否则改一次模型清单就把代理设置清掉了（用户会以为网络坏了）。
    proxy = _read_overlay_raw().get("proxy")
    if proxy:
        doc["proxy"] = proxy
    _write(overlay_path(), doc)
    return catalog()


def _write(path: Path, doc: dict[str, Any]) -> None:
    """整体替换写。空覆盖也照写——那是"恢复到配置默认"的表达方式。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _entry(kind: str, key: str) -> dict[str, Any]:
    """在 catalog 里找一条。找不到就抛——前端传错 key 时要说清楚，别静默无操作。"""
    data = catalog()
    bucket = data["vlm"] if kind == "vlm" else data["tag"]
    name = "key" if kind == "vlm" else "backend"
    for item in bucket:
        if str(item.get(name)) == key:
            return item
    raise ValueError(f"清单里没有这个模型：{kind}:{key}")


def start_download(kind: str, key: str) -> dict[str, Any]:
    from . import downloads as dl

    item = _entry(kind, key)
    repo = str(item.get("download_repo") or "")
    dest = Path(item["local_abs"])
    return dl.DOWNLOADS.start(kind, key, repo, dest, proxy=proxy_url())


def _read_overlay_raw() -> dict[str, Any]:
    """读原始 JSON（不做归一化）。写回时用它打底，避免把不认识的键抹掉。"""
    from ..config import overlay_path

    p = overlay_path()
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def proxy_url() -> str:
    from ..config import load_model_overlay

    return str(load_model_overlay().get("proxy") or "")


def set_proxy(url: str) -> dict[str, Any]:
    """单独保存代理。

    走单独接口而不是并进整表保存：代理是"这台机器怎么上网"，与"清单里有
    哪些模型"无关；混在一个保存按钮里，改个代理也得把整张表校验一遍。
    """
    from ..config import overlay_path
    from . import downloads as dl

    text = dl.normalize_proxy(url)
    doc = _read_overlay_raw()
    if text:
        doc["proxy"] = text
    else:
        doc.pop("proxy", None)
    _write(overlay_path(), doc)
    return {"proxy": text, "catalog": catalog()}


def test_proxy(url: str) -> dict[str, Any]:
    from . import downloads as dl

    return dl.test_proxy(url)


def cancel_download(kind: str, key: str) -> dict[str, Any]:
    from . import downloads as dl

    state = dl.DOWNLOADS.cancel(kind, key)
    return state or {}


def delete_files(kind: str, key: str) -> dict[str, Any]:
    """删掉这个模型的本地权重。只允许删 models/ 之内（见 downloads._guard）。"""
    from . import downloads as dl

    item = _entry(kind, key)
    if item.get("download") and item["download"].get("status") == "running":
        raise ValueError("还在下载，先取消再删")
    dl.DOWNLOADS.delete_dir(Path(item["local_abs"]))
    dl.DOWNLOADS.forget(kind, key)
    return catalog()


def reveal_files(kind: str, key: str) -> dict[str, Any]:
    from . import downloads as dl

    item = _entry(kind, key)
    shown = dl.DOWNLOADS.reveal_dir(Path(item["local_abs"]))
    return {"path": shown}


class _Registry:
    """与 UPLOADS / MANAGER 一致的进程级门面，方便 app.py 注入。"""

    catalog = staticmethod(catalog)
    save = staticmethod(save)
    start_download = staticmethod(start_download)
    cancel_download = staticmethod(cancel_download)
    delete_files = staticmethod(delete_files)
    reveal_files = staticmethod(reveal_files)
    set_proxy = staticmethod(set_proxy)
    test_proxy = staticmethod(test_proxy)


MODELS = _Registry()

