# 打标器交接文档

> 最后更新：2026-11-03
> 用途：换会话/换人接手时，先读这一份，不要从零摸索。
> 目标用户：训练团队内部使用，处理服务器上 4TB 级素材包。

---

## 0. 一句话现状

**打标 → 封包这条主链路已经打通**：输入可以是目录或单个 `.zip`，先跑打标产出成员产物，再按
parquet / WebDataset / zip 封包；命名布局、画师前缀与取第几层、画师注入、JPEG 质量/采样、
每包上限、盘位预算都能在 WebUI 里选。默认输出 **parquet**（HF 可直接流式读）。
CLI（`tagger pack`）与 Web API（`POST /api/jobs` 带 `pack: true`）走的是同一份 `pipeline.py`。

还没接的：分发包/回收/复核、Splash 启动页。

**NL 模式已接上**：VLM 引擎在 UI 上可选 `tag / nl / both`；本机 vLLM 服务能在页面里启停、
看日志尾巴、读环境指纹（不必再单开一个终端跑 `tagger serve`），远程服务用「探测」验连通性。

---

## 1. 怎么跑起来

```powershell
# 后端（Windows，仓库自带 Python）
.\env\python\python.exe -m tagger.cli web --port 27100

# 前端开发（改了 tsx 时）
cd tagger\web\ui; npm run dev      # 3010 端口已代理到后端

# 前端构建（部署/壳子用）
cd tagger\web\ui; npm run build

# Windows 桌面壳子
powershell -File desktop\build.ps1        # 发布到 dist\desktop\Tagger.Shell.exe
.\dist\desktop\Tagger.Shell.exe

# Linux/macOS（浏览器模式）
./run-web.sh

# 定并发用的压测（服务起着之后跑，详见 §4.5）
tagger bench --images "work/imgs/*.jpg" --concurrency 1,2,4,8,16 \
             --images-total 30000000 --machines 10
```

测试：

```powershell
.\env\python\python.exe -m pytest tests -q     # 432 条
cd tagger\web\ui; npm run build                # 含 tsc --noEmit
```

---

## 2. 代码地图

### 后端（`tagger/`）

| 文件 | 职责 | 状态 |
|---|---|---|
| `cli.py` | 20 个子命令的总入口（含压测 `bench`） | 完整 |
| `pack.py` | **核心**：输入发现 + 平铺命名 + 转码 + 封包（parquet/webdataset/zip）+ 断点记忆 | 完整 |
| `pipeline.py` | **接线层**：`plan_inputs()` 发现/画师/全局 id → `pack_items()` 读产物封包；zip 句柄池、封包断点记忆 | 完整 |
| `archive.py` | 分包读写，ZIP 中央目录当索引，不解压整包 | 完整 |
| `naming.py` | `flat` / `mirror` 两种布局，全局序号，画师名清洗 | 完整 |
| `artist.py` | 画师名提取、英文别名、注入进 caption | 完整 |
| `local_tag.py` | **本地** wd-eva02 打标（tag + rating，无 NL） | 完整 |
| `cpu_tagger.py` | tag 后端抽象与加载 | 完整 |
| `client.py` | **远端** vLLM 打标（tag / nl / both 三种模式） | 完整 |
| `quality.py` | 标注质量三层评分 + 重试回环（注意：这是**标注质量**，不是 JPEG 质量） | 完整 |
| `artifacts.py` | 成员产物的 parquet 落盘形态 | 完整 |
| `distribute.py` / `merge.py` / `verify.py` / `report.py` | 分发包、回收对账、完整性复核、批次报告 | 完整 |
| `hf_source.py` | 从 HF 拉 tar/parquet 素材 | 完整 |
| `web/app.py` | FastAPI：任务、预览、目录浏览（含 `.zip`）、缩略图（含 zip 成员）、事件流 | 完整 |
| `web/jobs.py` | 任务簿 + 两阶段（打标→封包）+ 协作式中止 + 事件流；VLM 模式映射 `vlm_backend_for` | 完整 |
| `web/vllm_service.py` | 本机 vLLM 启停/状态/日志尾巴/指纹写入（`/api/vllm*`） | 完整 |
| `web/packaging.py` | 分包/拼接/封包的后台任务（`/api/datajob*`） | 完整 |
| `web/models.py` | **模型清单**：`/api/models` 读写 `configs/models.json`（只存与 tiers.yaml 的差异），下拉与 CLI 共用同一份合并结果 | 完整 |
| `web/downloads.py` | **权重下载**：流式下载 + 进度/网速 + 按文件断点续传 + 取消 + 代理（http/socks5）；删除只允许 `models/` 之内 | 完整 |
| `netutil.py` | 回环地址判定：本机请求一律不吃系统代理（代理软件会拦 127.0.0.1） | 完整 |

### 前端（`tagger/web/ui/`）

