"""列出 app.py 里未解决的冲突块：位置、两侧行数、两侧首尾各 3 行。

只读，不改文件。冲突解决时用它一眼看清"这块是同一函数两版，还是两侧各有一摊新东西"。
"""
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "tagger/web/app.py"
lines = open(path, encoding="utf-8").read().split("\n")

i = 0
idx = 0
while i < len(lines):
    if lines[i].startswith("<<<<<<<"):
        start = i
        mid = None
        end = None
        j = i + 1
        while j < len(lines):
            if lines[j].startswith("=======") and mid is None:
                mid = j
            elif lines[j].startswith(">>>>>>>"):
                end = j
                break
            j += 1
        if end is None:
            print(f"!! 第 {start + 1} 行开始的冲突块没有结束标记")
            break
        idx += 1
        ours = lines[start + 1:mid]
        theirs = lines[mid + 1:end]
        print(f"--- 块 {idx}: 行 {start + 1}-{end + 1}  ours={len(ours)} 行 theirs={len(theirs)} 行")
        for tag, block in (("ours  ", ours), ("theirs", theirs)):
            head = [t for t in block[:8] if t.strip()][:3]
            tail = [t for t in block[-4:] if t.strip()][:2]
            for t in head:
                print(f"  {tag} | {t.strip()[:96]}")
            if tail and tail != head:
                for t in tail:
                    print(f"  {tag}… | {t.strip()[:96]}")
        i = end
    i += 1
print(f"共 {idx} 块")
