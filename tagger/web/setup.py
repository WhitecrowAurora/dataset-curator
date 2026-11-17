"""快速配置的完成状态和本地封包偏好；执行配置仍由原有接口负责。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from ..storage.atomic import read_json, write_json

ROOT = Path(__file__).resolve().parents[2]
SETUP_PATH = Path(os.environ.get("TAGGER_SETUP", "") or ROOT / "configs" / "setup.json")


class SetupState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completed: bool = False
    dismissed: bool = False
    usage: Literal["local", "center"] = "center"
    pack_format: Literal["parquet", "webdataset", "zip"] = "parquet"


router = APIRouter(prefix="/api/setup")


@router.get("")
def load() -> SetupState:
    raw = read_json(SETUP_PATH)
    try:
        return SetupState.model_validate(raw or {})
    except ValueError:
        return SetupState()


@router.put("")
def save(state: SetupState) -> SetupState:
    try:
        write_json(SETUP_PATH, state.model_dump())
    except OSError as exc:
        raise HTTPException(500, "快速配置保存失败，请检查配置目录是否可写") from exc
    return state
