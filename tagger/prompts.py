"""NL 提示词的配置文件（仓库根 `prompt/*.json`）。

为什么要这一层
--------------
内置提示词是按产出规格（长度、tag 数量、中英比例）在代码里生成的，想换一套
说法就得改 `client._prompt_for` 再重新打包。对那些"只想让模型换个语气/换套
段落要求"的场景，代价完全不成比例。这里把提示词变成**仓库里可编辑的文件**：

    一个 JSON 文件 = 一份配置，文件名（去掉 .json）就是界面上显示的名字。

中文、空格、括号都允许——名字直接就是文件名，不做 slug 化，否则"改个名字"
就变成"换一份配置"，provenance 反而对不上。

字段
----
    prompt     必填，英文/主提示词。整段替换内置那份（追加语义交给调用方，
               这里不提供 note 拼接，避免"以为是替换、结果是叠加"）。
    prompt_zh  可选，中文提示词。中文比例 > 0 时用它；不写则这批不出中文
               （不会退回去用内置中文版，那会和英文提示词各说各话）。
    system     可选，system 那一句。不写就用内置 SYSTEM_PROMPT。
    note       可选，纯给人看的说明，不参与推理。

以 `_` 开头的键当作注释放行（`"_comment"` 之类），其余未知键一律报错——
手写 JSON 打错一个字母（`promt`）时，静默忽略比报错难查得多。

provenance 靠提示词文本的 sha256 认亲（见 client._cache_fingerprint 与
artifact_expect 的 prompt_sha），所以改名不影响"这批数据用的哪套指令"，
只有改内容才会。

这个模块只管文件的读写与校验，不碰推理。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 与 configs/、models/ 一致：相对当前工作目录（壳子与 CLI 都把仓库根当 cwd）。
# 测试里 monkeypatch 这个常量即可指到临时目录。
_env = os.environ.get("TAGGER_PROMPT_DIR", "").strip()
PROMPT_DIR = Path(_env) if _env else Path("prompt")

# 可选字段的固定顺序：写文件时按它排，读出来也按它兜底。
OPTIONAL_FIELDS = ("prompt_zh", "system", "note")

# 文件名里 Windows 不允许的字符。**中文不在此列**：名字就是文件名。
_BAD_NAME_CHARS = set('\\/:*?"<>|')
# Windows 保留名：拿它们当文件名，写下去会得到一个打不开的文件。
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class PromptError(ValueError):
    """提示词文件不存在 / 格式不对 / 名字非法。继承 ValueError 是为了让
    HTTP 层把它直接翻成 400（见 app.create_job）。"""


@dataclass(frozen=True, slots=True)
class PromptDoc:
    """一份校验过的提示词。`id` 与 `name` 都是文件名，保持一致。"""

    id: str
    path: Path
    prompt: str
    prompt_zh: str = ""
    system: str = ""
    note: str = ""


# ---------------------------------------------------------------- 目录与文件

def dir_path() -> Path:
    return PROMPT_DIR


def ensure_dir() -> Path:
    d = dir_path()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _files() -> list[Path]:
    d = dir_path()
    if not d.is_dir():
        return []
    # 只认 *.json：README、临时文件、编辑器的 .swp 都不该出现在下拉框里。
    return sorted(p for p in d.glob("*.json") if p.is_file())


def _display_path(p: Path) -> str:
    """给界面看的路径。相对仓库根显示，绝对路径留给外部目录。"""
    try:
        return str(p.resolve().relative_to(Path.cwd().resolve())).replace("\\", "/")
    except (ValueError, OSError):
        return str(p)


def _read(path: Path) -> dict[str, str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptError(f"读不了 {_display_path(path)}：{exc}") from exc
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise PromptError(f"{_display_path(path)} 不是合法 JSON：{exc}") from exc
    return _validate(doc, where=_display_path(path))


def _validate(doc: Any, *, where: str) -> dict[str, str]:
    if not isinstance(doc, dict):
        raise PromptError(f"{where} 顶层必须是对象（形如 {{\"prompt\": \"…\"}}）")
    unknown = [k for k in doc if not str(k).startswith("_")
               and k not in ("prompt", *OPTIONAL_FIELDS)]
    if unknown:
        raise PromptError(
            f"{where} 里有不认识的字段：{'、'.join(sorted(map(str, unknown)))}。"
            f"可用字段：prompt（必填）、{'、'.join(OPTIONAL_FIELDS)}")
    out: dict[str, str] = {}
    prompt = doc.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise PromptError(f"{where} 的 prompt 必须是非空字符串")
    out["prompt"] = prompt
    for key in OPTIONAL_FIELDS:
        value = doc.get(key)
        if value is None:
            out[key] = ""
        elif isinstance(value, str):
            # 两侧空白没有语义：全空格的 system 应等于"没写"，否则文件里会留下
            # 一条看着存在、实际空白的字段。
            out[key] = value.strip()
        else:
            raise PromptError(f"{where} 的 {key} 必须是字符串")
    return out


def _write(path: Path, fields: dict[str, str]) -> None:
    """原子写：先写 .tmp 再 rename，中途断电不会留下半个 JSON。"""
    doc: dict[str, Any] = {"prompt": fields["prompt"]}
    for key in OPTIONAL_FIELDS:
        if fields.get(key):
            doc[key] = fields[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def check_name(raw: str) -> str:
    """把用户填的名字变成合法的文件名（不含 .json）。中文原样保留。"""
    name = (raw or "").strip()
    if not name:
        raise PromptError("名字不能为空")
    if len(name) > 60:
        raise PromptError("名字最多 60 个字符")
    bad = sorted({c for c in name if c in _BAD_NAME_CHARS or ord(c) < 32})
    if bad:
        raise PromptError("名字里不能有这些字符：" + " ".join(bad))
    if name.startswith(".") or name.endswith("."):
        raise PromptError("名字不能以点号开头或结尾")
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        raise PromptError(f"「{name}」是系统保留名，换一个")
    # 大小写不敏感地查重：Windows 上 Foo.json 与 foo.json 是同一个文件，
    # 放任下去会出现"下拉框里两条、实际只有一个文件"。
    for p in _files():
        if p.stem.lower() == name.lower():
            raise PromptError(f"已经有叫「{p.stem}」的提示词了")
    return name


# ---------------------------------------------------------------- 对外接口

def list_prompts() -> list[dict[str, Any]]:
    """下拉框用的清单。坏文件也列出来，带 error——静默消失会让人以为文件丢了。"""
    out: list[dict[str, Any]] = []
    for p in _files():
        try:
            stat = p.stat()
        except OSError:
            stat = None
        item: dict[str, Any] = {
            "id": p.stem,
            "name": p.stem,
            "path": _display_path(p),
            "bytes": stat.st_size if stat else 0,
            "mtime": stat.st_mtime if stat else 0.0,
        }
        try:
            doc = _read(p)
        except PromptError as exc:
            item["error"] = str(exc)
        else:
            item["chars"] = len(doc["prompt"])
            item["has_zh"] = bool(doc["prompt_zh"])
            item["has_system"] = bool(doc["system"])
            item["note"] = doc["note"]
        out.append(item)
    return out


def info() -> dict[str, Any]:
    """给 GET /api/prompts 的整体快照：目录 + 清单。"""
    return {"dir": _display_path(dir_path()), "items": list_prompts()}


def find(pid: str) -> Path:
    """按 id 找文件。id 就是文件名，所以不需要拼路径——从目录里比对，
    天然挡住 `../` 之类的穿越。"""
    pid = (pid or "").strip()
    if not pid:
        raise PromptError("没有指定提示词")
    for p in _files():
        if p.stem == pid:
            return p
    names = "、".join(p.stem for p in _files()) or "（空）"
    raise PromptError(
        f"找不到提示词「{pid}」。{_display_path(dir_path())} 里现在有：{names}")


def load(pid: str) -> PromptDoc:
    """现读文件：建任务时选好、跑之前可能刚被编辑过，缓存一份就是在骗自己。"""
    p = find(pid)
    return PromptDoc(id=p.stem, path=p, **_read(p))


def detail(pid: str) -> dict[str, Any]:
    """编辑弹窗用：完整内容 + 元信息。"""
    doc = load(pid)
    return {
        "id": doc.id,
        "name": doc.id,
        "path": _display_path(doc.path),
        "prompt": doc.prompt,
        "prompt_zh": doc.prompt_zh,
        "system": doc.system,
        "note": doc.note,
    }


# 只属于"界面元信息"的键：前端把读到的整个对象原样 PUT 回来时会带上它们，
# 不该因为多带了这几个就被拒。
_META_KEYS = {"id", "name", "path", "bytes", "mtime", "chars",
              "has_zh", "has_system"}


def _body(fields: dict[str, Any]) -> dict[str, Any]:
    """剥掉界面元信息，**保留其余键**——`promt` 这种手写错字必须报出来，
    在这里悄悄滤掉等于把打错的提示词写成了一个空壳配置。"""
    return {k: v for k, v in (fields or {}).items() if k not in _META_KEYS}


def save(pid: str, fields: dict[str, Any]) -> dict[str, Any]:
    p = find(pid)
    clean = _validate(_body(fields), where=_display_path(p))
    _write(p, clean)
    return detail(pid)


def create(name: str, fields: dict[str, Any]) -> dict[str, Any]:
    clean_name = check_name(name)
    where = f"{_display_path(dir_path())}/{clean_name}.json"
    clean = _validate(_body(fields), where=where)
    path = ensure_dir() / f"{clean_name}.json"
    _write(path, clean)
    return detail(clean_name)


def reveal() -> str:
    """在系统文件管理器里打开 prompt 目录。目录不存在就先建，
    否则"打开文件夹"会在第一次使用时报错——那不是错误，是还没建。"""
    target = ensure_dir().resolve()
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", str(target)])          # noqa: S603,S607
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])              # noqa: S603,S607
    else:
        subprocess.Popen(["xdg-open", str(target)])          # noqa: S603,S607
    return str(target)
