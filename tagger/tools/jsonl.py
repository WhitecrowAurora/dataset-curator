"""流式清洗已完成的 JSONL 标签产物，保留原文件与非标签字段。"""
from __future__ import annotations

import json
import os
from pathlib import Path


def strip_tag_confidence(row: dict) -> int:
    """原地移除 tags 数组中的概率，兼容分片内的 JSON 字符串。"""
    removed = 0
    tags = row.get("tags")
    if isinstance(tags, list):
        cleaned = []
        for tag in tags:
            if (isinstance(tag, (list, tuple)) and len(tag) == 2
                    and isinstance(tag[0], str) and isinstance(tag[1], (int, float))):
                cleaned.append(tag[0])
                removed += 1
            else:
                cleaned.append(tag)
        if removed:
            row["tags"] = cleaned
    tag = row.get("tag")
    if isinstance(tag, dict):
        removed += strip_tag_confidence(tag)
    elif isinstance(tag, str) and tag.lstrip().startswith("{"):
        try:
            payload = json.loads(tag)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            count = strip_tag_confidence(payload)
            if count:
                row["tag"] = json.dumps(payload, ensure_ascii=False)
                removed += count
    return removed


def input_files(path: str, recursive: bool = False) -> tuple[Path, list[Path]]:
    root = Path(path.strip()).expanduser().resolve()
    if not path.strip():
        raise ValueError("请选择已打好的 JSONL 文件或文件夹")
    if root.is_file() and root.suffix.lower() == ".jsonl":
        return root, [root]
    if not root.is_dir():
        raise ValueError("输入必须是 JSONL 文件或文件夹")
    files = sorted(p for p in (root.rglob("*") if recursive else root.iterdir())
                   if p.is_file() and p.suffix.lower() == ".jsonl")
    if not files:
        raise ValueError("所选范围没有 JSONL 文件")
    return root, files


def clean_file(source: Path, destination: Path) -> dict:
    result = {"source": str(source), "output": str(destination), "rows": 0,
              "changed_rows": 0, "removed_scores": 0, "invalid_rows": 0,
              "invalid_lines": []}
    destination.parent.mkdir(parents=True, exist_ok=True)
    # 失败只留下明确命名的 .partial，完整写入后才改为 JSONL。
    partial = destination.with_suffix(".jsonl.partial")
    with source.open(encoding="utf-8-sig", newline="") as reader, \
            partial.open("x", encoding="utf-8", newline="") as writer:
        for line_number, line in enumerate(reader, 1):
            if not line.strip():
                writer.write(line)
                continue
            result["rows"] += 1
            try:
                row = json.loads(line)
            except ValueError:
                row = None
            if not isinstance(row, dict):
                result["invalid_rows"] += 1
                if len(result["invalid_lines"]) < 10:
                    result["invalid_lines"].append(line_number)
                writer.write(line)
                continue
            count = strip_tag_confidence(row)
            if count:
                result["changed_rows"] += 1
                result["removed_scores"] += count
                newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                writer.write(json.dumps(row, ensure_ascii=False) + newline)
            else:
                writer.write(line)
        writer.flush()
        os.fsync(writer.fileno())
    # 输出目录由每次任务独立创建，完成后再提交文件名。
    partial.rename(destination)
    return result
