"""部署自检：点一下，把"这台机器现在能不能干活"逐项跑一遍。

为什么要有这个东西
------------------
用户在自己的机器上装完（解压 → install.bat → 双击 exe），出问题时的表现
高度相似：页面能打开，但一点开始就报错，而错误文本指向的往往不是真正的
根因（比如"分片不存在"其实是环境里少了个包）。把 15 项检查排成一次自检，
把"哪一环断了"直接摆出来，比让他贴日志快得多。

设计上的两个取舍
----------------
1. **不建任务**。自检自己走一遍最小链路（能不能读图 → 能不能连上服务 →
   能不能拿到一份能解析的回复），不写进任务历史。用户点"测试"不该在
   「历史任务」里留一条记录。
2. **每项之间留一点时间**。真实检查十几项可能 200ms 就跑完，动画一闪而过，
   用户根本看不清测了什么，反而怀疑"它到底跑了没有"。所以每项至少停留
   MIN_STEP_S，宁可慢 5 秒也要让人看明白。

状态语义：pass 通过 / fail 断了 / skip 这台机器上没这项或当前用不上
（没起服务、没装 torch）。**skip 不算失败**——部署验证不该因为"没开那个
可选功能"整片飘红。
"""

from __future__ import annotations

import base64
import io
import json
import os
import platform
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}

# 每项最少露脸多久（秒）。见模块头注释：这是给人看的节奏，不是性能指标。
MIN_STEP_S = 0.35

# 扫目录时最多看多少个条目就收手。自检只关心"有没有图、能不能读"，
# 面对百万张的目录真数完要好几分钟，而这个数对结论没有任何帮助。
SCAN_BUDGET = 5000

# 自检发的那一条推理请求。刻意用最朴素的格式要求：不同引擎的提示词兼容性
# 差异很大，而这一项的结论只是"服务能用、返回能解析"，不是"提示词好不好"。
_PROMPT = (
    "Describe this image. Answer with exactly two blocks:\n"
    "<TAGS>comma, separated, english, tags</TAGS>\n"
    "<CAPTION>one english sentence describing the image</CAPTION>"
)

_TAG_RE = re.compile(r"<TAGS>(.*?)</TAGS>", re.S | re.I)
_CAP_RE = re.compile(r"<CAPTION>(.*?)</CAPTION>", re.S | re.I)


@dataclass
class Step:
    key: str
    title: str
    # pending | running | pass | fail | skip
    status: str = "pending"
    detail: str = ""
    ms: int = 0


def _brief(text: Any, limit: int = 300) -> str:
    """错误信息压成一行可读的：保留换行会把方块撑变形。"""
    s = str(text).replace("\r", " ").replace("\n", " ").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _scan_images(root: Path, recursive: bool,
                 limit: int) -> tuple[list[Path], int, int]:
    """返回 (抽样图, 看到的图片数, 子目录数)。

    不用引擎那套 collect_shards：那要处理 zip/tar/hf 各种来源，自检只想回答
    "这个文件夹里的图能不能读"。走 os.walk 直白且不会因为包格式而误报。
    """
    found: list[Path] = []
    dirs = 0
    seen = 0
    if recursive:
        for cur, subdirs, files in os.walk(root):
            dirs += len(subdirs)
            for name in files:
                seen += 1
                if Path(name).suffix.lower() in IMAGE_EXT:
                    found.append(Path(cur) / name)
                if seen >= SCAN_BUDGET:
                    break
            if seen >= SCAN_BUDGET:
                break
    else:
        try:
            entries = list(os.scandir(root))
        except OSError:
            entries = []
        for e in entries:
            seen += 1
            try:
                if e.is_file() and Path(e.name).suffix.lower() in IMAGE_EXT:
                    found.append(Path(e.path))
            except OSError:
                continue
            if seen >= SCAN_BUDGET:
                break
    found.sort()
    return found[:limit], len(found), dirs


