"""冲突与决策页的发色复核接口。"""
from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from ..hair_color.contracts import COLORS, HairDecisionRequest, HairReviewRequest, HairWorkflowSettings, color_name
from ..hair_color.service import HairReviewService
from .downloads import ROOT

router = APIRouter(prefix="/api/conflicts/hair", tags=["conflicts"])


@lru_cache(maxsize=1)
def service():
    from .gpu_start import GPU_START_LOCK
    value = HairReviewService(ROOT / "work/conflicts/hair", ROOT / "models/hair-color")
    value.runtime.before_start = check_main_service
    value.runtime.startup_lock = GPU_START_LOCK
    return value


def check_main_service():
    from .vllm_service import VLLM
    from .multi_gpu import MULTI
    if (VLLM.status in ("starting", "ready", "stopping")
            or MULTI.status in ("starting", "ready", "partial", "stopping")):
        raise ValueError("本机主推理服务仍在使用 GPU：请停止该服务，或在复核设置中填写可复用的服务地址")


@router.get("")
def list_reviews(offset: int = 0, limit: int = 20, status: str = "all", job_id: str = ""):
    from ..review_learning.service import annotate_hair
    payload = {**service().store.list(max(0, offset), max(1, min(100, limit)), status, job_id),
            "colors": [{"color": key, "label": value[0], "swatch": value[1]} for key, value in COLORS.items()]}
    return annotate_hair(payload, service().store.root.parent)


@router.get("/settings")
def settings():
    return service().snapshot()


@router.put("/settings")
def configure(request: HairWorkflowSettings):
    return service().configure(request)


@router.post("/cache/release")
def release_cache():
    from ..runtime.model_cache import MODELS
    MODELS.forget()
    return service().snapshot()


@router.post("", status_code=202)
def start_review(request: HairReviewRequest):
    try:
        return service().submit(request)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{review_id}")
def get_review(review_id: str):
    try:
        return service().store.get(review_id)
    except KeyError as exc:
        raise HTTPException(404, "没有这条发色复核") from exc


@router.get("/{review_id}/image")
def get_image(review_id: str, cropped: bool = False):
    record = get_review(review_id)
    path = service().store.root / "images" / f"{record['id']}.png"
    if cropped and record.get("crop"):
        import io
        from PIL import Image
        with Image.open(path) as source:
            out = io.BytesIO()
            source.crop(record["crop"]).save(out, format="PNG")
        return Response(out.getvalue(), media_type="image/png")
    return FileResponse(path, media_type="image/png")


@router.post("/{review_id}/decide")
def decide(review_id: str, request: HairDecisionRequest):
    return _decide(review_id, request)


def _decide(review_id, request, *, multiple_people=False, confirmed_roles=()):
    with service().writeback_lock:
        return _decide_locked(review_id, request, multiple_people=multiple_people, confirmed_roles=confirmed_roles)


def _decide_locked(review_id, request, *, multiple_people=False, confirmed_roles=()):
    try:
        colors = list(dict.fromkeys(color_name(color) for color in request.colors))
        if "light" in colors:
            raise ValueError("浅色组不是具体发色，请选择白、银、灰或金色")
        if request.no_hair and colors:
            raise ValueError("无头发与具体发色不能同时选择")
        record = service().store.decide(review_id, colors, request.no_hair, multiple_people=multiple_people, confirmed_roles=confirmed_roles)
        from ..hair_color.batch import writeback
        try:
            writeback(record)
            service().store.set_writeback(review_id)
        except (OSError, ValueError) as exc:
            service().store.set_writeback(review_id, error=str(exc))
        from ..review_learning.service import schedule
        schedule(service().store.root.parent)
        return service().store.get(review_id)
    except KeyError as exc:
        raise HTTPException(404, "没有这条发色复核") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{review_id}/retry", status_code=202)
def retry(review_id: str):
    try:
        return service().retry(review_id)
    except KeyError as exc:
        raise HTTPException(404, "没有这条发色复核") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{review_id}/roles")
def roles(review_id: str):
    get_review(review_id)
    return service().store.children(review_id)


@router.post("/{review_id}/merge-roles")
def merge_roles(review_id: str):
    children = roles(review_id)
    if not children or any(r["status"] not in ("accepted", "decided", "skipped") for r in children):
        raise HTTPException(400, "请先完成每个角色的复核和确认")
    colors = list(dict.fromkeys(c for r in children for c in r["selected"]))
    return _decide(review_id, HairDecisionRequest(colors=colors, no_hair=not colors), multiple_people=len(children)>1,
                   confirmed_roles=[{"id": r["id"], "crop": r.get("crop"), "selected": r["selected"]} for r in children])


@router.post("/{review_id}/crop", status_code=202)
def recrop(review_id: str, request: dict):
    try:
        box = request.get("crop")
        if not isinstance(box, list) or len(box) != 4 or any(type(n) is not int for n in box):
            raise ValueError("请在图片上框选角色头部")
        return service().recrop(review_id, box)
    except KeyError as exc:
        raise HTTPException(404, "没有这条发色复核") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
