"""Web 层：把本地打标包装成"浏览器点两下就能跑"的服务。

分工：
    jobs.py  任务簿（线程跑、事件流、单任务串行）
    app.py   HTTP / WebSocket 路由 + 前端静态文件
    ui/      前端源码（Vite + React + TS + Tailwind），构建产物在 ui/dist

为什么单独一层而不是塞进 cli.py：CLI 是"一次一条命令、跑完退出"，
Web 要的是"进程常驻、任务可查、进度可订阅"。两套生命周期硬缝在一起，
最后一定是 CLI 里长出一堆 `if web:` 分支。
"""

__all__ = ["app", "jobs"]