class SelfCheck:
    """进程级单例状态（app.py 持有）。同一时刻只允许一趟。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.status = "idle"            # idle | running | done | stopped
        # 一开始就把 15 格摆出来（全部 pending）。前端不必再维护一份标题表——
        # 两份标题迟早对不上，而"第 7 格叫什么"只有后端知道。
        self.steps: list[Step] = [Step(key=k, title=t) for k, t, _ in _PLAN]
        self.started = 0.0
        self.finished = 0.0
        self.input_dir = ""
        self.recursive = True
        self.sample = 3
        self.url = ""
        self.summary = {"pass": 0, "fail": 0, "skip": 0, "total": 0}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._token: object | None = None

    # ---------------------------------------------------------------- 查询

    def snapshot(self) -> dict:
        with self._lock:
            steps = [asdict(s) for s in self.steps]
            data = {
                "status": self.status,
                "steps": steps,
                "started": self.started,
                "finished": self.finished,
                "input_dir": self.input_dir,
                "recursive": self.recursive,
                "sample": self.sample,
                "url": self.url,
                "summary": dict(self.summary),
            }
        data["elapsed"] = (
            (data["finished"] or time.time()) - data["started"]
            if data["started"]
            else 0.0
        )
        return data

    # ---------------------------------------------------------------- 启停

    def start(self, *, input_dir: str, recursive: bool = True,
              sample: int = 3, url: str = "") -> dict:
        root = Path(str(input_dir or "").strip())
        if not str(root):
            raise ValueError("请先选一个包含图片的文件夹")
        if not root.is_dir():
            raise ValueError(f"目录不存在或不是文件夹：{root}")
        sample = max(1, min(10, int(sample or 3)))

        with self._lock:
            if self.status == "running":
                raise RuntimeError("自检正在跑，等它结束或先停下")
            self.status = "running"
            self.started = time.time()
            self.finished = 0.0
            self.input_dir = str(root)
            self.recursive = bool(recursive)
            self.sample = sample
            self.url = str(url or "").strip()
            self.summary = {"pass": 0, "fail": 0, "skip": 0, "total": 0}
            self.steps = [Step(key=k, title=t) for k, t, _ in _PLAN]
            self._stop.clear()
            token = self._token = object()
            cfg = {"root": root, "recursive": self.recursive,
                   "sample": sample, "url": self.url}

        self._thread = threading.Thread(target=self._run, args=(token, cfg),
                                        name="selfcheck", daemon=True)
        self._thread.start()
        return self.snapshot()

    def stop(self) -> dict:
        self._stop.set()
        with self._lock:
            if self.status == "running":
                self.status = "stopped"
            for s in self.steps:
                if s.status in ("pending", "running"):
                    s.status = "skip"
                    s.detail = s.detail or "已停止"
        return self.snapshot()

    # ---------------------------------------------------------------- 内部

    def _run(self, token: object, cfg: dict) -> None:
        for idx, (key, _title, fn) in enumerate(_PLAN):
            if self._token is not token or self._stop.is_set():
                return
            with self._lock:
                step = self.steps[idx]
                step.status = "running"
            t0 = time.time()
            try:
                status, detail = fn(cfg)
            except Exception as exc:            # noqa: BLE001 - 自检的每一格都要落到结论
                status, detail = "fail", f"{type(exc).__name__}: {exc}"
            took = time.time() - t0
            if took < MIN_STEP_S:
                time.sleep(MIN_STEP_S - took)
            with self._lock:
                step.status = status
                step.detail = _brief(detail)
                step.ms = int((time.time() - t0) * 1000)
                self.summary[status] = self.summary.get(status, 0) + 1
                self.summary["total"] = idx + 1
        with self._lock:
            if self._token is token:
                self.status = "done"
                self.finished = time.time()


# ------------------------------------------------------------------ 各项检查
#
# 每项返回 (status, detail)。detail 是给人看的一句话，成功也要写——
# 只报"通过"不报"通过时看到了什么"，用户没法判断这个结果可不可信。


def _step_python(cfg: dict) -> tuple[str, str]:
    bits = platform.architecture()[0]
    where = "嵌入式" if "env" in sys.executable.lower() else "系统"
    return "pass", f"Python {platform.python_version()} · {bits} · {where}解释器"


def _step_deps(cfg: dict) -> tuple[str, str]:
    mods = ["fastapi", "uvicorn", "httpx", "websockets", "yaml", "PIL",
            "numpy", "pyarrow"]
    missing = []
    for name in mods:
        try:
            __import__(name)
        except Exception:                       # noqa: BLE001
            missing.append(name)
    if missing:
        return "fail", f"缺少：{', '.join(missing)}。跑 install.bat 重新装依赖"
    return "pass", f"{len(mods)} 个关键依赖可用"


def _step_config(cfg: dict) -> tuple[str, str]:
    from ..config import DEFAULT_TIERS, load_ops, load_tier_table

    table = load_tier_table(DEFAULT_TIERS)
    ops = load_ops(table)
    q = ops.quality or {}
    return "pass", (
        f"{DEFAULT_TIERS} 可读 · 质检线 {q.get('pass_score', '默认')}"
        f" / {q.get('drop_score', '默认')}")


def _step_tiers(cfg: dict) -> tuple[str, str]:
    from ..config import DEFAULT_TIERS, load_tier_table

    table = load_tier_table(DEFAULT_TIERS)

    # tiers / models 可能是映射也可能是列表（配置改过几版），只数个数，
    # 不假设容器类型——自检不该因为配置换了写法就误报"坏掉了"。
    def count(value: Any) -> int:
        if isinstance(value, dict):
            return len(value)
        if isinstance(value, (list, tuple)):
            return len(value)
        return 0

    tiers = count(table.get("tiers"))
    models = count(table.get("models"))
    if tiers == 0:
        return "fail", "分档表里没有任何 tiers，配置被改坏了"
    return "pass", f"{tiers} 个显存档位 · {models} 套权重"


def _step_gpu(cfg: dict) -> tuple[str, str]:
    from ..hardware import query_gpus

    gpus = query_gpus()
    if not gpus:
        # 没卡不是"配置错"，是这台机器只能跑 CPU 分类头——所以说清楚而不是报红。
        return "skip", "没有检测到 NVIDIA 显卡（nvidia-smi 不可用）"
    head = ", ".join(f"#{g.index} {g.name} {g.vram_gb:.0f}G" for g in gpus[:4])
    more = f" 等 {len(gpus)} 张" if len(gpus) > 4 else ""
    return "pass", head + more


def _step_disk(cfg: dict) -> tuple[str, str]:
    from ..pipeline import free_bytes

    root: Path = cfg["root"]
    free = free_bytes(root)
    gb = free / (1 << 30)
    if gb < 2:
        return "fail", f"目标盘只剩 {gb:.1f}GB，跑起来很快会写满"
    return "pass", f"输入盘剩余 {gb:.1f}GB"


def _step_scan(cfg: dict) -> tuple[str, str]:
    imgs, total, dirs = _scan_images(cfg["root"], cfg["recursive"], cfg["sample"])
    cfg["_images"] = imgs
    cfg["_total"] = total
    cfg["_dirs"] = dirs
    if total == 0:
        return "fail", (
            "这个文件夹里没有图片。支持 "
            + "/".join(sorted(e.lstrip(".") for e in IMAGE_EXT))
            + ("（已含子文件夹）" if cfg["recursive"] else "（没开子文件夹）"))
    return "pass", f"找到 {total}{'+' if total >= SCAN_BUDGET else ''} 张图 · 子目录 {dirs} 个"


def _step_recursive(cfg: dict) -> tuple[str, str]:
    if not cfg["recursive"]:
        return "skip", "没有勾选包含子文件夹"
    dirs = cfg.get("_dirs", 0)
    if dirs == 0:
        return "skip", "勾选了子文件夹，但这层没有子目录"
    return "pass", f"{dirs} 个子目录已纳入扫描"


def _step_decode(cfg: dict) -> tuple[str, str]:
    from PIL import Image

    imgs = cfg.get("_images") or []
    if not imgs:
        return "skip", "没有可抽样的图片"
    ok = 0
    bad: list[str] = []
    for p in imgs:
        try:
            with Image.open(p) as im:
                im.verify()
            with Image.open(p) as im:
                w, h = im.size
                mode = im.mode
            ok += 1
            cfg.setdefault("_first", (p, w, h, mode))
        except Exception as exc:                # noqa: BLE001
            bad.append(f"{p.name}: {_brief(exc, 60)}")
    if not ok:
        return "fail", "抽样图片全部打不开：" + "; ".join(bad[:3])
    first = cfg.get("_first")
    label = f"首张 {first[1]}×{first[2]} {first[3]}" if first else ""
    tail = f" · 坏图 {len(bad)} 张" if bad else ""
    return "pass", f"{ok}/{len(imgs)} 张解码正常 · {label}{tail}"


def _step_outdir(cfg: dict) -> tuple[str, str]:
    out = Path("run") / "selfcheck"
    out.mkdir(parents=True, exist_ok=True)
    probe = out / "write-probe.txt"
    probe.write_text("ok", encoding="utf-8")
    size = probe.stat().st_size
    probe.unlink()
    return "pass", f"可写：{out.resolve()}"


def _step_artifact(cfg: dict) -> tuple[str, str]:
    from ..tag_manage import scan

    out = Path("run") / "selfcheck" / "artifacts"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.json"):
        old.unlink()
    image_id = "selfcheck-0001"
    tag = {"image_id": image_id, "tags": ["1girl", "solo"],
           "model": "selfcheck", "created": time.time()}
    nl = {"image_id": image_id, "caption": "a self check image",
          "model": "selfcheck", "created": time.time()}
    (out / f"{image_id}-tag.json").write_text(
        json.dumps(tag, ensure_ascii=False), encoding="utf-8")
    (out / f"{image_id}-nl.json").write_text(
        json.dumps(nl, ensure_ascii=False), encoding="utf-8")
    rows = scan(out)
    kinds = {r.get("kind") for r in rows}
    if len(rows) != 2 or kinds != {"tag", "nl"}:
        return "fail", f"写了两份产物，扫描只认出 {len(rows)} 份（{kinds}）"
    return "pass", "写出 tag + nl 两份 json，合并器能正确识别"


def _step_fingerprint(cfg: dict) -> tuple[str, str]:
    from .vllm_service import FINGERPRINT_PATH

    path = Path(FINGERPRINT_PATH)
    if not path.is_file():
        return "skip", f"还没有指纹（{path}）。起过一次服务后就会生成"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:                    # noqa: BLE001
        return "fail", f"读不了 {path}：{_brief(exc)}"
    if not isinstance(data, dict) or not data:
        return "fail", f"{path} 是空的或格式不对"
    keys = ", ".join(list(data)[:4])
    return "pass", f"{path} 可读 · 字段 {keys}…"


def _step_normalize(cfg: dict) -> tuple[str, str]:
    from ..client import normalize_tags

    raw = "  1Girl, Long Hair ,,  Blue_Eyes , 1girl  "
    tags = normalize_tags(raw)
    if not tags:
        return "fail", "归一化把样例洗成空列表了"
    if tags.count("1girl") > 1:
        return "fail", f"归一化没有去重：{tags}"
    return "pass", f"样例 → {', '.join(tags)}"


def _step_service(cfg: dict) -> tuple[str, str]:
    import httpx

    from .multi_gpu import MULTI

    url = cfg.get("url") or ""
    if not url:
        ready = MULTI.urls()
        if ready:
            url = ready[0]
            cfg["_multi"] = len(ready)
    if not url:
        return "skip", "没有填服务地址，也没起多卡服务"
    cfg["_url"] = url.rstrip("/")
    try:
        with httpx.Client(timeout=8.0, trust_env=False) as c:
            r = c.get(f"{cfg['_url']}/v1/models")
        if r.status_code != 200:
            return "fail", f"{url}/v1/models 返回 {r.status_code}：{_brief(r.text, 120)}"
        models = [m.get("id") for m in (r.json().get("data") or [])]
    except Exception as exc:                    # noqa: BLE001
        return "fail", f"连不上 {url}：{_brief(exc, 140)}"
    if not models:
        return "fail", f"{url} 活着，但没有加载任何模型"
    cfg["_model"] = models[0]
    return "pass", f"{url} 就绪 · 模型 {models[0]}"


def _step_infer(cfg: dict) -> tuple[str, str]:
    import httpx
    from PIL import Image

    if not cfg.get("_url"):
        return "skip", "服务没连上，跳过推理"
    first = cfg.get("_first")
    if not first:
        return "skip", "没有可用的样张"

    path, w, h, _mode = first
    # 缩到 512：这一项测的是链路通不通，不是画质；原图 4000px 只会让等待变长。
    with Image.open(path) as im:
        im = im.convert("RGB")
        if max(im.size) > 512:
            im.thumbnail((512, 512))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    payload = {
        "model": cfg.get("_model") or "tagger",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
        "temperature": 0.2,
        "max_tokens": 220,
    }
    t0 = time.time()
    try:
        with httpx.Client(timeout=180.0, trust_env=False) as c:
            r = c.post(f"{cfg['_url']}/v1/chat/completions", json=payload)
    except Exception as exc:                    # noqa: BLE001
        return "fail", f"请求失败：{_brief(exc, 160)}"
    took = time.time() - t0
    if r.status_code != 200:
        return "fail", f"HTTP {r.status_code}：{_brief(r.text, 200)}"
    try:
        text = r.json()["choices"][0]["message"]["content"]
    except Exception:                           # noqa: BLE001
        return "fail", f"返回不是 OpenAI 格式：{_brief(r.text, 200)}"
    tag_m = _TAG_RE.search(text or "")
    cap_m = _CAP_RE.search(text or "")
    if not tag_m and not cap_m:
        return "fail", f"返回里没有 <TAGS>/<CAPTION> 段：{_brief(text, 160)}"
    tags = [t.strip() for t in (tag_m.group(1) if tag_m else "").split(",") if t.strip()]
    cap = (cap_m.group(1).strip() if cap_m else "")
    return "pass", (
        f"{took:.1f}s · {w}×{h} → {len(tags)} 个 tag"
        + (f" · 描述 {len(cap)} 字符" if cap else " · 无描述")
        + (f" · {cfg['_multi']} 卡服务取首个" if cfg.get("_multi") else ""))


_PLAN: list[tuple[str, str, Callable[[dict], tuple[str, str]]]] = [
    ("python", "解释器", _step_python),
    ("deps", "依赖库", _step_deps),
    ("config", "配置文件", _step_config),
    ("tiers", "显存档位", _step_tiers),
    ("gpu", "显卡", _step_gpu),
    ("disk", "磁盘空间", _step_disk),
    ("scan", "扫描图片", _step_scan),
    ("recursive", "子文件夹", _step_recursive),
    ("decode", "图片解码", _step_decode),
    ("outdir", "产物目录", _step_outdir),
    ("artifact", "产物格式", _step_artifact),
    ("fingerprint", "环境指纹", _step_fingerprint),
    ("normalize", "标签归一", _step_normalize),
    ("service", "推理服务", _step_service),
    ("infer", "真实推理", _step_infer),
]


# 进程级单例：路由和页面读的是同一份。
SELFCHECK = SelfCheck()
