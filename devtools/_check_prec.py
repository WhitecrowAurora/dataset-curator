"""临时校验：精度档在 resolve_weights 里的行为。跑完即删。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tagger.config import ConfigError, load_tier_table, resolve_profile  # noqa: E402
from tagger.hardware import primary_gpu  # noqa: E402
from tagger import nl_engine  # noqa: E402

gpu = primary_gpu()
table = load_tier_table()

for prec in (None, "q4", "q5", "q6", "int4"):
    p = resolve_profile(table, gpu, model_override="qwen3vl-8b",
                        precision_override=prec)
    try:
        gguf, mm = nl_engine.resolve_weights(p)
        print(f"{str(prec):5} precision={p.precision:5} -> {gguf.name}")
    except ConfigError as exc:
        print(f"{str(prec):5} precision={p.precision:5} -> 报错: {str(exc).splitlines()[0]}")
