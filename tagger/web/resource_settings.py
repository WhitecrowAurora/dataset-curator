"""本机资源偏好；调度员不能覆盖本机磁盘阈值。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..config import DEFAULT_TIERS, load_ops, load_tier_table
from ..storage import disk_policy


class DiskSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    disk_min_free_gb: float = Field(ge=0, le=1048576)
    disk_min_free_pct: float = Field(ge=0, le=1)


router = APIRouter(prefix='/api/settings/disk')


@router.get('')
def load() -> DiskSettings:
    policy = load_ops(load_tier_table(DEFAULT_TIERS)).guard_policy()
    return DiskSettings(**{key: getattr(policy, key) for key in disk_policy.RANGES})


@router.put('')
def save(settings: DiskSettings) -> DiskSettings:
    try:
        disk_policy.save(settings.model_dump())
    except OSError as exc:
        raise HTTPException(500, '磁盘保护设置保存失败，请检查配置目录是否可写') from exc
    return settings
