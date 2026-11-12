# Tagger 打标器 · AI 接入与使用手册

> 本文读者是 **AI 助手**（以及替它做配置的人）。人类上手看 `README-FIRST.txt`，开发细节看 `devtools/HANDOFF.md`。

## 1. 先对齐结论

Tagger 是给扩散模型训练数据做 **tag 标注 / NL 自然语言描述 / 分包封包** 的本地流水线。它已经有完整的 CLI 和 Web UI；`mcp/tagger_mcp.py` 只是一个 **MCP 薄封装**，把现有的 Web API 暴露成工具，**不实现任何打标或打包逻辑**。

```
AI 客户端 ──stdio MCP──> mcp/tagger_mcp.py ──HTTP──> 127.0.0.1:27100 (tagger/web/app.py)
                                                        └── 真正的任务、模型、产物状态都在这里
```

三条最重要的判断：

1. **必须先有 Web 服务在跑**。MCP server 只是客户端，服务没起时所有工具都会返回启动指引。
2. **任务状态只在 Web 进程内存里**。不要绕开 API 直接跑 CLI 去"查进度"，读到的会是假状态。
3. **服务只监听本机、无鉴权、单 worker**。默认 `http://127.0.0.1:27100`，同一时刻只能有一个打标任务在跑。

## 2. 一次性前置条件

| 步骤 | 做法 |
|---|---|
| 装环境 | 解压后跑一次 `install.bat`（Linux/macOS 用 `bash install.sh`，脚本开头会自己补齐三个 `.sh` 的执行位）。跳过这步，服务起不来。 |
| 起服务 | 双击 `Tagger.Shell.exe`（推荐，关窗口即停）；或 `run-web.cmd` / `run-web.sh`；或让 AI 调 `tagger_web_start`。 |
| 下权重 | 界面「模型管理」下载 NL 模型权重（几 GB），或 `tagger_models` 看 `ready` 状态。报「GGUF 权重不存在」= 缺权重，不是缺依赖。 |
| Python | 用包内的 `env/python/python.exe`（Linux `env/linux/bin/python`）跑 MCP server。它是零第三方依赖的，任何 Python 3.10+ 都能跑，**不需要为它改 `install.bat` 或再装包**。 |

服务端口：Web UI `27100`，本机 NL 推理服务 `8000`（多卡基端口 `8010`），前端 dev `3010`。

## 3. 接入配置

### 3.1 通用片段（MCP 生态的 `mcpServers` 格式）

把下面三处路径换成解压后的实际路径。Windows 用正斜杠即可，路径带空格/中文没问题。

```json
{
  "mcpServers": {
    "tagger": {
      "command": "H:/tagger/env/python/python.exe",
      "args": ["H:/tagger/mcp/tagger_mcp.py"],
      "env": {
        "TAGGER_MCP_ROOT": "H:/tagger",
        "TAGGER_MCP_URL": "http://127.0.0.1:27100"
      }
    }
  }
}
```

| 客户端 | 配置位置 |
|---|---|
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` |
| Cursor | `%USERPROFILE%\.cursor\mcp.json`（全局）或项目内 `.cursor\mcp.json` |
| 其它支持 MCP 的客户端（含 CodeBuddy、Cline、Continue 等） | 在该客户端的 **MCP / 工具设置** 里新增一个 stdio 服务器，`command` / `args` / `env` 三项照上面填。具体入口以你所用版本为准。 |

若客户端只自动读取 `AGENTS.md` 而不读本文件，把 `agent.md` 复制一份为 `AGENTS.md` 即可，内容不用改。

Linux/macOS 把 `command` 换成 `<解压目录>/env/linux/bin/python`（或 `python3`），`args` 与 `TAGGER_MCP_ROOT` 换成实际路径。

### 3.2 可调环境变量

| 变量 | 默认 | 用途 |
|---|---|---|
| `TAGGER_MCP_URL` | `http://127.0.0.1:27100` | Web 服务地址。别指向公网：服务无鉴权。 |
| `TAGGER_MCP_ROOT` | 脚本的上一级目录 | 仓库根。`tagger_web_start` 用它当 cwd。 |
| `TAGGER_MCP_TIMEOUT` | `30` | 单次 HTTP 超时（秒）。 |
| `TAGGER_MCP_DEBUG` | 未设置 | 设 `1` 才把日志写 stderr。**默认 stderr 全静默**，避免被客户端并进 stdout 打断协议流。 |

