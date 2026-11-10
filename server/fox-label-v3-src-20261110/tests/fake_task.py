#!/usr/bin/env python3
"""测试用假打标任务：按步推进度、可配置时长与失败码。

环境变量：
  FOXLABEL_DURATION  总时长秒（默认 1.2）
  FOXLABEL_STEPS     进度步数（默认 3）
  FOXLABEL_FAIL      非 0 则以该码退出
  FOXLABEL_PROGRESS  进度文件路径（worker 注入）
"""
import json
import os
import time

dur = float(os.environ.get("FOXLABEL_DURATION", "1.2"))
steps = int(os.environ.get("FOXLABEL_STEPS", "3"))
fail = int(os.environ.get("FOXLABEL_FAIL", "0"))
prog = os.environ.get("FOXLABEL_PROGRESS")

for i in range(1, steps + 1):
    if prog:
        with open(prog, "w", encoding="utf-8") as fh:
            json.dump({"done": i * 10, "total": steps * 10}, fh)
    time.sleep(dur / steps)

if fail:
    print(f"[fake_task] 模拟失败，退出码 {fail}")
    raise SystemExit(fail)
print("[fake_task] 完成")
