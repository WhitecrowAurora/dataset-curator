"""HTTP 冒烟：首页、SPA 回退、文档、底栏依赖的两个接口。

只读，不写任何文件。页面本体是前端 client-side 路由，这里只确认
"服务端把 index.html 交出去了、静态资源拿得到"。
"""
import re

from fastapi.testclient import TestClient

from tagger.web.app import create_app

c = TestClient(create_app())

r = c.get("/")
print("GET / ->", r.status_code, f"{len(r.content)} bytes")
assets = re.findall(r"assets/[^\"']+", r.text)
print("bundle:", assets[:3])

for path in ("/downloads", "/deps", "/settings", "/tag-manage", "/models"):
    resp = c.get(path)
    print(f"SPA {path} ->", resp.status_code, "html" if "text/html" in resp.headers.get("content-type", "") else resp.headers.get("content-type"))

for path in ("/api/docs", "/api/activities", "/api/sysinfo", "/api/plan"):
    print(path, "->", c.get(path).status_code)

if assets:
    print("asset fetch ->", c.get("/" + assets[0]).status_code)