```
src/
  App.tsx                八页路由（hash）
  layout/AppShell.tsx    侧栏壳 + 顶栏（平台/Python/GPU/主题）
  layout/Sidebar.tsx     导航
  pages/NewJob.tsx       新建（打标方式 / 输入 / VLM 服务 / 命名 / 模型与性能）
  pages/RunJob.tsx       运行中（阶段感知：打标进度 → 封包进度、分片列表、日志）
  pages/History.tsx      历史（接 /api/jobs）
  pages/Results.tsx      结果（缩略图网格 + 详情抽屉）
  pages/Packaging.tsx    数据打包（分包 / 拼接 / 封包）
  pages/Dedup.tsx        去重（独立扫描，不占 GPU）
  pages/Models.tsx       模型管理（NL 模型清单 / tag 分类头清单，写 configs/models.json）
  pages/Upload.tsx       回传（FTP 传 parquet）
  pages/Stats.tsx        统计（通过率 / 质检分趋势）
  components/FolderPicker.tsx
  components/ui/         launcher / controls / ProgressBar / OverviewFigures
  styles/tokens.css      --lx-* 令牌（深色默认 + 浅色覆盖）
  index.css              .lx-* 组件类
  lib/{api,store,format,theme,cn,motion}.ts
```

### 桌面壳子（`desktop/Tagger.Shell/`）

WPF + WebView2，托管 `python -m tagger.cli web` 子进程。
关键文件：`Services/PythonServer.cs`（找仓库/找解释器/选端口/等健康检查）、`MainWindow.xaml.cs`（遮罩、错误面板、退出时杀进程树）。

---

## 3. 用户最新需求 vs 现状（**最重要的一节**）

用户原话要点：需要支持选「4TB 单文件压缩包」或「有子目录的文件夹」，需要能选输出 WebDataset **parquet** 的方式，
需要「保持原结构」vs「画师名-序号.格式」两种平铺命名，需要 JPEG 质量滑块（80-100）与色度采样（444~420，默认 444）。

| 需求 | 状态 | 落在哪 |
|---|---|---|
| 输入是**压缩包**（zip） | ✅ 单个 `.zip` 或目录；目录里的 `.zip` 可展开，也可只看散图 | `pipeline.plan_inputs` / `/api/fs` 列 `zips` |
| 输入是**嵌套子目录** | ✅ 递归开关 | `discover_inputs` |
| 输出 **parquet** | ✅ **UI 默认格式**（HF 可直接 `streaming=True` 读），pyarrow 写出 | `pack.py` + `/api/defaults` |
| 输出 **WebDataset(tar)** | ✅ 可多选 | `pack.py` |
| 输出 **zip 分片** | ✅ 可多选，stored/deflate 可选 | `pack.py` |
| **保持原结构** vs **平铺改名** | ✅ flat / mirror，新建页实时预览；mirror 不再加包名前缀 | `naming.py` + `/api/preview` |
| 命名 `画师-000123.jpg` | ✅ 全局序号 + 画师前缀，`seq_width=6` | `pipeline.make_image_id` |
| **画师取第几层目录** | ✅ UI 数字输入（0=第一层），改了就重算预览与命名指纹 | `JobSpec.artist_depth` + `NewJob.tsx` |
| **JPEG 质量滑块** | ✅ 90~100（下限不放开，见 §4.1） | `NewJob.tsx` + `PackOptions` 校验 |
| **色度采样** | ✅ **默认 4:4:4**，可选 4:2:2 / 4:2:0 | `PackOptions.jpeg_subsampling=0` |
| 压缩方式 stored/deflate | ✅ | `PackOptions.compress` |
| 每包条数/字节上限 | ✅ `max_records=5000` / `max_bytes` | `PackOptions` |
| **盘位预算** | ✅ 预计产出 + 20% 冗余；开跑前拒绝、运行中复检（见 §4.3） | `pipeline.estimate_*` + `space_check()` |
| **NL（自然语言）打标** | ✅ VLM 引擎三种产出模式 `tag / nl / both`；服务可在页面启停（见 §4.4） | `client.py` + `web/vllm_service.py` + `NewJob.tsx` |
| 保留哪些 tag 类别 | ⚠️ 后端有 rating，类别筛选仍是隐式的 | 待查清 |
| **画师注入 caption** | ✅ UI 开关：写进 `.tag/.nl` 文本，或只留 JSON 字段 | `pipeline.pack_items(inject_artist=)` + `NewJob.tsx` |
| 多人分发 / 合并 / 复核 | ❌ 还没接 UI | 后端有 |

### 结论

- 主链路是**两阶段 job**：`_run()` 先 `local_tag.tag_items()` 写成员产物，再 `_pack_phase()` 读产物封包；
  `job.status` 只在**封包收尾之后**才落 `done`，轮询方看到 done 时 `.tmp` 已 rename、`packs` 已填好。
- **打标中止就不封包**：半批数据不该进下游。封包中止则已封好的分片保留，靠 `PackState` 续跑。
- 两份断点记忆各管一段，不要混：
  - 打标：配置指纹写进产物，`force` 可绕过；
  - 封包：`.packstate-<参数指纹>.jsonl`，参数（格式/质量/采样/上限/布局）一变就换文件 → 自然重封。
- `pack=False` 的纯打标任务仍用旧的相对路径 id，老产物能命中断点，不受新命名影响。

---

## 4. 已定的决策（2026-11-03 用户确认）

