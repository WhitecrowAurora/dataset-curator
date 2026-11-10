"""把每分片的画师表传到数据集仓库（在远端执行机上跑）。

要点：
- `HF_ENDPOINT` 必须显式改成 huggingface.co：那台机器机器级变量指向 hf-mirror，
  而镜像是只读的，写会得到 "you must use a write token" 这种误导性 403；
- token 用 K:\\anlia\\hf_token_write.txt（fine-grained，scope 含 Anime-2024Dru768-8M）；
- 走本地 7897 代理，否则 huggingface.co 会被 SNI 阻断；
- 一次 commit 传完 844 个文件，不要逐个 commit（会把仓库历史刷成 844 条）。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ["HF_ENDPOINT"] = "https://huggingface.co"
os.environ["HTTP_PROXY"] = "http://127.0.0.1:7897"
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7897"
os.environ.pop("ALL_PROXY", None)

from huggingface_hub import HfApi                      # noqa: E402

REPO = "MatchaCuratio/Anime-2024Dru768-8M"
SRC = Path(r"K:\anlia\_artist\shards")
TOKEN_FILE = Path(r"K:\anlia\hf_token_write.txt")


def main() -> int:
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    files = sorted(SRC.glob("*.parquet"))
    total = sum(f.stat().st_size for f in files)
    print(f"[upload] {len(files)} files, {total / 2**20:.1f} MB", flush=True)

    api = HfApi(token=token)
    who = api.whoami()
    print(f"[upload] as {who.get('name')}", flush=True)

    t0 = time.time()
    api.upload_folder(
        folder_path=str(SRC),
        path_in_repo="artist",
        repo_id=REPO,
        repo_type="dataset",
        commit_message=f"artist tables per shard: {len(files)} files, "
                       f"{total / 2**20:.1f} MB (danbooru post id -> artists)",
        allow_patterns=["*.parquet"],
    )
    print(f"[upload] done in {int(time.time() - t0)}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
