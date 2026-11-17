"""在隔离进程中加载分类模型，避免失败后领取任务或残留显存。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from ...config import ConfigError
from ...runtime.processes import spawn_owned
from ..downloads import ROOT


def load():
    from ...cpu_tagger import build_backend

    cfg = json.loads(os.environ["LULYNX_CLASSIFIER_CHECK"])
    backend = build_backend(cfg["tag_backend"], device=cfg["tag_device"],
                            model=cfg.get("tag_model"),
                            model_dir=cfg["tag_model_dir"],
                            precision=cfg["tag_precision"], batch_size=cfg["tag_batch"],
                            token=os.environ.get("HF_TOKEN"), verbose=False)
    backend.load()


def check(cfg, token, cancel):
    from .inference import Cancelled

    if cancel.is_set():
        raise Cancelled("已停止准备")
    env = dict(os.environ)
    env["LULYNX_CLASSIFIER_CHECK"] = json.dumps(cfg)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    if token:
        env["HF_TOKEN"] = token
    with tempfile.TemporaryFile() as output:
        proc = spawn_owned([sys.executable, "-c",
                                 "from tagger.web.center_runtime.classifier_check import load; load()"],
                                cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            while proc.poll() is None:
                if cancel.wait(0.25):
                    raise Cancelled("已停止分类模型加载")
            if proc.returncode:
                output.seek(0, 2)
                output.seek(max(0, output.tell() - 4096))
                detail = output.read().decode("utf-8", errors="replace").strip()
                raise ConfigError(f"TAG 模型加载失败：{detail}")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
