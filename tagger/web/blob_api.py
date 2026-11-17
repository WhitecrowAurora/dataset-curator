"""分块回传的 HTTP 路由。挂在 app.py 的 create_app() 里。

接口：

    GET  /api/blob/have?sha=a,b,c      → {have:[...], bad:[...]}
    POST /api/blob/chunk?sha=<64hex>   → {new:bool, bytes:int}   body 是原始字节
    POST /api/blob/commit              → {name, bytes, sha256, chunks}
    GET  /api/blob/stats               → 仓库占用
    POST /api/blob/gc?dry_run=1        → 孤儿块报告 / 清理
    GET  /api/blob/files               → 已提交成品清单
    GET  /api/blob/auth                → 本机鉴权状态（前端据此决定要不要填令牌）
    POST /api/blob/token               → 设置/清除回传令牌

★为什么块体走**原始 body** 而不是 multipart：multipart 要多解析一层边界、
  多一次内存拷贝，而且 8MB 一块的 multipart 在部分反向代理上有额外限制。
  直接 `await request.stream()` 读进哈希器，一个字节都不用整块驻留。

★鉴权是**条件开启**的，这一点很关键：
  - 没设令牌（默认）→ 只接受回环地址来的请求。同源打开的页面天然能用，
    局域网/公网访问一律 403。这条堵住"部署到公网后谁都能往仓库灌块把盘写满"。
  - 设了令牌 → 任何来源都要带 `Authorization: Bearer <token>`（常量时间比对），
    或者 `?token=` 查询参数（curl / 脚本方便）。本机页面也会带上，不会误伤。
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .blob_gc import BlobJanitor
from .blob_upload import BLOB_UPLOADS, list_uploadable
from .blobs import BLOBS, BlobError
from .uploads import shard_rows

router = APIRouter(prefix="/api/blob", tags=["blob"])

# 令牌文件与仓库同根，避免两处配置漂移（换 TAGGER_BLOB_ROOT 时一起搬走）
TOKEN_NAME = "token"


def _token_path() -> Path:
    return BLOBS.root / TOKEN_NAME


def read_token() -> str:
    """当前令牌。空串 = 没设，走回环限制模式。"""
    env = os.environ.get("TAGGER_BLOB_TOKEN")
    if env is not None:
        return env.strip()
    p = _token_path()
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_token(token: str) -> None:
    p = _token_path()
    if not token:
        try:
            p.unlink()
        except OSError:
            pass
        return
    BLOBS.root.mkdir(parents=True, exist_ok=True)
    p.write_text(token, encoding="utf-8")
    # 只给属主读写：令牌就是这仓库的钥匙
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _is_loopback(host: str) -> bool:
    h = (host or "").strip().strip("[]").lower()
    if h in ("", "localhost"):
        return True
    if h.startswith("::ffff:"):                 # 双栈下 IPv4 会映射成这个前缀
        h = h[7:]
    if h.startswith("127.") or h == "::1":
        return True
    return False


def _presented(request: Request, token_q: str | None,
               auth: str | None) -> str:
    if token_q:
        return token_q
    if auth:
        scheme, _, value = auth.partition(" ")
        if scheme.lower() == "bearer":
            return value.strip()
    return ""


def require_token(request: Request, token: str | None = None,
                  authorization: str | None = None) -> None:
    """鉴权闸门。所有改动仓库的接口都过这里。"""
    want = read_token()
    if not want:
        # 无令牌模式：只放行回环。反向代理会把真实来源放进 X-Forwarded-For，
        # 这里**不看它**——它可被伪造，信了就等于没限制。
        client = request.client.host if request.client else ""
        if not _is_loopback(client):
            raise HTTPException(
                403, "回传仓库未设置令牌，只接受本机访问。"
                     "请先 POST /api/blob/token 设置令牌，客户端带 Bearer 头。")
        return
    got = _presented(request, token, authorization)
    if not got or not hmac.compare_digest(got, want):
        raise HTTPException(401, "回传令牌无效", headers={"WWW-Authenticate": "Bearer"})


class TokenRequest(BaseModel):
    """设置令牌。token 留空 = 清除（回到只允许本机）。"""
    token: str = ""


class _Reader:
    """把 async 生成器包成"读到头返回空字节"的读取器。

    生成器耗尽时抛 StopAsyncIteration，而 put_stream 的循环是判 `if not part`
    退出——两者对不上会在**正常传完**时抛异常，所以在这里收口一次。
    """

    def __init__(self, stream) -> None:
        self._it = stream.__aiter__()

    async def read(self) -> bytes:
        try:
            return await self._it.__anext__()
        except StopAsyncIteration:
            return b""


class CommitRequest(BaseModel):
    """manifest：按顺序列出这一份成品的块。"""
    name: str
    chunks: list[str] = Field(default_factory=list)
    bytes: int = 0
    # 要求哪几侧（both / tag / nl）。空 = 不要求，只查"有没有空行/坏行"。
    # 服务器不知道这片是被派去补 tag 还是补 nl，所以由上传方说清楚。
    need: str = ""
    # 传 true 可跳过校验（补传一个已知有问题的大文件时省时间）
    skip_verify: bool = False


class BlobUploadRequest(BaseModel):
    """页面上"用分块通道回传"的表单。token 只进内存，不落盘不进快照。"""
    dir: str
    files: list[str] = Field(default_factory=list)
    url: str
    token: str = ""
    remote_dir: str = ""


# ---------------------------------------------------------------- 回传任务

@router.get("/upload/list")
def blob_upload_files(path: str = "") -> dict:
    """列目录里可回传的分片。**比 /api/artifacts 多认 .tar/.zip**：
    封包产物是 webdataset 时只有 tar，按 parquet/jsonl 过滤会得到空列表，
    用户会以为"没东西可传"。"""
    if not path.strip():
        return {"path": "", "files": []}
    base = Path(path)
    out = []
    for p in list_uploadable(base):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({"name": p.name, "bytes": st.st_size,
                    "mtime": st.st_mtime, "rows": shard_rows(p)})
    return {"path": str(base), "files": out}


@router.post("/upload/start", status_code=202)
def blob_upload_start(req: BlobUploadRequest) -> dict:
    try:
        job = BLOB_UPLOADS.create(req.dir, req.files, url=req.url,
                                  token=req.token, remote_dir=req.remote_dir)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return job.snapshot()


@router.get("/upload/{job_id}")
def blob_upload_status(job_id: str) -> dict:
    job = BLOB_UPLOADS.get(job_id)
    if job is None:
        raise HTTPException(404, "没有这次回传")
    return job.snapshot()


@router.post("/upload/{job_id}/stop")
def blob_upload_stop(job_id: str) -> dict:
    if not BLOB_UPLOADS.stop(job_id):
        raise HTTPException(409, "这次回传已结束或不存在")
    job = BLOB_UPLOADS.get(job_id)
    return {"ok": True, "snapshot": job.snapshot() if job else None}


@router.get("/auth")
def blob_auth(request: Request) -> dict:
    """本机鉴权状态。前端靠它决定"要不要填令牌"——不泄露令牌本身。"""
    want = read_token()
    client = request.client.host if request.client else ""
    return {
        "required": bool(want),
        "token_hint": f"…{want[-4:]}" if len(want) >= 8 else ("已设置" if want else ""),
        "client": client,
        "loopback": _is_loopback(client),
    }


def require_console(request: Request, token_q: str | None = None,
                    authorization: str | None = None) -> None:
    """改锁的闸门。比 require_token 多一条"只限本机"：

    令牌是好东西，但"能换锁"比"能进门"危险得多——远程只要能发一个请求，
    就能把令牌换成自己的，把合法上传方全踢出去。

    ★无令牌阶段放宽一步：此时没有任何凭据可验，`/token` 是唯一的引导入口。
      若坚持只认回环，反向代理后（request.client 是代理地址）第一次设令牌
      就永远做不成。放宽的影响仅限于"设锁之前"，设完立刻收紧。
    """
    client = request.client.host if request.client else ""
    if _is_loopback(client):
        return
    old = read_token()
    if not old:
        return                        # 引导阶段：还没锁，允许设
    got = _presented(request, token_q, authorization)
    if not got or not hmac.compare_digest(got, old):
        raise HTTPException(403, "只能在本机修改回传令牌", headers={"WWW-Authenticate": "Bearer"})


@router.post("/token")
def blob_set_token(req: TokenRequest, request: Request,
                   token: str | None = None,
                   authorization: str | None = Header(default=None)) -> dict:
    """设置/清除令牌。已有令牌时**必须带上旧令牌**，防同机其他进程顺手换锁。"""
    require_console(request, token, authorization)
    old = read_token()
    if old:
        got = _presented(request, token, authorization)
        if not got or not hmac.compare_digest(got, old):
            raise HTTPException(401, "需要旧令牌才能修改", headers={"WWW-Authenticate": "Bearer"})
    new = (req.token or "").strip()
    if new and len(new) < 16:
        raise HTTPException(400, "令牌至少 16 个字符")
    write_token(new)
    return {"ok": True, "required": bool(new)}


@router.post("/token/generate")
def blob_generate_token(request: Request, token: str | None = None,
                        authorization: str | None = Header(default=None)) -> dict:
    """一键生成强令牌并生效。返回明文**仅此一次**（之后只能看到尾部提示）。"""
    require_console(request, token, authorization)
    new = secrets.token_urlsafe(32)
    write_token(new)
    return {"ok": True, "token": new, "required": True}


@router.get("/have")
def blob_have(request: Request, sha: str = "", token: str | None = None,
              authorization: str | None = Header(default=None)) -> dict:
    """告诉客户端哪些块已经有了。逗号分隔；坏名字单列，不拖垮整次询问。"""
    require_token(request, token, authorization)
    wanted = [s.strip() for s in sha.split(",") if s.strip()]
    if not wanted:
        return {"have": [], "bad": []}
    have, bad = BLOBS.have(wanted)
    return {"have": have, "bad": bad}


@router.post("/chunk")
async def blob_chunk(request: Request, sha: str,
                     token: str | None = None,
                     authorization: str | None = Header(default=None)) -> dict:
    """收一块。已有同名块直接返回 new=false（秒传），请求体照样读干净。"""
    require_token(request, token, authorization)
    try:
        new, total = await BLOBS.put_stream(sha, _Reader(request.stream()).read)
    except BlobError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"new": new, "bytes": total}


@router.post("/commit", status_code=201)
def blob_commit(req: CommitRequest, request: Request, token: str | None = None,
                authorization: str | None = Header(default=None)) -> dict:
    """拼块成文件，**然后立刻校验**并把报告写到成品旁边。

    ★为什么校验放在提交这一侧、而不是事后异步扫：此刻"这次传的是哪个分片、
    期望它有什么"还是已知的——传完就判，结果能直接回给上传方，页面上一眼
    看到红绿。事后扫只能看到"文件长什么样"，回答不了"该有的都有了没有"。

    ★校验失败**不回滚提交**：块已经齐了、文件确实拼出来了，这是客观事实；
    把它撤掉只会让人重传一遍同样有问题的数据。失败信息写在报告的 `ok:false`
    与 `problems` 里，由页面呈现。
    """
    require_token(request, token, authorization)
    try:
        done = BLOBS.commit(req.name, req.chunks, total_bytes=int(req.bytes or 0))
    except BlobError as exc:
        raise HTTPException(400, str(exc)) from exc

    report = None
    if not req.skip_verify:
        try:
            report = verify_committed(done, req.need)
            done["verify"] = report
        except Exception as exc:        # noqa: BLE001 - 校验坏了不能反噬提交
            done["verify"] = {"ok": False, "problems": [
                f"校验没能跑完：{type(exc).__name__}: {exc}"]}
    return done


def verify_committed(done: dict, need: str = "") -> dict:
    """校验一份刚提交的成品。

    只校验**标注分片**（artifacts-*）：成品包是 tar/zip，里面是图片，没有
    "每行一张图"的结构，用同一套行判定去扫只会报一堆假问题。
    """
    from .shard_scan import verify_shard, write_report

    name = str(done.get("name") or "")
    path = BLOBS.files / name
    rep = verify_shard(path, need=(need or "").strip().lower())
    # 报告落在成品旁：`artifacts-xxx.jsonl.verify.json`。用后缀而不是
    # 同名改扩展名，是为了让 `files/` 里"一个成品 + 一个报告"并排可见。
    write_report(rep, path.with_name(path.name + ".verify.json"))
    return rep


@router.get("/stats")
def blob_stats(request: Request, token: str | None = None,
               authorization: str | None = Header(default=None)) -> dict:
    require_token(request, token, authorization)
    # 顺带把清扫状态塞进来：盘在涨的时候，第一个想知道的是"定时清有没有在跑"。
    # janitor 绑的是 BLOBS 那个实例，测试替换 BLOBS 时这里要跟着换成同一个对象。
    try:
        from .blob_gc import JANITOR

        jan = (JANITOR.snapshot() if JANITOR.store is BLOBS
               else BlobJanitor(BLOBS).snapshot())
    except Exception:                   # noqa: BLE001 - 状态查不到不该让 stats 挂
        jan = {}
    return {**BLOBS.stats(), "janitor": jan}


@router.post("/gc")
def blob_gc(request: Request, dry_run: bool = True, linger_hours: float = 24.0,
            token: str | None = None,
            authorization: str | None = Header(default=None)) -> dict:
    """清孤儿块。**默认 dry_run**：这是不可逆操作，必须先看报告再动。"""
    require_token(request, token, authorization)
    return BLOBS.gc(dry_run=dry_run, linger_hours=linger_hours)


@router.post("/gc/run")
def blob_gc_run_now(request: Request,
                    token: str | None = None,
                    authorization: str | None = Header(default=None)) -> dict:
    """立刻按定时任务的口径清一次（真删）。给"盘快满了，不想等下一轮"用。"""
    require_token(request, token, authorization)
    from .blob_gc import JANITOR

    jan = JANITOR if JANITOR.store is BLOBS else BlobJanitor(BLOBS)
    return jan.run_once()


@router.get("/files")
def blob_files(request: Request, token: str | None = None,
               authorization: str | None = Header(default=None)) -> dict:
    """已提交的成品清单。给对账/排障用，不参与上传流程。"""
    require_token(request, token, authorization)
    root: Path = BLOBS.files
    out = []
    if root.is_dir():
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.name.endswith((".part", ".chunks",
                                                   ".verify.json")):
                out.append({"name": str(p.relative_to(root)).replace("\\", "/"),
                            "bytes": p.stat().st_size, "mtime": p.stat().st_mtime})
    return {"root": str(root), "files": out}


# ---------------------------------------------------------------- 分片统计

@router.get("/manifests")
def blob_manifests(request: Request, token: str | None = None,
                   authorization: str | None = Header(default=None)) -> dict:
    """仓库里每个标注分片的统计 + 批次汇总。

    "标注分片"= `files/` 下已提交的 artifacts-*。**不含成品包**（tar/zip 里
    是图片，没有"每行一张图"的结构）。`summary` 是顶部卡片要的数。
    """
    require_token(request, token, authorization)
    from .shard_scan import list_shards, scan_shard

    items = []
    total = dict(rows=0, tagged=0, only_tag=0, only_nl=0, both=0,
                 excluded=0, empty=0, bytes=0, kinds={})
    for p in list_shards(BLOBS.files):
        rep = scan_shard(p)
        d = rep.as_dict()
        d.pop("image_ids", None)          # 列表页不需要 id 集合
        rep_path = p.with_name(p.name + ".verify.json")
        d["verified"] = _read_report(rep_path)
        items.append(d)
        for k in ("rows", "tagged", "only_tag", "only_nl", "both",
                  "excluded", "empty", "bytes"):
            total[k] += d[k]
        for k, v in d["excluded_kinds"].items():
            total["kinds"][k] = total["kinds"].get(k, 0) + v
    total["shards"] = len(items)
    return {"root": str(BLOBS.files), "shards": items, "summary": total}


def _read_report(path) -> dict | None:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return None


@router.get("/manifests/verify")
def blob_manifest_verify(request: Request, name: str, need: str = "",
                         token: str | None = None,
                         authorization: str | None = Header(default=None)) -> dict:
    """重跑一次校验（同步）。用于"上传时跳过了校验"或想拿最新结论。"""
    require_token(request, token, authorization)
    from .shard_scan import verify_shard, write_report

    path = (BLOBS.files / name).resolve()
    if not str(path).startswith(str(BLOBS.files.resolve())):
        raise HTTPException(400, "路径越界")
    if not path.is_file():
        raise HTTPException(404, "没有这份成品")
    rep = verify_shard(path, need=(need or "").strip().lower())
    write_report(rep, path.with_name(path.name + ".verify.json"))
    return rep
