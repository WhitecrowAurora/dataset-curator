"""历史 JSONL 全量对账；客户端与中心使用同一份标准库实现。"""
import hashlib
import json

from .payload import payload_done

PARTS = ("tag", "nl")
TERMINAL_KINDS = ("skipped", "artist", "excused", "excluded")


def ids_digest(ids):
    return hashlib.sha256(json.dumps(sorted(ids), ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def _reason(row, part):
    outcomes = row.get("outcomes") or {}
    if not isinstance(outcomes, dict):
        raise ValueError("outcomes 必须是对象")
    value = outcomes.get(part, {})
    if isinstance(value, dict) and value.get("status") in ("excluded", "failed"):
        reason = str(value.get("reason") or "").strip()
        return (value["status"], reason) if reason else ("missing", "")
    excluded = row.get("excluded")
    if isinstance(excluded, dict):
        reason = str(excluded.get("reason") or "").strip()
        if reason and excluded.get("kind") in TERMINAL_KINDS:
            return "excluded", reason
        if reason:
            return "failed", reason
    raw = row.get(part)
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("ok") is False:
        reason = str(payload.get("error") or "").strip()
        if reason:
            return "failed", reason
    return "missing", ""


def _body(value, part):
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        pass
    if isinstance(value, dict):
        value = value.get("tag") if part == "tag" else (value.get("caption") or value.get("nl"))
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def reconcile(expected, rows):
    """每图每项归并；相同重复幂等，不同成功内容为冲突。"""
    if not isinstance(expected, (list, tuple, set)):
        raise ValueError("源分片图片清单格式不正确")
    expected = list(expected)
    if not expected or any(not isinstance(x, str) or not x for x in expected):
        raise ValueError("源分片没有有效图片清单")
    if len(set(expected)) != len(expected):
        raise ValueError("源分片图片 ID 重复，不能确认归属")
    known = set(expected)
    merged = {}
    problems = []
    conflicted = set()
    duplicates = 0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("image_id"), str):
            raise ValueError("产物包含缺少 image_id 的记录")
        iid = row["image_id"]
        if iid not in known:
            raise ValueError("产物含源分片之外的图片：" + iid)
        target = merged.setdefault(iid, {"image_id": iid, "tag": "", "nl": "", "outcomes": {}})
        for part in PARTS:
            raw = row.get(part)
            if payload_done(part, raw):
                if payload_done(part, target[part]):
                    if _body(target[part], part) != _body(raw, part):
                        problems.append(iid + ": " + part + " 内容冲突")
                        conflicted.add(part)
                        continue
                    else:
                        duplicates += 1
                target[part] = raw
                target["outcomes"].pop(part, None)
            elif not payload_done(part, target[part]):
                status, reason = _reason(row, part)
                old = target["outcomes"].get(part, {})
                if status != "missing" and (old.get("status") != "excluded" or status == "excluded"):
                    target["outcomes"][part] = {"status": status, "reason": reason}
    counts = {part: dict(success=0, excluded=0, failed=0, missing=0) for part in PARTS}
    normalized = []
    for iid in sorted(known):
        row = merged.get(iid, {"image_id": iid, "tag": "", "nl": "", "outcomes": {}})
        for part in PARTS:
            status = "success" if payload_done(part, row[part]) else row["outcomes"].get(part, {}).get("status", "missing")
            counts[part][status] += 1
        normalized.append(row)
    complete = [part for part in PARTS if not counts[part]["missing"] and not counts[part]["failed"] and part not in conflicted]
    report = {"expected": len(known), "ids_sha256": ids_digest(known), "parts": counts,
              "complete_parts": complete, "accounted": all(not counts[p]["missing"] for p in PARTS),
              "ok": len(complete) == 2, "conflicts": problems[:100], "duplicates": duplicates}
    return normalized, report


def read_rows(paths):
    for path in paths:
        if str(path).lower().endswith(".parquet"):
            from ..artifacts import iter_parquet_artifacts
            yield from iter_parquet_artifacts(path)
            continue
        if str(path).lower().endswith(".json"):
            with open(path, encoding="utf-8") as stream:
                data = json.load(stream)
            rows = data if isinstance(data, list) else data.get("rows", [data]) if isinstance(data, dict) else None
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("历史 JSON 产物格式不正确")
            yield from rows
            continue
        with open(path, encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    raise ValueError(f"{path}: 第 {number} 行不是有效 JSON") from exc
                if not isinstance(row, dict):
                    raise ValueError(f"{path}: 第 {number} 行不是对象")
                yield row
