"""从客户端源码生成只依赖标准库的独立 worker.py。"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SOURCES = (
    "fox_worker.py", "netutil.py", "hardware.py", "nl_api.py",
    "center_delivery.py", "blob_client.py", "artifacts.py", "storage/atomic.py",
    "center_runtime/__init__.py", "center_runtime/artifacts.py", "center_runtime/liveness.py",
    "center_runtime/identity.py",
    "verification/__init__.py", "verification/history.py", "verification/payload.py", "verification/source.py", "verification/local.py",
    "naming.py",
)
LOADER = '''#!/usr/bin/env python3
"""lulynx 调度 worker；由 devtools/release/build_worker.py 同源生成。

Python 3.10+，无需 pip；模型执行仍由 run --cmd 指定。
"""
import base64
import importlib.abc
import importlib.util
import io
import sys
import zipfile

_PAYLOAD = """{payload}"""


class _Sources(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self):
        self.archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(_PAYLOAD)))
        self.names = set(self.archive.namelist())

    def find_spec(self, fullname, path=None, target=None):
        name = fullname.replace('.', '/')
        package = name + '/__init__.py' in self.names
        source = name + ('/__init__.py' if package else '.py')
        if source in self.names:
            return importlib.util.spec_from_loader(fullname, self, is_package=package)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        name = module.__name__.replace('.', '/')
        source = name + ('/__init__.py' if module.__spec__.submodule_search_locations is not None else '.py')
        module.__file__ = __file__ + '/' + source
        exec(compile(self.archive.read(source), module.__file__, 'exec'), module.__dict__)


if __name__ == '__main__':
    sys.meta_path.insert(0, _Sources())
    from tagger.fox_worker import main
    raise SystemExit(main())
'''


def build(output: Path) -> dict:
    sources = {"tagger/__init__.py": b"", "tagger/storage/__init__.py": b""}
    sources.update({"tagger/" + name: (ROOT / "tagger" / name).read_bytes()
                    for name in SOURCES})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(sources.items()):
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(LOADER.format(payload=payload), encoding="utf-8", newline="\n")
    report = {"file": output.name, "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
              "sources": {name: hashlib.sha256(content).hexdigest()
                          for name, content in sorted(sources.items())}}
    output.with_name("worker-sources.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "dist/release/worker.py")
    args = parser.parse_args()
    print(json.dumps(build(args.out), indent=2))