### 3.3 验证接上了

```powershell
& H:/tagger/env/python/python.exe H:/tagger/mcp/tagger_mcp.py --selftest
```

应输出 `5/5 通过`（自检不依赖 Web 服务）。然后在客户端里让 AI 调 `tagger_health`，能看到 `python` / `platform` / `gpus` / `ui_built` / `running_job` 就通了。

分发包说明：`pack-dist.bat` 已把 `mcp\` 与 `agent.md` 一起打进 zip，收包的人不用额外下载。

## 4. 工具清单（15 个）

| 工具 | 干什么 | 对应 API |
|---|---|---|
| `tagger_health` | 探活 + 环境/GPU/前端状态 + 当前任务 | `GET /api/health` |
| `tagger_web_start` | 把 Web 服务拉成独立后台进程并等就绪 | `tagger web --no-open` |
| `tagger_defaults` | 服务端默认参数（分档、质检、保护、封包） | `GET /api/defaults` |
| `tagger_job_form` | 上次表单记忆 `form` 与上次提交的 `spec` | `GET /api/job-form` |
| `tagger_browse` | 浏览目录选素材（子目录/图片数/zip-tar 包/盘符） | `GET /api/fs` |
| `tagger_preview` | 预演发现张数、命名样例、画师覆盖、预计盘位 | `GET /api/preview` |
| `tagger_models` | 模型清单与权重就绪/下载进度 | `GET /api/models` |
| `tagger_service` | NL 推理服务：`status/report/capability/plan/start/stop/probe/llama/multi/deps` | `/api/vllm*`、`/api/llama`… |
| `tagger_jobs` | 任务列表（内存活任务 + 盘上历史） | `GET /api/jobs` |
| `tagger_job_start` | **新建并立即开跑打标任务** | `POST /api/jobs` |
| `tagger_job` | 单任务 `status/events/results/stats/stop/pause/resume/restart/resume_state` | `/api/jobs/{id}*` |
| `tagger_artifacts` | 列可回传产物分片与每片行数 | `GET /api/artifacts` |
| `tagger_datajob` | 分包 split / 拼接 merge / 封包 pack：`preview/start/list/status/stop` | `/api/datajob*` |
| `tagger_endpoints` | 从 `/openapi.json` 列出全部路由，可按关键字过滤 | `GET /openapi.json` |
| `tagger_api` | 通用逃生口：任意 `/api/*` 或 `/openapi.json` | 任意 |

所有工具都返回带 `ok` / `status` / `data` 的结构化 JSON；失败时附 `hint`，不要忽略它。

## 5. 标准工作流

```
tagger_health ──不通──> tagger_web_start（或让用户双击 Tagger.Shell.exe）
      │ 通
      ▼
tagger_defaults + tagger_job_form      ← 拿真实参数口径，别自己编数值
      ▼
tagger_browse → tagger_preview          ← 确认素材目录、张数、命名、盘位
      ▼
tagger_models + tagger_service(status)  ← 确认权重与 NL 推理服务就绪
      ▼
tagger_job_start(spec)                  ← 【写操作】必须先向用户确认参数
      ▼
tagger_job(events/status) 轮询          ← 202 不是"完成"，用 after 游标增量拉
      ▼
tagger_job(results/stats) + tagger_artifacts
      ▼
tagger_datajob(kind=pack) 或 tagger_api 做校验/回收
```

1. **探活**：先 `tagger_health`。服务没起就 `tagger_web_start`；它返回 `pid` 与日志路径 `work/mcp-web.log`，这个进程是常驻的，不会随会话退出。
2. **摸参数**：`tagger_defaults` 给默认值，`tagger_job_form` 的 `spec` 是上次成功跑过的任务，是最好的模板——取出来改几项再提交。
3. **确认素材**：`tagger_browse(path)` 找目录，`tagger_preview(path=..., pack=...)` 看张数、命名样例与预计盘位。只打标的任务传 `pack=false`（命名口径不同）。这一步只读，几百万张的批次务必先做。
4. **确认模型/服务**：`tagger_models` 看 `ready`；需要 NL 时 `tagger_service(action="plan", options={...})` 先看分档与 `warnings`，再 `action="start"`，然后 `action="status"` 轮询到就绪。
5. **建任务**：`tagger_job_start(spec)`。**这是会真的花几小时 GPU 的写操作，先把完整 `spec` 念给用户确认**。返回 `202` 只代表开跑了。
6. **轮询**：`tagger_job(job_id, action="events", after=<上次最大 seq>)` 增量拉事件，同时看返回的 `snapshot`（`status` / `phase` / `progress` / `counts`）。不要每次重建任务，也不要每秒狂刷。
7. **看结果**：`action="results"`（支持 `offset/limit/q/only_failed`，`limit` 上限 200）、`action="stats"`（`bucket` 必须是 100 的倍数）。
8. **产物与收尾**：`tagger_artifacts(path)` 看分片行数；`tagger_datajob(action="start", kind="pack", params={...})` 封包；校验/回收用 `tagger_api`（见 §8）。

## 6. `tagger_job_start` 的 spec

### 6.1 四种常用配方

**A. 只打 tag（本地分类头，不吃 VLM）**

```json
{"input_dir": "D:/data/batch1", "out_dir": "D:/data/batch1-tags",
 "engine": "local", "backend": "wd-eva02"}
```

**B. 本机 VLM 出 NL + tag（需先起推理服务）**

```json
{"input_dir": "D:/data/batch1", "out_dir": "D:/data/batch1-out",
 "engine": "vlm", "vlm_provider": "local", "url": "http://127.0.0.1:8000",
 "vlm_model": "tagger", "output_mode": "both", "concurrency": 2}
```

**C. 外部 API 出 NL（不加载本地权重）**

```json
{"input_dir": "D:/data/batch1", "out_dir": "D:/data/batch1-out",
 "engine": "vlm", "vlm_provider": "openai", "api_base": "https://api.openai.com/v1",
 "api_key": "<密钥>", "vlm_model": "gpt-4o", "output_mode": "nl"}
```

**D. 打标 + 直接封包一条龙**

在 A/B/C 任一份上追加：

```json
{"pack": true, "formats": ["parquet"], "layout": "flat"}
```

### 6.2 字段表（`POST /api/jobs` 的 `JobRequest`）

没给的字段一律用服务端默认值，**不要自己编数值**。

| 组 | 字段（括号为默认值 / 取值） |
|---|---|
| 输入 | `input_dir`（本地引擎必填；HF 输入可空）、`input_shards`[]、`out_dir`、`recursive`(true)、`include_zips`(true)、`include_loose`(true)、`limit`(0)、`force`(false) |
| 引擎 | `engine` = `local`（本地 tag 后端）/ `vlm`（VLM 出 NL） |
| tag 后端 | `backend` = `wd-eva02` / `pixai` / `cl-tagger-v2` / `onnx`、`model_dir`、`device`、`precision`、`batch`(4)、`preprocess` = `pil`/`tensor`、`threshold`、`keep_categories`[]、`thresholds`{}、`model_repo`、`guard`(true)、`guard_policy`{}、`check_space`(true) |
| VLM | `vlm_provider` = `local`/`openai`/`anthropic`、`api_base`、`api_key`、`url`(`http://127.0.0.1:8000`)、`vlm_model`(`tagger`)、`output_mode` = `both`/`nl`/`tag`、`temperature`(0.2)、`top_p`(0.9)、`max_tokens`(0)、`caption_chars`[lo,hi]、`caption_zh_ratio`(0.3)、`prompt_id`、`timeout_s`(90)、`concurrency`(2)、`multi_gpu`(false)、`multi_gpus`[]、`per_card_seqs`(0)、`max_pixels`(0)、`seed`、`max_failures`(3)、`use_file_url`(false)、`escalate_url`、`escalate_model` |
| 质检 | `quality_pass_score`、`quality_drop_score`、`quality_max_attempts`、`quality_retry_temp_step`、`quality_retry_temp_cap`、`quality_retry_pixels_scale`、`quality_retry_pixels_cap`、`quality_enable_clip`、`quality_clip_min_score`、`quality_clip_model`、`quality_clip_pretrained`、`quality_min_chars`、`quality_max_chars`、`quality_min_tag_coverage`、`quality_max_repeat_ratio`（0 / None / 空 = 跟配置） |
| HF 输入 | `hf_repo`、`hf_token`、`hf_revision`(main)、`hf_files`[]、`hf_format` = `auto`/`parquet`/`webdataset`、`hf_prefetch`(1)、`hf_conns`(4)、`hf_proxy`、`hf_reserve_gb` |
| 缓存 / 产物 | `cache`(false)、`cache_max_gb`(8)、`artifact_format`(jsonl)、`parquet_rows`(2000) |
| 命名 | `artist`(true)、`artist_depth`(0)、`artist_inject`(true)、`artist_inject_tags`(true)、`artist_table`、`layout` = `flat`/`mirror`、`seq_width`(6) |
| 封包 | `pack`(false)、`formats`([`parquet`] / `webdataset` / `zip`)、`jpeg_quality`(95)、`jpeg_subsampling`(0=4:4:4)、`reencode` = `always`/`non_jpeg`/`never`、`compress` = `stored`/`deflate`、`max_records`(5000)、`max_bytes`(0) |

> **封包质量与色度采样的团队口径尚未定稿**（质量下限、444 还是 420）。按后端默认值走，别擅自改，改动前先问人。

### 6.3 `tagger_service(action="start")` 的 options（`VllmStartRequest`）

`port`(8000)、`timeout_s`(900)、`engine` = `vllm`/`llamacpp`（Windows 原生只有 llamacpp）、`model`、`tier`、`precision` = `bf16`/`fp8`/`w4a16`、`served_name`、`max_model_len`、`max_num_seqs`、`max_num_batched_tokens`、`gpu_mem`、`local_media_root`、`slots`。留空 = 按分档表推算。改了 `port` 或 `served_name`，任务 `spec` 里的 `url` / `vlm_model` 要一起改。

### 6.4 `tagger_datajob` 的 params

**未知字段会被后端明确拒绝**（与 `/api/jobs` 的静默忽略不同），照抄字段名。

| kind | 字段 |
|---|---|
| `split` | `input_dir`(必填)、`out_dir`、`table`、`mode`=`count`/`bytes`、`per_shard`(20000)、`max_bytes`、`layout`、`seq_width`、`start_seq`(1)、`previous_table`、`recursive`、`exclude`、`limit`、`verify_pack`、`artist_table`、`nested`、`work_dir`、`passwords`、`workers`、`cpu_reserve`(2)、`mem_reserve_gb`(4)、`resize_mode`=`none`/`longest`/`shortest`/`pixels`、`resize_value`(768)、`jpeg_quality`(92)、`scan_table`、`verify_n`(20) |
| `merge` | `roots`(必填，数组)、`shard_dir`、`table`、`tables`[]、`out`、`fmt`=`jsonl`/`json`、`require`=`both`/`nl`/`tag`、`dataset`、`sample_n`(20)、`sample_out`、`keep_empty` |
| `pack` | `input_dir`(必填)、`out_dir`、`formats`、`jpeg_quality`、`jpeg_subsampling`、`reencode`、`compress`、`max_records`、`max_bytes`、`layout`、`seq_width`、`start_seq`、`assign_ids`(false)、`artist`、`artist_depth`、`artist_inject`、`artist_table`、`include_zips`、`include_loose`、`recursive`、`limit`、`check_space` |

`pack` 的 `assign_ids` 必须与打标那次口径一致，否则产物一条都对不上：本机「新建任务」打的标用 `false`，成员按分包表打回来的产物用 `true`。

## 7. 硬约束（违反会出事）

1. **不要猜字段**。`POST /api/jobs` 的请求模型会**静默丢掉拼错的字段**，参数看起来提交了、实际没人读，几小时跑完才发现口径不对。拿不准就 `tagger_endpoints` 查 `/openapi.json`，或对照 §6.2。
2. **同一时刻只允许一个打标任务**，重复创建返回 `409`。建之前先看 `tagger_jobs`。
3. **长任务是 `202` + 轮询，不是调用即完成**。别把 `tagger_job_start` 的返回当成结果，别为了"刷新"反复重建任务。
4. **服务重启后内存态会丢**：历史任务只剩盘上快照，`events` 为空、`stats` 空结构是正常现象，不是数据丢了。产物在 `out_dir` 里。
5. **只读工具随便用；写操作先经用户确认**：`tagger_job_start`、`tagger_service(start)`、`tagger_datajob(start)`、`tagger_api` 里的 POST 下载/上传/安装/登录类端点、`tagger_job` 的 stop/pause/restart。
6. **一律给绝对路径**。后端配置（`configs/tiers.yaml` 等）相对**仓库根**解析；`tagger_web_start` 已经把 cwd 设成 `TAGGER_MCP_ROOT`，命令行手动起服务时要 `cd` 到仓库根。
7. **密钥别外泄**：`api_key` / `hf_token` / FTP 密码不回显（快照里抹成 `***`），不要在回复里复述，也不要写进聊天记录或日志。
8. **不要把服务暴露出去**：`tagger_web_start` 的 `host` 保持 `127.0.0.1`；`TAGGER_MCP_URL` 不要指向公网地址。
9. **先估盘位**：大批次先 `tagger_preview`，`check_space` 默认开着，别等爆盘才发现装不下。
10. **CLI 是给人用的备用通道**：`tagger.cmd <子命令>` 输出中文文本、没有 `--json` 开关、机器可读结果靠 `--json-out` 之类写文件。**不要用 CLI 代替 MCP 去查进度**。
11. AI 接入不需要改前端源码，也不需要 `npm run build`。

## 8. 没封装成专用工具的能力

`tagger_endpoints(filter)` 从 `/openapi.json` 列出路由 → `tagger_api(method, path, query, body)` 直接调。

| 想做什么 | 调用 |
|---|---|
| 看/改 NL 提示词 | `GET /api/prompts`、`GET /api/prompts/one?id=`、`PUT /api/prompts/one?id=` |
| 配置方案预设 | `GET /api/presets`、`POST /api/presets/apply` |
| 去重 | `POST /api/dedup/start` + `GET /api/dedup/{job_id}` |
| FTP 回传 | `POST /api/upload/start`（`dir` / `files` / `ftp{host,port,user,password,remote_dir,passive}`） |
| 冲突裁决 | `POST /api/conflicts/scan`、`GET /api/conflicts`、`POST /api/conflicts/decide` |
| 部署自检 | `POST /api/selfcheck/start`（`input_dir` / `recursive` / `sample` / `url`） |
| HF 分片清单/下载 | `POST /api/hf/files`、`POST /api/downloads/start` |
| 模型权重下载 | `POST /api/models/download`（`{"kind":"vlm","key":"<key>"}`） |
| 排错报告 | `tagger_service(action="report")`，或直接 `GET /api/vllm/report` |
| 调度中心（派工） | `POST /api/center/login`、`POST /api/center/start` |
| 断点续跑 | `tagger_job(action="resume_state")`，再 `POST /api/resume` |

`tagger_api` 只放行 `/api/*` 与 `/openapi.json`，拼错路径会直接被挡下来。

## 9. 故障排查

| 现象 | 原因 / 下一步 |
|---|---|
| `tagger_health` 连不上 | 服务没起。`tagger_web_start`，或让用户双击 `Tagger.Shell.exe` / 跑 `run-web.cmd`；仍不行先跑一次 `install.bat`。日志 `work/mcp-web.log`、`%LOCALAPPDATA%\Tagger\shell.log`。 |
| 「GGUF 权重不存在」 | 缺权重。`tagger_models` 看 `ready` / `missing_files`，走「模型管理」下载或拷 `models/nl/`。**不是缺依赖**。 |
| `422` | 请求体字段校验失败，`tagger_endpoints` 查 `/openapi.json` 的真实 schema。 |
| `400` | 参数非法，对照 `tagger_defaults` / `tagger_job_form` 与 §6 字段表。 |
| `409` | 已有任务或服务在跑 / 状态不允许。`tagger_jobs`、`tagger_service(status)` 看现状，**别重试同一请求**。 |
| `404` | 路径或 `job_id` 不存在。用 `tagger_browse` / `tagger_jobs` 确认。 |
| 任务跑不动、推理服务起不来 | `tagger_service(action="report")` 拿一段可直接粘给 AI 的现场报告；`action="capability"` 看引擎在这台机器上能不能用。 |
| 界面是「前端还没构建」说明页 | 分发包漏了 `tagger/web/ui/dist/index.html`，与服务无关。 |
| 中文日志乱码 | 需要看 MCP 日志时设 `TAGGER_MCP_DEBUG=1` 并让子进程继承 `PYTHONIOENCODING=utf-8`。 |

## 10. 概念与目录速查

| 词 | 含义 |
|---|---|
| `tag` / `nl` / `both` | 标签串 / 自然语言描述 / 两者都要（`output_mode`） |
| `engine=local` / `vlm` | 本地 tag 分类头 / VLM 生成 NL |
| `vlm_provider` | `local` 本机或自建服务、`openai` / `anthropic` 外部 API |
| `tier` | 显存分档表 `configs/tiers.yaml`，决定权重、上下文、并发 |
| 分包 `split` / 封包 `pack` / 拼接 `merge` | 切成离线包 / 打成 parquet·webdataset·zip 成品 / 回收成员产物对账成总表 |
| 分片 `shard` | `*.tar` / `*.parquet` 离线包，一人一份 |
| `image_id` / `layout` | 图片唯一 id / `flat` 拍平命名、`mirror` 保留原目录结构 |
| `prompt` / `preset` | `prompt/*.json` NL 提示词 / `configs/ui-presets.json` 配置方案快照 |

| 目录 | 放什么 |
|---|---|
| `configs/` | `tiers.yaml` 主分档表、`models.json` 模型覆盖、`ui-form.json` 上次表单、`ui-presets.json` |
| `models/nl/<key>/` | GGUF + mmproj 权重；`models/wd-eva02/` 本地 tag 分类头 |
| `prompt/*.json` | NL 提示词（文件名就是下拉名）；`prompts/*.md` 是标注规则文档 |
| `work/` | `dist` 分包、`out` 打标产物、`merged.jsonl` / `sample.md`、`report.json/md`、`center/` |
| `run/` | `resume.json` 断点、`history/` 历史、`fingerprint.json` 环境指纹、结果缓存 |