### 4.1 JPEG 质量区间：**维持 90-100**，下限不放开

用户原先说的 "80" 是自己口误（实际意图就是 90），因此 `pack.py` 的 90~100 校验**保持原样**。
UI 滑块范围 90-100，低于 92 显示体积/画质提示。若将来真要放开，只需改一处校验 + UI 下限。

### 4.2 色度采样默认值：**已改成 4:4:4（`jpeg_subsampling = 0`）**

用户明确"4:2:0 损失太大"。改动点：`PackOptions.jpeg_subsampling` 默认 0、
`configs/tiers.yaml` 的 `archive.jpeg_subsampling: 0`、UI 默认选 4:4:4。
4:2:2 / 4:2:0 仍可在 UI / CLI 显式选，用于压体积。

### 4.3 盘位判定：**预计产出 + 20% 冗余**，不再用固定阈值

用户口径：不按"剩多少 G 才允许写"这类绝对阈值拦，而是**按这批要写多少**判。

- 估算：每张的原始字节 × 转码涨幅（4:4:4 ≈ 2.6×，4:2:2 ≈ 1.8×，4:2:0 ≈ 1.3×；
  质量每 ±1 档 ×1.06）× 格式数 + 每条 1.6 KB 元数据，再 ×1.2。
  `reencode=never`（或 `non_jpeg` 下的原图 JPEG）是原样拷贝，不计涨幅。
- 三道闸：
  1. `/api/preview` 给 UI 一个 `estimate`，新建页直接显示"预计写入 / 剩余"；
  2. `JobManager.create()` 在**建任务时**按同一口径判一次，装不下抛 400，**一张图都不写**；
  3. `pack_items()` 开跑前判一次（`PackSpaceError`），运行中每封一个分片 +
     每 500 条用"实测/先验"比例复算剩余量，真的不够就停（落 `stopped`，不是 `done`）。
- 与 `ResourceGuard` 的分工：磁盘**不再是** guard 的固定阈值（剩 30G/15% 对 3 张图也拦，
  对 4TB 批次又太松）。`check_space=true` 时 guard 的磁盘阈值被清零，温度/占空比照旧。
  想完全跳过预算检查：UI 的「盘位预算」开关 / `--no-space-check`。
- 已知局限：估算是**先验系数**，对"已经高度压缩过的小图"会偏高；20% 冗余和"实测比例"
  只吸收一部分。所以运行中那条只在**确实不够**时才停，并且可以关掉。

### 4.4 NL 模式：三种产出模式 + 页内启停的 vLLM

- **模式**：`tag / nl / both`（表单 `output_mode`，`JobSpec.VLM_MODES`）。
  - `both`：NL 由 VLM 出；tag 按「模型与性能」里的后端出（选 `none`/`vlm` 则 VLM 自己出）。
  - `nl`：只产 NL。**本地 tag 后端强制关掉** —— 后端在场时 `run_client` 会把它算出的 tag
    一起写进产物（`need_tags = wants_tag or 后端在场`），等于偷偷多写一份没人要的文件。
  - `tag`：只产 tag，且必须由 VLM 出。CPU 后端在场时 `run_client` 直接抛
    `"--mode tag 不能配 CPU tag 后端"`（VLM 没有活干），所以这里同样强制 `none`。
  - 映射函数是 `jobs.vlm_backend_for(mode, backend)`，单测钉住；模式本身在
    `JobManager.create()` 就校验（否则 HTTP 已回 202，页面只会显示"任务失败"）。
- **本机服务**：`web/vllm_service.py` 的 `VLLM`（进程级单例），端点
  `GET /api/vllm`、`POST /api/vllm/start|stop`、`GET /api/vllm/probe?url=`。
  - 启动走 `model_loader.start_server(..., wait=False)` + 自己的 `wait_ready(on_tick=…)`，
    这样"停止"能在两次轮询之间生效；就绪后写 `run/fingerprint.json`
    （与 `run_client` 读的是**同一个** `FINGERPRINT_PATH`，不一致就会永远"指纹缺失"）。
  - 环境不满足（Windows 原生 / 无卡 / 缺 vllm / CUDA MPS）在 `start` 时**当场 400**，
    不先回 202 再翻 failed。
  - Web 进程退出时（FastAPI lifespan）收掉自己拉起的子进程，否则显存被占着。
  - 远程服务只能 `probe`：`/health` 说明活着、`/v1/models` 说明模型名对不对。
- **UI**：新建页 VLM 区显示本机服务状态芯片 + 启停 + 端口 + 指纹摘要 + 日志尾巴；
  服务地址旁有「探测」。就绪后自动把表单地址对齐到实际 `url`。
  地址指向本机而服务没起时给出明确警告（"连不上"和"模型名写错"在页面上长得一样）。
- **复核口径**：`tagger verify` 加了 `--mode tag|nl|both`（默认跟配置走）。
  VLM 批次若按 `both` 复核，tag-only 的每条都会被判成"缺 nl"——那不是数据坏了。
  同时 `verify._iter_parquet` 认得 `artifacts.ParquetSink` 的 payload-JSON 列
  （`tag` / `nl` 列存整段 json，正文在 `{"tag": …}` / `{"caption": …}` 里）。

