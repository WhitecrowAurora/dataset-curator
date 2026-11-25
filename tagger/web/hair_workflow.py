"""Web 任务桥接发色领域服务，模型执行与产物更新仍由内部模块承担。"""
from __future__ import annotations

from contextlib import contextmanager
from threading import Lock
import time


@contextmanager
def suspend_main(job):
    """临时让出本任务所用的托管服务，结束后恢复原配置。"""
    from . import app as app_module
    main = getattr(app_module, "VLLM", None)
    snapshot = None
    def pause():
        nonlocal snapshot
        if main is not None and main.status == "ready" and (
                getattr(job.spec, "engine", "") == "local" or main.url.rstrip("/") == job.spec.url.rstrip("/")):
            snapshot = main.snapshot()
            main.stop()
    job.pause_main_service = pause
    try:
        yield
    finally:
        del job.pause_main_service
        if snapshot is not None:
            job.push("log", message="恢复任务前的本机描述服务")
            try:
                main.start(port=snapshot["port"], **snapshot["overrides"])
                while main.status == "starting":
                    time.sleep(.2)
                if main.status != "ready":
                    raise RuntimeError(main.error or "服务未就绪")
            except Exception as exc:
                job.push("log", message=f"标注产物已保存，原描述服务恢复失败：{exc}")


def batch_for(job):
    from ..hair_color.batch import HairBatch
    from .conflict_hair import service
    return HairBatch(service(), job.id, job.spec.hair_media)


def attach_backend(backend, job):
    backend.hair_batch = batch_for(job)
    if hasattr(backend, "enable_hair_scores"):
        backend.enable_hair_scores()


def finish_local(backend, job):
    if backend is None or not hasattr(backend, "hair_batch"):
        return
    # 主打标全部结算后让出显存，复核 VLM 才加载。
    from ..hair_color.pixai_runtime import release_backend
    release_backend(backend)
    getattr(job, "pause_main_service", lambda: None)()
    job.phase = "hair"
    job.push("phase", phase="hair", message="复核发色，分歧保留供人工确认")
    counts = backend.hair_batch.finish(job.stop_flag.is_set)
    job.push("log", message=f"发色复核完成：{counts}；人工确认入口在「冲突与决策」")


def finish_client(batch, job):
    getattr(job, "pause_main_service", lambda: None)()
    job.phase = "hair"
    job.push("phase", phase="hair", message="复核发色，分歧保留供人工确认")
    def stopped():
        while job.pause_flag.is_set() and not job.stop_flag.is_set():
            time.sleep(.2)
        return job.stop_flag.is_set()
    counts = batch.finish(stopped, out_dir=job.out_dir)
    job.push("log", message=f"发色复核完成：{counts}；人工确认入口在「冲突与决策」")


def run_caption_stages(args, job, run, hook):
    """标签阶段结束后启动描述模型，避免 GPU 分类器与 VLM 同时驻留。"""
    from copy import copy
    from functools import partial
    from ..client import ClientStats, _prov_base
    from ..inference.media_route import MediaRouter
    from .conflict_hair import service as hair_service
    svc = hair_service()
    settings = svc.settings.model_copy()
    getattr(job, "pause_main_service", lambda: None)()
    if args.mode == "both":
        tag_args = copy(args)
        tag_args.mode = "tag"
        tag_args.workflow_part = "tag"
        job.push("phase", phase="tag", message="先生成标签，结束后释放 GPU 分类器")
        job.phase = "tag"
        code = run(tag_args, hook)
        if code or job.stop_flag.is_set():
            return code
    caption_args = copy(args)
    caption_args.mode = "nl"
    caption_args.workflow_part = "nl"
    caption_args.tag_backend = "none"
    caption_args.concurrency = 1
    caption_args.urls = []
    caption_args.api_provider = ""
    caption_args.api_base = ""
    caption_args.api_key = ""
    caption_args.escalate_url = ""
    caption_args.escalate_model = ""
    caption_args.use_file_url = False
    mixed = job.spec.hair_media == "unknown"
    router = MediaRouter(svc.root / "models/media", svc.store.root / "media-routes.sqlite",
                         lambda message: job.push("log", message=message)) if mixed else None
    stats = ClientStats()
    try:
        codes = []
        kinds = ("torii", "qwen") if mixed else (("torii",) if job.spec.hair_media == "anime" else ("qwen",))
        for kind in kinds:
            if job.stop_flag.is_set():
                break
            lane = copy(caption_args)
            if kind == "torii" and settings.torii_engine == "native" and not settings.torii_url:
                lane.concurrency = max(1, int(args.concurrency or 2))
            lane.model_name = getattr(settings, kind + "_model")
            lane.model_id = lane.model_name
            lane.caption_format = "torii" if kind == "torii" else ""
            lane.url = "http://127.0.0.1"
            lane.runtime_fingerprint = {"provider": "local", "model_id": lane.model_name, "engine": "pending"}
            if router:
                lane.item_selector = partial(router.select, kind)
                lane.workflow_stats = stats
            if kind == "torii":
                lane.prompt_text = lane.prompt_zh_text = lane.system_text = ""
                lane.max_tokens, lane.max_pixels = 2048, 1_000_000
                lane.temperature, lane.seed, lane.caption_zh_ratio = .5, 20261119, 0
            started = False
            start_lock = Lock()
            def prepare(client):
                nonlocal started
                with start_lock:
                    if started:
                        return
                    url, model = svc.runtime.endpoint(kind, settings, slots=lane.concurrency)
                    if kind == "torii" and settings.torii_engine == "native" and not settings.torii_url:
                        from ..inference.qwen35.runtime import MAX_BATCH_SIZE
                        job.push("log", message=f"Torii native：{lane.concurrency} 个在途请求，"
                                 f"最多 {svc.runtime.fingerprint.get('slots', min(MAX_BATCH_SIZE, lane.concurrency))} 张连续处理")
                    client.base_url, client.model_name = url.rstrip("/"), model
                    client.pixel_cap = int(svc.runtime.fingerprint.get("max_pixels") or 0)
                    client.prov_base.update(_prov_base(
                        svc.runtime.fingerprint, model_id=model,
                        max_pixels=int(lane.max_pixels or client.prov_base.get("max_pixels") or client.pixel_cap),
                        tier=client.prov_base.get("tier", ""),
                        caption_chars=client.prov_base.get("caption_chars")))
                    client.cache_prefix = client._cache_fingerprint() if client.cache is not None else ""
                    started = True
            lane.before_process = prepare
            lane.workflow_last = kind == kinds[-1]
            stats.input_complete = False
            job.push("phase", phase="nl", message=f"按图片类型生成描述：{lane.model_name}")
            job.phase = "nl"
            codes.append(run(lane, hook))
            svc.runtime.stop()
        if svc.store.job_ids(job.id):
            from ..hair_color.artifact_update import update_artifacts
            with svc.writeback_lock:
                update_artifacts(job.out_dir, svc.store, job.id)
        return max(codes, default=0)
    finally:
        svc.runtime.stop()
        if router:
            router.close()
