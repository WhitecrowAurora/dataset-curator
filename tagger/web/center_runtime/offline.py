"""导入清单、离线执行与恢复联网后的补传。"""
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

from ...storage.atomic import write_bytes
from ... import fox_worker as worker
from .config import execution_config
from .inference import InferenceRuntime


class OfflineTasks:
    def __init__(self,center):
        self.center=center
        self.root=center.dir/"offline"
        self.lock=threading.RLock()
        self.running=False
        self.cancel=threading.Event()
        self.state={"status":"idle","message":"","error":"","tasks":[],"id":""}

    def snapshot(self):
        return {**self.state,"running":self.running}

    def load(self,path):
        with self.lock:
            if self.running or self.center.runtime.active or self.center.running:raise ValueError("请先停止当前执行")
            source=Path(path).expanduser()
            if not source.is_file() or source.stat().st_size>2*1024*1024:raise ValueError("请选择不超过 2 MiB 的离线 JSON 清单")
            data=json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(data,dict) or data.get("format")!="lulynx-offline-task" or data.get("version")!=1:raise ValueError("不是有效的 lulynx 离线任务清单")
            job=data.get("id","")
            if not isinstance(job,str) or len(job)!=17 or job[0]!="O" or any(ch not in "0123456789abcdef" for ch in job[1:]):raise ValueError("任务 ID 不正确")
            if not isinstance(data.get("server"),str):raise ValueError("回传地址不正确")
            url=urlsplit(data["server"])
            if url.scheme not in ("http","https") or not url.hostname or url.username or url.password:raise ValueError("回传地址不正确")
            identity=data.get("identity")
            if not isinstance(identity,dict) or any(not isinstance(identity.get(k),str) or not identity[k] for k in ("user_id","device_id","secret")):raise ValueError("离线任务缺少分配身份")
            if not isinstance(data.get("config",{}),dict):raise ValueError("执行配置不正确")
            config=execution_config(data.get("config",{}))
            data["config"] = config
            tasks=data.get("tasks")
            if not isinstance(tasks,list) or not 0<len(tasks)<=500:raise ValueError("清单必须包含 1～500 个任务")
            seen=set()
            for task in tasks:
                if not isinstance(task,dict):raise ValueError("分包格式不正确")
                pkg=task.get("package",""); repo=task.get("repo",""); lid=task.get("lease_id","")
                if (not isinstance(pkg,str) or not pkg or "\\" in pkg or any(x in ("", ".", "..") for x in pkg.split("/"))
                        or not isinstance(repo,str) or len(repo.split("/"))!=2 or any(x in ("",".","..") for x in repo.split("/"))
                        or not isinstance(lid,str) or not lid or lid in seen):raise ValueError("分包路径或租约重复／不正确")
                from huggingface_hub.utils import validate_repo_id
                validate_repo_id(repo)
                if any(ord(ch)<32 or ch in ':*?"<>|' for ch in pkg) or not lid.startswith("L") or any(ch not in "0123456789abcdef" for ch in lid[1:]):
                    raise ValueError("分包文件名或租约不正确")
                if task.get("repo_type", "dataset") not in ("dataset", "model"):
                    raise ValueError("仓库类型不正确")
                task.setdefault("repo_type", "dataset")
                if task.get("size") is not None and (type(task["size"]) is not int or task["size"]<0):raise ValueError("分片大小不正确")
                sha = task.get("sha256")
                if sha and (not isinstance(sha,str) or len(sha)!=64 or any(ch not in "0123456789abcdef" for ch in sha)):
                    raise ValueError("分片哈希不正确")
                seen.add(lid)
                parts=task.get("missing_parts")
                if not isinstance(parts,list) or not parts or any(p not in ("tag","nl") for p in parts):raise ValueError("分包缺项不正确")
            directory=self.root/job;directory.mkdir(parents=True,exist_ok=True)
            target=directory/"task.json"
            write_bytes(target,json.dumps(data,ensure_ascii=False).encode("utf-8"))
            target.chmod(0o600)
            self.manifest=data
            self.path=target
            self.state={"status":"ready","message":"已导入，分片可从仓库自动下载；也可放入素材目录复用","error":"","tasks":[{"repo":t["repo"],"package":t["package"]} for t in tasks],"id":job,"config":config,
                        "out_dir":str(source.parent) if (source.parent/(job+"-results.json")).is_file() else ""}
            return self.snapshot()

    def configure(self, changes):
        with self.lock:
            if self.running:raise ValueError("请先停止离线任务")
            if not getattr(self,"manifest",None):raise ValueError("请先导入离线清单")
            cfg=execution_config({**self.manifest["config"], **changes})
            if cfg["mode"] != self.manifest["config"]["mode"]:raise ValueError("请保留管理员分配的打标模式")
            updated={**self.manifest,"config":cfg}
            write_bytes(self.path,json.dumps(updated,ensure_ascii=False).encode("utf-8"))
            self.manifest=updated;self.state["config"]=cfg
            return self.snapshot()

    def start(self,shard_dir,out_dir,upload=False):
        with self.lock:
            if self.running or self.center.runtime.active or self.center.running:raise ValueError("请先停止当前执行")
            if not getattr(self,"manifest",None):raise ValueError("请先导入离线清单")
            if not isinstance(out_dir,str) or not out_dir.strip():raise ValueError("请选择产物目录")
            if not isinstance(shard_dir,str) or (not upload and not shard_dir.strip()):raise ValueError("请选择分片目录")
            self.running=True;self.cancel=threading.Event()
            self.state.update(status="uploading" if upload else "preparing",error="",out_dir=out_dir)
            threading.Thread(target=self._run,args=(Path(shard_dir).expanduser(),Path(out_dir).expanduser(),upload),name="center-offline",daemon=True).start()
            return self.snapshot()

    def stop(self):
        self.cancel.set()
        return self.snapshot()

    def _phase(self,status,message):
        self.state.update(status=status,message=message)
        self.center._push("[offline] "+message)

    def _run(self,shards,out,upload):
        from ...center_delivery import deliver
        runtime=InferenceRuntime(self.center._push,self._phase)
        manifest=self.manifest
        results_path=out/(manifest["id"]+"-results.json")
        try:
            results=json.loads(results_path.read_text(encoding="utf-8")) if results_path.is_file() else {}
            out.mkdir(parents=True,exist_ok=True);shards.mkdir(parents=True,exist_ok=True)
            receipt=out/(manifest["id"]+".json")
            write_bytes(receipt,json.dumps(manifest,ensure_ascii=False).encode("utf-8"));receipt.chmod(0o600)
            cfg={**self.center.config(),**manifest["config"],"hosting_mode":"active"}
            identity={**manifest["identity"],"server":manifest["server"]}
            if not upload:
                cfg=runtime.prepare(cfg,self.center.credentials.effective_token(),self.cancel)
            for task in manifest["tasks"]:
                lid=task["lease_id"]
                if self.cancel.is_set():raise ValueError("已停止；分配与已有产物保留")
                task_out=out/lid;task_out.mkdir(parents=True,exist_ok=True)
                result=results.get(lid,{})
                if upload:
                    if result.get("uploaded"):continue
                    if not result.get("done"):raise ValueError("分包尚未离线打标完成："+task["package"])
                    status=worker.api(identity,"/api/v1/offline-status",{"lease_id":lid},retries=0)["status"]
                    if status=="open":
                        reporter=worker.SkipReporter(identity,task_out,task["package"],lid)
                        reporter._flush()
                        deliver(identity,task,task_out)
                        worker.api(identity,"/api/v1/done",{"lease_id":lid,"parts":{p:"done" for p in task["missing_parts"]},"metrics":result.get("metrics",{})},retries=0)
                    elif status!="done":raise ValueError("离线分配已失效，请联系管理员："+task["package"])
                    result["uploaded"]=True
                else:
                    if result.get("done"):
                        if not list(task_out.glob("artifacts-*.jsonl")):raise ValueError("完成记录对应的产物缺失："+task["package"])
                        continue
                    self._phase("downloading","下载／复用 "+task["package"])
                    env=self.center._env(cfg)
                    from .offline_process import execute
                    metrics=execute(task,shards,task_out,env,self.cancel,self.center._push)
                    result={"done":True,"metrics":metrics}
                results[lid]=result
                write_bytes(results_path,json.dumps(results,ensure_ascii=False).encode("utf-8"))
            self._phase("uploaded" if upload else "completed","全部已回传" if upload else "离线打标完成，可联网后补传")
        except Exception as exc:
            self.state.update(status="failed",error=self.center.credentials.redact(str(exc)).replace(manifest["identity"]["secret"],"***"))
            self.center._push("[offline] "+self.state["error"])
        finally:
            runtime.stop(True)
            self.running=False