### 4.5 吞吐：为 3000 万张准备的四件事

背景：目标量级是 3000 万张。按 40 ms/张的老数字（那是**本地 tag 后端**的实测）
单机要跑两周以上，所以吞吐链路上的每个"保守默认值"都要重新过一遍。

1. **客户端并发不再默认 1**，改为跟服务端 `max_num_seqs` 走（`client.resolve_concurrency`）。
   指纹里没这个数时退回 4。上限 32 —— 再多只是排队，吞吐不变而客户端内存涨。
   显式给 `--concurrency` 仍然优先。Web 表单里 `0` = 自动。
2. **服务端加 `--max-num-batched-tokens`**（`TierSpec.max_num_batched_tokens`，
   0 = 不传、用 vLLM 默认 8192）。这个数是"并发调大却不涨"的头号原因：
   每张图 prefill 约 576 图像 token + 400 指令 token，8192 一步只放得下三四张，
   后面的请求只能等下一步。经验配额是 `seqs × 1024` 起步。
   同时 `--max-num-seqs` / `--gpu-mem` 也能在 `tagger serve` 上当场覆盖
   （`resolve_profile` 的三个 override），Web 面板「批调度」里同样可改。
3. **本机地址不走系统代理**（`netutil.trust_env_for`）。打标机常开着 Clash 一类
   的代理，它的规则未必放过 `127.0.0.1`：轻则每张图的 base64 多绕一圈
   （吞吐砍半），重则请求到不了 vLLM，报出来却是"服务端超时"。
   受影响的是 `TaggerClient`、`wait_ready`、`probe_version`、`/api/vllm/probe`、压测。
   远程地址维持默认——那边可能真需要代理。
4. **`tagger bench`**：扫并发梯度（默认 `1,2,4,8,16`），输出 img/s、tok/s、
   p50/p95 与失败数，并给出**建议并发**（吞吐峰值 90% 以内最小的那个——峰值
   往往是"刚好塞满"，连跑几天会掉到线下）。默认用产出规格生成的真实指令
   （`--real-prompt` 默认开），不是 smoke 那段短 prompt；后者 prompt token 少一半，
   会高估并发上限。给 `--images-total` / `--machines` 就直接折算成天数和台数，
   并把 `limits.duty_cycle` 折进去（压测直接打 HTTP，不经过硬件保护）。

**客户端单张准备成本**（`_resize_to_budget` + base64，2000×1500 源图，本机实测）：
单线程 53 ms/张，4 线程 17 ms/张，8 线程 14 ms/张。也就是说**单线程准备的上限
只有 ~19 img/s**，服务端再快也被这里卡住——超过这个量级必须靠并发线程，
而并发线程正好是上面第 1 条带来的。

**没有采用但值得知道的**：
- DFlash2（块扩散投机草稿器）不解决显存问题，是纯解码加速，且要额外加载
  1~2 GB 草稿权重；官方只发了 Muse-Glimmer-30B / Qwen3.8-27B 两个草稿器，
  与 Qwen3-VL 对不上，vLLM 的多模态投机解码也没有明确保证。打标是 prefill 主导、
  输出短、并发高，收益本就不大。
- 真要省显存应该走 MTP（草稿头长在目标权重里）或量化 KV，不是投机解码。

---

### 4.6 模型选择：两条轴分开选，且选之前能看到后果

用户要求「在页面上就能切 tag 和 NL 的模型，不用回命令行」。落地后两个模型轴各自独立：

- **tag 模型（本地分类头）**：新建页「模型与性能」里的下拉，选项来自
  `/api/defaults` 的 `tag_models`（即 `configs/config.yaml` 的 `tagging.models`，
  按**后端**分组）。选中后会自动把 `backend` 切到它所属的后端——把 pixai 的仓库名
  喂给 wd-eva02 会在加载时炸，这是最容易搞错的一处。最后一项是「自定义仓库名…」，
  落到自由文本输入。
- **NL 模型（VLM 权重）**：「VLM 服务」里的「模型（本机服务权重）」下拉，选项来自
  `/api/defaults` 的 `vllm_models`（即 `configs/tiers.yaml` 的 `models:` 段）。
  默认「自动（按显存落档）」。**权重和分档是正交的**：分档决定 `max_model_len` /
  默认并发 / 精度下限，模型只决定加载哪套权重——所以单独的 `qwen3vl-8b`
  在 16 GB 卡上仍然落 `low-16g`，上下文只有 4096。这个坑靠下面的预览提前暴露。
- **落档预览** `GET /api/vllm/plan?model=&tier=&max_num_seqs=&max_num_batched_tokens=&gpu_mem=`：
  不启动服务，只跑 `resolve_profile` 把「会落哪一档、什么精度、多少上下文、默认并发」
  算出来。前端 400 ms 防抖，服务 ready 时改显示真实 `snapshot().plan`。
  失败时返回 `{"ok": false, "error": "一句话"}`，`ConfigError` 不前缀类名。
- **服务名**：`--served-model-name` 现在真的来自表单的「模型名」（之前写死 `tagger`）。
  它只是路由名，必须和 `JobSpec.vlm_model` 一致，否则报「模型不存在」。
