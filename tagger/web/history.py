"""任务历史落盘。

为什么单独存一份 snapshot
--------------------------
`Job` 是普通类，握着线程、锁、事件、进度表（`jobs.py:549` 起），序列化不了。
所以这里只存 `Job.snapshot()` 的返回值——它本来就是给页面看的那一份，
字段稳定、可 JSON 化。内存里的 `Job` 仍然是唯一真相，磁盘只是"进程没了还能
把列表画出来"的备份。

为什么放在 `run/history/`
------------------------
`run/` 不进货包（`pack-dist.bat` 的白名单里没有它），天然是"每台机器自己的
记录"，不会跟着升级覆盖。而且必须和续做记录 `run/resume-jobs/` 分开：
`storage/resume.py` 的 `load()` 会扫 `resume-jobs/*.json` 挑最新的续做候选，
历史文件混进去会被当成"上次没跑完的任务"。

重启后 running 要改成 stopped
----------------------------
进程一没，线程就没了，但盘上那条记录还写着 running。原样读回来的后果是
页面上多一条永远不动的任务，而且 `JobManager.running()` 会把 pending/running/
paused 都当成"占着位子"，直接卡住新建任务。所以加载时统一改写，并在 error
里写清原因，别让人以为是任务自己挂的。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..storage.atomic import write_json

# 测试要能把这个目录指到临时位置（与 TAGGER_RESUME_PATH 同一套做法）：
# 不隔离的话，跑一次 pytest 就会往真实的 run/history 里塞假任务。
_env = os.environ.get("TAGGER_HISTORY_DIR", "").strip()
HISTORY_DIR = Path(_env) if _env else Path("run") / "history"

# 保留多少条。列表只展示 20 条，留 50 条是为了"翻旧账"时还能找到上上次。
KEEP = 50

_INTERRUPTED = "进程已退出，这个任务没有正常收尾（重启后只能看记录，跑不了）"


def _path(job_id: str) -> Path:
    """一条记录一个文件。id 只留安全字符，别让外部传的 id 跳出目录。"""
    safe = "".join(ch for ch in job_id if ch.isalnum() or ch in "-_")
    return HISTORY_DIR / f"{safe}.json"


def save(snapshot: dict[str, Any]) -> None:
    """写一条。失败一律吞掉：历史写不进去不该让任务本身失败。"""
    job_id = str(snapshot.get("id") or "").strip()
    if not job_id:
        return
    try:
        write_json(_path(job_id), snapshot)
    except OSError:
        pass


def stats_path(job_id: str) -> Path:
    """分桶统计的旁挂文件：与快照同 id，不同后缀。

    为什么不塞进 snapshot：细桶是"每 100 张一行"的整数数组，百万张任务也有
    一万行；快照每次状态变化都整份重写，塞进去会把一份小文件写成几百 KB。
    分开存就能只追加，不必重写。
    """
    safe = "".join(ch for ch in job_id if ch.isalnum() or ch in "-_")
    return HISTORY_DIR / f"{safe}.stats.jsonl"


def append_stats(job_id: str, row: list[int]) -> None:
    """追加一个满桶。失败一律吞掉：统计写不进去不该影响打标。"""
    if not job_id:
        return
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        with stats_path(job_id).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError:
        pass


def load_stats(job_id: str) -> list[list[int]]:
    """读回某任务落盘的桶。坏行跳过，不做长度校验（调用方按自己的列数补齐）。"""
    p = stats_path(job_id)
    if not p.is_file():
        return []
    rows: list[list[int]] = []
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, list) and row:
                    try:
                        rows.append([int(x) for x in row])
                    except (TypeError, ValueError):
                        continue
    except OSError:
        return []
    return rows


def _interrupt(raw: dict[str, Any]) -> dict[str, Any]:
    """把"进行中"改成"已中断"。其余字段原样保留。"""
    if str(raw.get("status") or "") in ("pending", "running", "paused"):
        raw = dict(raw)
        raw["status"] = "stopped"
        raw["error"] = _INTERRUPTED
        raw["ended"] = float(raw.get("ended") or 0) or None
        # 进度条不能停在 60% 那种位置骗人：总量未知的流式任务保持 progress=None。
        if isinstance(raw.get("progress"), (int, float)):
            raw["progress"] = float(raw["progress"])
    return raw


def load_all() -> list[dict[str, Any]]:
    """读全部记录，按创建时间倒序。坏文件跳过——一条记录坏掉不该让列表全空。"""
    if not HISTORY_DIR.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in HISTORY_DIR.glob("*.json"):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        revived = _interrupt(raw)
        out.append(revived)
        if revived is not raw:
            save(revived)          # 改写回盘，下次启动不必再改一遍
    out.sort(key=lambda r: float(r.get("created") or 0), reverse=True)
    _prune(out)
    return out


def _prune(items: list[dict[str, Any]]) -> None:
    """只留最近 KEEP 条。超出的直接删文件。"""
    if len(items) <= KEEP:
        return
    for raw in items[KEEP:]:
        job_id = str(raw.get("id"))
        try:
            _path(job_id).unlink(missing_ok=True)
            # 统计文件一起删：快照没了还留一份孤儿 stats，下次同 id（几乎不可能）
            # 会读到陈旧数据，而且磁盘上会慢慢攒一堆没人认领的小文件。
            stats_path(job_id).unlink(missing_ok=True)
        except OSError:
            pass


def clear(keep: set[str] | None = None) -> int:
    """清空历史，返回删掉了几条快照。

    `keep` 里的 id 不删：页面上那个「清空」按钮要能保住还在跑的任务——
    它们的记录还在被 `_persist` 回写，删掉当场就会被重新写出来，而且用户
    会以为"刚跑的任务丢了"。真正该清的是已经结束的那些。

    快照和旁挂的 stats 一起删：只删快照会留下一堆没人认领的孤儿文件，
    而"清空历史"这个动作的动机正是不要再占盘。
    """
    if not HISTORY_DIR.is_dir():
        return 0
    skip = keep or set()
    removed = 0
    for pattern in ("*.json", "*.stats.jsonl"):
        for p in HISTORY_DIR.glob(pattern):
            # 两种文件名都以 id 开头：`<id>.json` / `<id>.stats.jsonl`
            if p.name.split(".", 1)[0] in skip:
                continue
            try:
                p.unlink(missing_ok=True)
                if pattern == "*.json":
                    removed += 1
            except OSError:
                pass
    return removed
