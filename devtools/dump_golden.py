"""打印黄金测试的期望值（tests/test_tag_golden.py 里的 EXPECTED）。

什么时候用
----------
1. 首次建立黄金基线；
2. **有意**改了预处理/精度/tag 过滤口径之后，确认新数字是对的，再更新基线。
   无意改动导致的差异应该让测试变红，而不是来这里重刷——所以任何一次
   刷新都必须在提交说明里写清"为什么口径变了"。

为什么跑 CPU
------------
CPU fp32 结果与驱动、tf32、CUDA 版本都无关，换台机器也能复现；
GPU 上同一份代码换个驱动就可能差几个 1e-4，钉不牢。

用法
----
    env\\python\\python.exe devtools\\dump_golden.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

try:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass


def main() -> int:
    from tagger.cpu_tagger import build_backend

    from test_tag_golden import MODEL_DIR, THRESHOLD, _images

    if not MODEL_DIR.is_dir():
        raise SystemExit(f"没有权重目录 {MODEL_DIR}")

    backend = build_backend("wd-eva02", model_dir=MODEL_DIR, device="cpu",
                            precision="fp32", batch_size=1, use_graph=False,
                            verbose=True, thresholds={"default": THRESHOLD})
    backend.load()
    preds = backend.predict_batch(_images())

    print("\nEXPECTED: dict[str, dict[str, float]] = {")
    for i, pred in enumerate(preds):
        print(f'    "img{i}": {{')
        for tag, prob in pred.tags:
            print(f'        {tag!r}: {prob:.4f},')
        print("    },")
    print("}")
    print("\nrating（仅供核对，不写进 EXPECTED）:")
    for i, pred in enumerate(preds):
        print(f"  img{i}: {pred.rating}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