- **启动前校验**：`start(model=…)` 会先查 `tiers.yaml` 的 `models` 和 `tiers`，
  未知值当场 `ValueError`（转 400），不会先回 202 再翻 failed。

同一轮补上的静默丢弃点（表单上有、但没传到后端的）：

- VLM 路径的**画师注入**：`_run_vlm` 之前完全忽略表单的 `artist` / `artist_inject`，
  现在会写进 `ops.artist`。注意 `enabled` 和 `inject_caption` 是两个开关，
  关掉注入仍保留 JSON 里的 `artist` 字段。
- **质检三条线**（`quality_pass_score` / `quality_drop_score` / `quality_max_attempts`）：
  之前只能改配置文件。现在 `0` = 跟配置走，非 0 才覆盖。
- **失败跳过阈值**（`max_failures`）：之前只能命令行给。
- **盘位预算**（`check_space`）：之前只有「数据打包」页有，打标任务写满盘不可达；
  现在新建页也有，且只在非 VLM 模式显示（VLM 模式不落原图）。

**这四项后来全部接线了**，见 §4.7——审计当时列在这里，用户追问「能不能也加到 webui」，
结论是能，而且其中三条（CLIP、升级、file://）原来不只是"没入口"，是**链路本身没通**。

---

### 4.7 把留在命令行的四项也接上了（第二轮：其余配置项全量曝光）

§4.6 报表里"有意留在命令行"的四项，用户要求全部上页面。接线过程中发现它们分两类：

**(a) 只是没入口，链路是通的**

- **重试阶梯**（`retry_temp_step` / `retry_temp_cap` / `retry_pixels_scale` / `retry_pixels_cap`）：
  表单新增四个数字框，`0` = 跟配置。页面上直接显示配置里的真实数字
  （`0 = 用配置的 0.15`），来源是新加的 `/api/defaults` 的 `quality` 段——
  写死在 JSX 里的话，改了 `tiers.yaml` 页面就开始骗人。
  注意抬分辨率要求服务端 `max_pixels` 留余量，否则会被钳回基线（`client.py` 有对应警告）。

**(b) 链路本身没通，先修链路再给入口**

- **L2 图文 CLIP 打分**（`quality.enable_clip`）：原来 `QualityGate.evaluate` 从来没收到过
  图片——`_run` 只传了文本。现在质检前按需解码一次（**只有开了 CLIP 才解码**，
  否则每张图白付一次全尺寸解码），`ClipScorer` 同时修了三个问题：
  每次 `score` 都 `.cuda()`（改成设备缓存）、没有锁（并发会互相踩权重）、
  缺依赖时只说"不可用"不说为什么（`reason` 里给出 `pip install open_clip_torch`）。
  并发仍被摁到 1——锁只能保证不崩，保证不了吞吐。
- **重试升级**（`escalate_model`）：vLLM 一个进程只服务一套权重，所以升级目标只能是
  **另一个地址**上的第二份服务。`TaggerClient` 新增 `escalate_url` / `escalate_model`，
  只有 `RetryPlan.model_hint == "escalate"` 时才切，且切了不再退回（重试的语义是
  "小模型救不回来"）。产物 provenance 记 `escalated: true` + `escalated_model`——
  两套权重的样本不能当同一批数据混用，清洗侧要能一行挑出来。
  只填地址或只填模型名时启动会警告并保持关闭（静默不生效比报错更难查）。
- **`file://` 传图**（`serve --data-root` / `client --use-file-url`）：
  服务端侧 `--allowed-local-media-path` 现在来自「批调度」里的「file:// 放行目录」，
  `ServerHandle.local_media_root` → 指纹 → 页面据此决定能不能开直传开关。
  放行目录必须覆盖**产物目录**（客户端的 spool 在 `<out>/.spool`），
  否则每张图都是 400 而错误只说"路径不被允许"——页面上有这一条实时校验。
  启动时传不存在的目录当场 400，不让它起得来但每张都失败。

**第二轮：剩下"看起来该留在配置里"的也全上页面了**（用户明确表态：团队内部工具，
这些字段放页面上不是问题）。这轮没有新增链路，纯曝光，但有一个哨兵值的坑：

| 上页面的项 | 落点 | 注意 |
|---|---|---|
| `quality.clip_pretrained`（+ `clip_model`） | 生成参数 → 语义复核（L2），随 CLIP 开关展开 | 只在该开关打开时出现；换预训练来源等于换打分口径，换完历史分数不可比 |
| `min_chars` / `max_chars` / `min_tag_coverage` / `max_repeat_ratio` | 生成参数 → 规则层（L1） | **不能用 `0` 当"跟配置"的哨兵**：0 在这里是合法值（不限最短长度、零重复也放行），所以"未设置"用 `None`（页面留空 = JSON null）。`min_chars > max_chars` 在 `create()` 就 400，否则跑起来是 0% 过检 |
| `kp` / `ki` / `temp_deadband_c` / `min_duty` / `max_sleep_s` | 运行保护 → 阈值 → 折叠的「闭环细调（目标非 0 时才生效）」 | `GUARD_KNOBS` 白名单从 7 条扩到 12 条，`normalize_guard_policy` 负责夹区间；PID 参数只在该块里，默认折叠，避免误调 |

