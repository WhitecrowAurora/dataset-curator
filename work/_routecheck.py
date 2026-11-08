"""比对前端 api.ts 里出现的 /api 路径与后端实际注册的路由。

只读。输出两个清单：前端在用但后端没有（会 404）、后端有但前端没调（可能是被砍掉的界面入口）。
路径里的模板变量按 {x} 归一，查询串一律剥掉，这样 `?a=b` 不会造成假差异。
"""
from __future__ import annotations

import re
from pathlib import Path

front = Path("tagger/web/ui/src/lib/api.ts").read_text(encoding="utf-8")

# 前端：`/api/xxx` 或 `/api/xxx/${encodeURIComponent(...)}`
raw = set()
for m in re.finditer(r"[`'\"](/api/[^`'\"\s]*)", front):
    raw.add(m.group(1))


def norm(path: str) -> str:
    path = path.split("?")[0]
    path = re.sub(r"\$\{[^}]*\}", "{x}", path)
    path = re.sub(r"\{[^}]*\}", "{x}", path)
    return path.rstrip("/") or "/"


used = {norm(p) for p in raw}

import sys  # noqa: E402

sys.path.insert(0, ".")
from tagger.web.app import create_app  # noqa: E402

backend = set()
for route in create_app().routes:
    p = getattr(route, "path", "")
    if p.startswith("/api"):
        backend.add(norm(p))

print("=== 前端在用、后端没有（会 404） ===")
for p in sorted(used - backend):
    print("  ", p)
print("=== 后端有、前端没直接调 ===")
for p in sorted(backend - used):
    print("  ", p)
