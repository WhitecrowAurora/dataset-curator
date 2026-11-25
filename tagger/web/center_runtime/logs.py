"""调度页面的本机日志查看与目录入口。"""
from pathlib import Path
from fastapi import APIRouter, HTTPException

from ...storage.logs import tail
from ...reveal import open_dir


def router(center, service):
    routes = APIRouter(prefix='/api/center/logs')

    def directory(kind):
        if kind not in ('center', 'service'):
            raise HTTPException(400, '日志类型只能为 center 或 service')
        return center.log_file.directory if kind == 'center' else Path(service.log_path).resolve().parent

    @routes.get('')
    def latest(kind: str = 'center'):
        folder = directory(kind)
        path = center.log_file.path if kind == 'center' else Path(service.log_path).resolve()
        if not path.is_file():
            files = sorted(folder.glob('*.log'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
            path = files[0] if files else None
        return {'directory': str(folder), 'path': str(path) if path else '', 'tail': tail(path) if path else []}

    @routes.post('/reveal')
    def reveal(kind: str = 'center'):
        return open_dir(directory(kind))

    return routes