至此页面能调的与后端认的是同一份名单。真正只剩配置文件的：`pass_score`/`drop_score`
之外的策略常量（如 `max_pixels` 基线）、Dedup / Upload / Packaging / Stats 的
命令行开关——后四者各自的页面本来就全有。

---

## 5. 尚未做：Splash 启动页

用户要求：参考 `h:/lulynx-trainer` 的 splash 样式，但**动画要重新设计**，因为两边功能不同（训练器 vs 打标器）。

现状：WPF 壳子只有一个朴素的启动遮罩（`MainWindow.xaml` 的 `Overlay` + `LoadingPanel`），确定进度的 `ProgressBar` 是 indeterminate。

设计方向（待实现，可自由发挥）：

- 视觉令牌沿用 `tokens.css`：底色 `#0c0f13`、唯一强调色 PyTorch 橙 `#ee4c2c`
- 动画叙事对应真实流程，而不是通用的"转圈"：
  1. 一串小方块（代表图片）从左依次亮起 → 表达"扫目录"
  2. 方块被逐个"打上"橙色角标 → 表达"逐张打标"
  3. 方块堆叠成一个整齐的网格并收拢 → 表达"平铺/封包"
- 启动阶段可以真实驱动：`PythonServer` 已经在抛 `Output` 事件，可把"加载后端""后端就绪""N 张待处理"这些日志映射成阶段文字
- 参考实现文件（只读，不要复制代码）：`h:/lulynx-trainer/backend/launcher/desktop/`、`.../web/src/components/`

> 注意：**不要 import 或依赖 lulynx 仓库的任何文件**。视觉可参考，代码在本仓库独立实现。

---

## 6. 环境坑（踩过的，别再踩）

1. **批量删除被拦**：`Remove-Item -Recurse -Force` 报
   `[safe-delete][SAFE_DELETE_BULK_GUARD_ERROR] failed to read state: Unexpected token ... is not valid JSON`，什么都没删。
   → 清理临时目录改用 `Move-Item` 到 `%TEMP%`，不要反复试删除、也不要绕过守卫。

2. **`vite.config.ts` 里 `emptyOutDir: false`**：构建时清空 `dist` 会撞上第 1 条导致构建失败。
   代价是 `dist/assets` 会累积旧 hash 文件，要干净就手动清空 `dist` 再 build。

3. **PowerShell 5.1 编码**：无 BOM 的 `.ps1` 会被按 GBK 解析，含中文注释直接语法错误 → `.ps1` 存 **UTF-8 BOM**；
   `.cmd` 相反，要存 **GBK(936)**，否则 `echo` 中文乱码。

4. **`wsl` 不可用**（本机 WSL 未装发行版），校验 bash 脚本语法用 `C:\Program Files\Git\bin\bash.exe -n`。

5. **`web/jobs.py` 速率算法**：已改成从"第一张产出"起算，避免模型加载 6~8s 把 ETA 撑成"还剩 1 小时"。改动原因写在代码注释里，不要改回从 `started` 起算。

6. **残留目录**：`work/_lt_smoke/`（800 张测试图 + 探针脚本）需要手动删。

---

## 7. 建议的下一步顺序

1. ~~定 4.1 / 4.2 两个决策~~ ✅ 已定：质量 90-100，采样默认 4:4:4。
2. ~~打通 pack 链路~~ ✅ 两阶段 job + CLI `tagger pack` + Web API + 前端。
3. ~~`/api/fs` 支持压缩包~~ ✅ 列 `.zip`，可点选，缩略图支持 zip 成员。
4. ~~前端加"输出"分区~~ ✅ 格式/布局/质量/采样/压缩/每包上限 + 命名实时预览。
5. ~~画师层级与注入的 UI 开关~~ ✅ 层级数字输入 + 注入 `.tag/.nl` 开关（也可只留 JSON）。
6. ~~盘位预算改成"预计产出 + 20%"~~ ✅ 见 §4.3；mirror 布局也去掉了包名前缀。
7. ~~加 NL 模式~~ ✅ 三种产出模式 + 页内 vLLM 启停 + 指纹；见 §4.4。
8. **分发包 / 回收对账 / 复核**接 UI（后端已有 `distribute` / `merge` / `verify`）。
9. **Splash**：放到最后，纯视觉，不阻塞功能。

---

## 8. 关键事实速查

- tag 后端：`wd-eva02` 本地权重在 `models/wd-eva02/`，CUDA 可用
- 实测吞吐：800 张 / 32.0s ≈ **40ms/张**（4070 Ti SUPER，bf16，batch=4，含解码与写盘）；模型加载另算 6~8s
- 默认端口 `27100`；前端 dev 在 `3010`
- 产物（本地打标）：`<out>/tags/<image_id>-tag.json`，含 `tag / tags[] / rating{} / provenance{}`；
  封包任务的 `image_id` 是全局序号（`alice-000001`），纯打标任务仍是相对路径 id
