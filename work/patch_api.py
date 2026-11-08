"""用 f55dc72 里的真实实现，替换我先前手写的占位方法。

占位版本只为"别让整页崩"临时写的，路径和返回结构都和真实契约不一致
（例如 probeNlApi 我写成了 /api/nlapi/probe，真实是 /api/nl-api/probe）。
"""
import io
import re

OLD = 'work/old_api.ts'
CUR = 'tagger/web/ui/src/lib/api.ts'

REPLACE = ['vllmCapability', 'probeNlApi', 'saveNlApiKey', 'pause', 'resume',
           'restart', 'saveProxy', 'testProxy']

old = io.open(OLD, encoding='utf-8').read()
cur = io.open(CUR, encoding='utf-8').read()


def bounds(src: str, name: str):
    m = re.search(r'^  ' + name + r'\s*[:(]', src, re.M)
    if not m:
        return None
    nxt = re.search(r'^  \w+\s*[:(]', src[m.start() + 1:], re.M)
    end = m.start() + 1 + nxt.start() if nxt else len(src)
    return m.start(), end


cnt = 0
for name in REPLACE:
    ob = bounds(old, name)
    cb = bounds(cur, name)
    if not ob or not cb:
        print('skip', name, bool(ob), bool(cb))
        continue
    cur = cur[:cb[0]] + old[ob[0]:ob[1]].rstrip() + '\n' + cur[cb[1]:].lstrip('\n')
    cnt += 1

io.open(CUR, 'w', encoding='utf-8', newline='\n').write(cur)
print('replaced', cnt)
