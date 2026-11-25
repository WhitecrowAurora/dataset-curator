"""重打包仓库的实际分包 ID 清单。"""
from pathlib import Path
from . import store


def companion_tables(sid,repo,revision,token):
    from huggingface_hub import HfApi,snapshot_download
    api=HfApi(token=token)
    revision=api.repo_info(repo,repo_type='dataset',revision=revision).sha
    files=api.list_repo_files(repo,repo_type='dataset',revision=revision);available=set(files)
    tables=[v for v in files if v.startswith('artist/') and v.endswith('.artist.parquet')
        and v.rsplit('/',1)[-1].replace('.artist.parquet','.tar') in available]
    if not tables:raise ValueError('总表 src_shard 指向旧源包；请提供含实际分包路径的总表')
    store.state(sid,message='总表记录的是旧源包；正在准备实际分包 ID 清单')
    snapshot=snapshot_download(repo,repo_type='dataset',revision=revision,token=token,
        allow_patterns=tables,max_workers=8)
    return [Path(snapshot)/v for v in tables],revision