- 产物（打包）：parquet（默认）/ `.tar`（webdataset）/ `.zip`，按 `max_records` 封口；
  每次封包的分片行记在 `.packstate-<参数指纹>.jsonl`（指纹含格式/质量/采样/重编码/上限/布局/命名口径）
- 盘位预算：先验系数在 `pipeline._TRANSCODE_GROWTH` / `_QUALITY_STEP`，冗余 `DISK_HEADROOM=0.2`；
  `reencode` 的原样拷贝路径不计涨幅（否则会高估一倍）
- mirror 布局的成员名就是包内路径，不再套 `包名/` 前缀；同名冲突才追加 `-<image_id>` 兜底
- 桌面壳子产物：`dist/desktop/Tagger.Shell.exe`（约 152 KB，依赖 .NET 8+ 桌面运行时）
- vLLM 服务：页面拉起的写 `run/fingerprint.json`（任务也读这个同一个路径）；
  端点 `/api/vllm`，模块 `web/vllm_service.py`
- 模型权重可在「模型管理」页直接下载：每行一个目录（默认 tag=`models/<后端>`、
  NL=`models/nl/<key>`），进度/网速靠轮询 `/api/models` 的 `download` 字段；
  下完变成「删除 / 打开位置」，取消后留下的半截文件会标记 `partial` 并支持续传。
  **删除有守卫：只允许删 `models/` 之内的路径**，防止 local_dir 填成素材目录。
- 下载代理在「模型管理」页底部，存 `configs/models.json` 的 `proxy` 字段，
  支持 `http://` / `socks5://`（只写 `host:port` 会按 http 处理）。
  **只作用于这页的下载**：`vllm serve` 自己拉权重仍走系统 `HTTP_PROXY/HTTPS_PROXY`。
  socks5 依赖 `socksio`（已装并锁进 requirements-embedded.txt）；
  `HF_ENDPOINT` 换镜像仍然有效，两者可以叠加。
- tiers.yaml 里加了 `qwen3vl-4b-abliterated` / `qwen3vl-8b-abliterated`
  （prithivMLmods 的 Unredacted-MAX）。打标用无审查版：原版遇到 NSFW 图会拒答，
  表现是"整张只出几句干话、一个 tag 都没有"，而且不报错。这两个没进 tiers，
  要用得手动选或 `--model` 覆盖。
- 新建页的「打标方式」是三选：**Tag 打标**（engine=local）/ **NL 打标**（engine=vlm + output_mode=nl）/
  **同时**（engine=vlm + output_mode=both）。页面按选择只显示对应的模型面板；
  模型清单在「模型管理」页改（左栏独立 tab），写 `configs/models.json`，由
  `config.load_tier_table()` 合并进 `models` / `tagging.models` 两段，CLI 与起服务同源。
  新建页常驻挂载（`App.tsx` 里 CSS 隐藏），切页回来表单内容不丢。
- 硬件保护阈值（占空比 / 温度闭环 / 熔断 / 恢复 / 连跑休息）在新建页可调，默认值来自
  `configs/tiers.yaml` 的 `limits` 段（`/api/defaults` 的 `guard`），白名单是
  `jobs.GUARD_KNOBS`——页面能调的和后端认的必须同一份名单。磁盘两项不在其中，
  仍归「盘位预算」。改动只作用于本次任务，不写回配置文件。
- 客户端并发：不填时 = 指纹里的 `max_num_seqs`（上限 32），没有指纹时 4。
  填 1 不会报错，只是慢十倍——这是最容易在 3000 万量级上踩的坑
- 服务端批调度：`--max-num-seqs` / `--max-num-batched-tokens` / `--gpu-mem`
  在 `tagger serve` 与 Web 面板都能改；后者默认 0（= vLLM 的 8192）
- 本机地址不吃系统代理（`netutil`）：开着代理软件时，"服务起着却连不上"通常就是这个
- 模型选项的唯一来源是配置文件：tag 模型来自 `configs/config.yaml` 的 `tagging.models`
  （按后端分组，切模型会同时切后端），VLM 权重来自 `configs/tiers.yaml` 的 `models:`。
  往 YAML 里加一条，页面下拉立刻多一项，不用改前端
- `GET /api/vllm/plan` 只算不启动：权重 + 分档是两个正交轴，单挑 8B 在 16 GB 卡上
  仍然落 `low-16g`（4096 上下文）。启动前先看这个，别等跑起来才发现
- 质检的默认值走 `/api/defaults` 的 `quality` 段：表单里的 `0` 是"不覆盖"的哨兵，
  占位说明里的数字必须来自配置，不能写死在 JSX
- L2 CLIP 打分要求**先装依赖**（`pip install open_clip_torch`），缺依赖时质检降级到
  L1 并把原因写进 `ClipScorer.reason`；开启后并发被摁到 1，只适合小批量抽检
- 重试升级需要**第二个 vLLM 服务**（另一个地址 + 另一个 `--served-model-name`），
  产物 provenance 会带 `escalated` / `escalated_model`，清洗时按它整批剔除
