"""便携 Python 随目录移动；子进程导入同一份仓库源码。"""
import sys
from pathlib import Path


def configure(root=None):
    root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    path = str(root)
    if path not in sys.path:
        sys.path.insert(0, path)
    python = Path(sys.executable).resolve().parent
    # 嵌入式解释器忽略 PYTHONPATH，所有 -m 子进程依靠 site 的相对路径。
    if python == root / 'env' / 'python' and any(python.glob('*._pth')):
        target = python / 'Lib' / 'site-packages' / '_tagger_repo.pth'
        content = '..\\..\\..\\..\n'
        if not target.is_file() or target.read_text(encoding='utf-8-sig') != content:
            try:
                target.write_text(content, encoding='utf-8')
            except OSError as exc:
                raise RuntimeError('便携 Python 的仓库路径无法更新，请把客户端移到可写目录后重试') from exc
