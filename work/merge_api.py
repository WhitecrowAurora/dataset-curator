"""把 f55dc72 版本 api.ts 里被 407ea15 删掉的方法与类型，补回当前文件。

只做"缺什么补什么"：当前已有的定义一律不动（我今天改过的 hfFiles / downloads /
proxy 等保持原样）。切割按行首缩进识别顶层声明与 api 对象内的方法。
"""
import io
import re

OLD = 'work/old_api.ts'
CUR = 'tagger/web/ui/src/lib/api.ts'
OUT = 'work/new_api.ts'

old = io.open(OLD, encoding='utf-8').read()
cur = io.open(CUR, encoding='utf-8').read()


def api_body(src: str) -> tuple[str, int, int]:
    """返回 api 对象体的 (体文本, 起止下标)。"""
    i = src.find('export const api =')
    start = src.find('{', i)
    depth = 0
    end = None
    for k in range(start, len(src)):
        ch = src[k]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = k
                break
    return src[start + 1:end], start, end


def split_methods(body: str) -> dict[str, str]:
    """把 api 对象体按 `  name:` 切成 {名字: 文本}。"""
    lines = body.split('\n')
    blocks: dict[str, str] = {}
    cur_name = None
    buf: list[str] = []
    for line in lines:
        m = re.match(r'^  (\w+)\s*[:(]', line)
        if m:
            if cur_name:
                blocks[cur_name] = '\n'.join(buf).rstrip()
            cur_name = m.group(1)
            buf = [line]
        elif cur_name is not None:
            buf.append(line)
    if cur_name:
        blocks[cur_name] = '\n'.join(buf).rstrip()
    return blocks


def split_types(src: str) -> dict[str, str]:
    """按 `export type X` / `export interface X` 切类型块。"""
    lines = src.split('\n')
    blocks: dict[str, str] = {}
    cur_name = None
    buf: list[str] = []
    for line in lines:
        m = re.match(r'^export (?:type|interface) (\w+)', line)
        if m:
            if cur_name:
                blocks[cur_name] = '\n'.join(buf).rstrip()
            cur_name = m.group(1)
            buf = [line]
        elif cur_name is not None:
            if re.match(r'^export ', line):
                blocks[cur_name] = '\n'.join(buf).rstrip()
                cur_name = None
                buf = []
            else:
                buf.append(line)
    if cur_name:
        blocks[cur_name] = '\n'.join(buf).rstrip()
    # 去掉纯空白的尾巴
    return {k: v for k, v in blocks.items() if v.strip()}


old_body, _, _ = api_body(old)
cur_body, cur_start, cur_end = api_body(cur)

old_methods = split_methods(old_body)
cur_methods = split_methods(cur_body)
old_types = split_types(old)
cur_types = split_types(cur)

missing_methods = [n for n in old_methods if n not in cur_methods]
missing_types = [n for n in old_types if n not in cur_types]

method_text = '\n\n'.join(old_methods[n] for n in missing_methods)
type_text = '\n\n'.join(old_types[n] for n in missing_types)

new = cur
# 1) 方法插到 api 对象结尾之前
anchor = new.rfind('\n}', 0, len(new))
new = new[:anchor] + '\n\n  // ==== 以下从 f55dc72 恢复：407ea15 提交时被整段删掉，\n  // 界面一直在调，导致 tsc 报 174 个错、并在运行时抛\n  // "xxx is not a function" 把整棵树打崩。\n' + method_text + new[anchor:]

# 2) 类型插到 ApiError 之前
m = re.search(r'^export class ApiError', new, re.M)
new = new[:m.start()] + type_text + '\n\n' + new[m.start():]

io.open(OUT, 'w', encoding='utf-8', newline='\n').write(new)
print('methods restored:', len(missing_methods), missing_methods)
print('types restored:', len(missing_types))
print('out lines:', new.count('\n') + 1)