- `file://` 直传要三件事同时成立：服务端 `--allowed-local-media-path`（页面上叫
  「file:// 放行目录」）、放行范围覆盖产物目录、任务侧开「file:// 直传」。
  缺一条就是每张图 400，而错误信息只说"路径不被允许"
- 表单哨兵有两套，别混：大多数数字框 `0` = 跟配置；但 L1 四条（`min_chars` /
  `max_chars` / `min_tag_coverage` / `max_repeat_ratio`）的 `0` 是**合法值**，
  "跟配置"必须用 `None`（页面留空）。`min_chars > max_chars` 在 `create()` 就拒
- 测试：`pytest tests -q` → 451 passed

---

## 9. 2026-11-04：NL 引擎改为双引擎（新增 llama.cpp）

**起因**：Windows 成员机起不了 vLLM（官方只有 Linux 版），本地 NL 必须走 WSL2 或远程。
实测过三个候选后确认：**Windows 原生能跑 Qwen3-VL 且真用上 GPU 的只有 llama.cpp**。

| 引擎 | Windows GPU | 实测（8B-Q4_K_M，4070 Ti SUPER） |
|---|---|---|
| llama.cpp | ✅ CUDA 预编译 | 0.41 / 0.74 / **1.27** / 1.34 张每秒（并发 1/2/4/8） |
| mistral.rs | ❌ 只有 CPU 版 | 4.86 tok/s（慢 40 倍；CUDA wheel 只发 manylinux） |
| vLLM 官方 | ❌ Linux only | — |

**改动**
- 新增 `tagger/nl_engine.py`：引擎名归一化、GGUF/mmproj 解析、`llama-server` 命令行拼装、
  `/props` 版本探测、缺件时的人话报错。vLLM 那一支的判定仍走 `model_loader.check_environment`。
- `model_loader`：`start_server(engine=)` 分流；`ServerHandle.engine`；指纹新增
  `engine` / `gguf` / `mmproj` / `engine_version`（**换引擎等于换一代数据**，
  与 `quantization` 同理，混进一份数据集事后无法区分）。
- `config.ModelSpec` 新增 `gguf` / `mmproj`；`tiers.yaml` 的 `qwen3vl-8b` 已配好路径。
- `serve --engine vllm|llamacpp`；Web 面板「引擎」下拉 + `GET /api/vllm/capability?engine=`。
- 依赖清理：`quantize` extra 去掉 `compressed-tensors`（后端一次都没 import 过，
  只是 llmcompressor 的传递依赖，钉住它只会在升级时制造冲突）。
- `devtools/setup_embedded_env.ps1` 增加引擎可用性检查与提示。
- 新增 `devtools/bench_nl_engine.py`（基准）与 `devtools/nl-engine-bench.md`（数据）。

**四个容易踩的点**
1. `-c` 是**总池**，vLLM 的 `max_model_len` 是**每序列**。必须 `-c = max_model_len × slots`，
   照抄会让每 slot 上下文缩到 1/n，长 caption 被截断且**不报错**。
2. `max_pixels` 要翻译成 `--image-max-tokens`（32×32 像素/token，589824 → 576）。
   两个引擎喂给模型的视觉 token 数必须一致，否则数据出现按引擎分组的伪特征。
3. 缺 `mmproj` 时 llama-server **照常启动**，只是每张图走纯文本路径——模型开始编内容。
   所以缺它要当硬错误拦住，不能当成可选文件。
4. 4 个 slot 就饱和（8 → p95 翻倍、吞吐 +5%）。并发别照 vLLM 的 `max_num_seqs` 直接搬。

**机时账（必须让所有人知道）**：1.27 张/秒 → 每人 300 万张 = 26 天/机；
3000 万张单机 260 天，要 30 天收工至少 9 台同规格机器。单机跑不完不是优化问题。

### 9.1 槽位预算（把 GPU 从 58% 用到 82%）
`-np` 与 `-c` 的配比是最容易抄错的地方：照搬 vLLM 的 `max_num_seqs × max_model_len`
（4×8192）只用到 58% 的 GPU。实测（8B-Q4_K_M，真实素材）：

| slot | ctx/slot | KV | 张/秒 | p50 | util |
|---:|---:|---|---:|---:|---:|
| 4 | 8192 | f16 | 0.91 | 3.80s | 58% |
| 8 | 4096 | f16 | 1.00 | 7.21s | 79% |
| **12** | **2816** | **q8_0** | **1.22** | 8.37s | **82%** |
| 16 | 2560 | q8_0 | 1.18 | 11.88s | 79% |

12 是拐点（16 更慢）。已写成 `nl_engine.slot_plan()`：按显存分档
（16G→12 / 10G→8 / 7G→4 / 未知→2），`-c = slots × 3072`，KV 量化 q8_0。
★ `-np 8 -c 65536` 会让显存吃满 15.8/16.4G、decode 崩到 1 tok/s —— 别开。

**prefill 不是瓶颈**：并发 1→4 时短输出从 1.37 → 5.70 张/秒（4.16×），
视觉编码按批并行。所以 IO 预取/队列最多贡献 2~3%（准备 82ms vs 请求 2.5s），
真正的上限在 decode 的每步固定成本（llama.cpp 没有 CUDA Graph）。
