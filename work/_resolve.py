"""按给定规则解决 git 冲突标记。

用法：python work/_resolve.py <file> <ours|theirs> [块号:ours|theirs ...]

没单列的块走默认侧。块号从 1 开始，与 work/_conflicts.py 的输出一一对应。
"""
import sys

path, default = sys.argv[1], sys.argv[2]
rules = {}
for arg in sys.argv[3:]:
    num, side = arg.split(":")
    rules[int(num)] = side

lines = open(path, encoding="utf-8").read().split("\n")
out = []
i = 0
idx = 0
while i < len(lines):
    line = lines[i]
    if not line.startswith("<<<<<<<"):
        out.append(line)
        i += 1
        continue
    start = i
    mid = end = None
    j = i + 1
    while j < len(lines):
        if lines[j].startswith("=======") and mid is None:
            mid = j
        elif lines[j].startswith(">>>>>>>"):
            end = j
            break
        j += 1
    if end is None:
        raise SystemExit(f"第 {start + 1} 行的冲突块没有结束标记")
    idx += 1
    ours = lines[start + 1:mid]
    theirs = lines[mid + 1:end]
    side = rules.get(idx, default)
    out.extend(ours if side == "ours" else theirs)
    i = end + 1

open(path, "w", encoding="utf-8", newline="\n").write("\n".join(out))
print(f"已解决 {idx} 块，规则 {rules or default}")
