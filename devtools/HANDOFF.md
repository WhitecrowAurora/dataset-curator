# 打标器交接文档

> 最后更新：2026-11-04
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
powershell -File desktop\build.ps1              # 多文件 → dist\desktop\Tagger.Shell.exe
powershell -File desktop\build.ps1 -SingleFile  # 单文件 → dist\desktop-single\Tagger.Shell.exe
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

### 多卡（数据并行）

一张卡一个独立服务进程，各自打各自的图，产物写同一个输出目录——**不需要事后合并**，
每张图落到哪个 json 与它跑在哪张卡上无关。

```bat
:: 页面上：新建任务 → 本机服务 → 引擎 → 勾「启用多卡」→ 选卡 → 启动多卡
:: 命令行：--url 是第一个槽，--urls 是其余的
env\python\python.exe -m tagger.cli client --url http://127.0.0.1:8010 ^
    --urls http://127.0.0.1:8011 http://127.0.0.1:8012 --concurrency 4
```

- **不是张量并行**。每卡一份完整权重（8B-Q4 约 6GB）换线性吞吐；一张卡挂了其余照跑
  （状态变 `partial`）。张量并行要按层切权重，llama.cpp 只能靠 `--split-mode` 且默认
  1:1 切，卡显存不等时直接 OOM。
- 隔离靠子进程的 `CUDA_VISIBLE_DEVICES`，不靠引擎的 `--device` 参数：两个引擎都认它，
  且隔离后子进程眼里卡号是 0，slot 规划还在原来那套假设里。
- **每卡并发**（页面 / `per_card_seqs`）= 该卡服务的 slot 数 = 任务侧每槽在途数。
  两处必须同一个数：服务开 8 个 slot 而任务只发 2 条，等于把卡闲着一半。
- 端口 = 基端口（默认 8010）+ 卡序号。和单卡服务的 8000 错开，避免默认值撞车。
- 单卡服务在跑时**拒绝**启动多卡（会抢显存），反之亦然。
- CLIP 质检开着时多卡降级为只用一个服务：那个打分器是进程内单例，不是线程安全的。
- 卡号不进产物、不进指纹：换卡不该改变产出内容，也不该让同一批数据被判成两代。

代码位置：`tagger/client.py`（`run_batch(clients=...)` 轮询分发、每槽仍是
`concurrency` 条）、`tagger/web/multi_gpu.py`（一组服务 + 每卡状态表）、
`tagger/web/ui/src/components/MultiGpuPanel.tsx`（选卡与监控）。

### 暂停与续做

「暂停」不是「停止」：停止是收工（不再回来），暂停是原地挂起（等你回来）。
两者都不丢进度，区别在恢复的代价。

- **暂停**（运行页按钮）：在途的几张按批结清，之后不再发新任务；恢复是瞬时的，
  模型和连接都还热着。封包阶段不能暂停——tar/zip 不支持追加，停下只会留半成品。
  本地分类头引擎（`engine=local`）也不支持：它按块整批喂 GPU，块内没有"停在
  两张之间"的位置，假装暂停只会让状态卡住而进度还在涨。
- **关掉程序再打开**：暂停或停止时会把参数写成 `run/resume.json`，下次打开页面
  顶部出现「上次的任务没跑完 → 继续打标」，点一下就用原参数重建任务。进程没了
  就没有"接着那个线程"这回事，重建后已完成的图靠**产物 + 指纹**判定跳过，效果
  仍是续做。跑完/失败会自动删掉这条记录；「忽略」只删记录，产物一张不动。
- **历史任务页每行的「继续」**：`paused` 走 `/api/jobs/{id}/resume`（叫醒还挂着的
  worker，同一个 id 接着跑）；`stopped` / `failed` 走 `/api/jobs/{id}/restart`
  （线程已退出，按原 spec 重开一个，历史里会多一条）。两者对用户都叫"继续"，
  区别只在后台是一条命还是两条命。
- **暂停时强制落盘**（`_flush_sink`）：parquet 产物默认攒满 2000 行才写一片，
  不在这里 flush 的话，用户按完暂停就关窗口，界面上的「已完成 300 张」会随着
  关闭凭空消失、下次还要重打。json 布局逐张写，不受影响。
- 测试隔离：`TAGGER_RESUME_PATH` 环境变量可改落盘位置，`tests/conftest.py`
  把它指到系统临时目录——否则跑一次 pytest 就会把用户页面上的"继续上次任务"
  顶成一条指向 pytest 临时目录的假记录。

代码：`tagger/client.py` 的 `run_batch(paused=...)`、`tagger/web/jobs.py` 的
`pause / resume / resume_last`、`tagger/web/ui/src/components/ResumeBanner.tsx`。

### 部署自检（侧栏「部署自检」）

给接收方用的：装完点一下，15 个方块逐格亮起——转圈 → 打勾 / 打叉 / 划掉。
选一个装图片的文件夹（可勾「包含子文件夹」），自动验完环境、目录、产物格式与服务链路，
最后**真发一张图**过去，确认返回能被解析成 tag + caption。

15 项：解释器 / 依赖库 / 配置文件 / 显存档位 / 显卡 / 磁盘空间 / 扫描图片 / 子文件夹 /
图片解码 / 产物目录 / 产物格式 / 环境指纹 / 标签归一 / 推理服务 / 真实推理。

- 三种终态：**✓ 通过**（绿）、**✕ 失败**（红，方块抖一下并进下方错误清单）、
  **– 跳过**（灰虚线）。**跳过不算失败**——没起服务、没装 torch、没勾子文件夹都属于
  "这台机器上没这项"，装机验证不该因此整片飘红。
- 每项至少停 350ms（`MIN_STEP_S`）。自检本身十几项 200ms 就跑完了，不给下限的话
  动画一闪而过，用户反而怀疑"它到底跑了没有"。宁可慢 5 秒也要让人看明白。
- **不建任务**，不写进「历史任务」：点一下测试不该在历史里留一条记录。
- 服务地址留空 = 自动取已就绪的多卡服务第一个；都没有就跳过最后两项。
- 全程只写 `run/selfcheck/`，不碰用户数据。

代码：`tagger/web/selfcheck.py`（15 项检查与状态机）、
`tagger/web/ui/src/components/SelfCheckDialog.tsx`（方块墙与动画）。

### 打包分发给别人

一条命令出包（白名单式，不会误带 `models\`）：

```bat
pack-dist.bat                        :: 不带 env\python，约 22MB，对方联网装
pack-dist.bat -WithEnv               :: 带精简后的 env\python，对方免联网
pack-dist.bat -NoTests -Out D:\x.zip :: 不要测试、指定输出路径
```

产物落在 `dist\tagger-<时间戳>.zip`，包内顶层是 `tagger-<时间戳>\` 一个目录（解压不会散落），
并附一份 `README-FIRST.txt` 给接收方。

**脚本自动包含**：`tagger\`（含 `web\ui\dist`，排除 `node_modules` / `dist.bak`）、`tests\`、
`devtools\`、`configs\`、`desktop\`（排除 `bin` `obj`）、`env\initialization\`（embeddable
解释器 + pip wheel，约 12MB）、根目录脚本与 `Tagger.Shell.exe`。

**永远不会包含**：`models\`（42GB）、`work\`、`run\`、`.pytest_cache`、`node_modules`、
`dist\desktop`、`env\python`（除 `-WithEnv`）。

`-WithEnv` 会先看 `env\python` 里还有没有 torch，有就中止——否则会出一个 4GB 的包。要带环境
就先 `prune-env.bat -Apply -StripBytecode`。

**无论带不带 env，接收方都必须先跑一次 `install.bat`**：它会把 `_tagger_repo.pth` 里的绝对
路径改写成自己的仓库位置，不跑就 import 不到 tagger，壳子只会报"服务刚启动就退出了"。

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
关键文件：`Services/PythonServer.cs`（找仓库/找解释器/选端口/等健康检查）、`MainWindow.xaml.cs`（遮罩、错误面板、退出时杀进程树）、`Services/RuntimeCheck.cs`（.NET 与 WebView2 运行时缺失时的提示与下载引导）。

发布形态：`-SingleFile` 出单个 exe（约 1.4MB，依赖框架版，目标机需 .NET 8+ 桌面运行时）；
加 `-SelfContained` 则自带运行时（约 65MB，压缩后），不需要目标机装任何东西。
运行时版本不足时由 `RuntimeCheck` 弹中文窗并打开微软下载页；**完全没装 .NET** 时托管代码不会执行，
那种情况由 apphost 自带的英文对话框兜底（同样可以一键跳下载页）。

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
- NL 推理可以不吃本地权重：新建页「推理来源」三选（本机/自建、OpenAI 兼容 API、
  Anthropic API）。API 模式不用起服务、没有环境指纹、不认 `file://` 直传；
  方言差异集中在 `tagger/nl_api.py`，主循环不分支。详见 §15
- API 模式三件事别再踩：429 会退避重试（读 `Retry-After`），401/403/404 连续
  20 条直接中止整批，预检用「测试连接」（`POST /api/nl-api/probe`，发 8×8 小图）。
  成本看 token：输入才是大头，价目在 `configs/nl_api.json` 的 `prices` 段，
  没填就只报 token 不报钱。详见 §16
- 测试：`pytest tests -q` → 534 passed

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

---

## 10. 2026-11-04：新建任务页接上「描述长度」

**起因**：`caption_chars`（字符区间引导）后端从 `config.py` 到 `client._prompt_for`
一直是通的，但 WebUI 只发 `max_tokens`，等于最省机时的旋钮没接出来。

**改动**（纯接线，后端零改动）
- `web/ui/src/pages/NewJob.tsx`：`max_tokens` 下面新增「描述长度」两个数字框
  （下限 100–2900 / 上限 200–3000，空 = 派生）；payload 里 `caption_chars`
  只在**两端都填且上界 > 下界**时发数组，否则发 `null`。
- `web/ui/src/lib/api.ts`：`JobRequest` 镜像类型补 `caption_chars: [number, number] | null`。

**两个参数不是一个东西**（页面上也这么写，别合并）
- `max_tokens`：生成硬上限，写不下就截断，属于闸门。
- `caption_chars`：只告诉模型"往多长写"，走 `_prompt_for` 的 `_CAPTION_RULES`，
  不动 `nl_min` / `max_tokens`。实测 400-1024 → 250-500：字数 −33%、
  单张 −36%，六段要素覆盖只掉 2~3pp。
- 后端 `parse_caption_chars` 也校验 100 ≤ lo < hi ≤ 3000，前端边界与之对齐；
  `None` 时按 token 区间派生（400-1024），旧任务行为不变。
- 该值会进 provenance 的 `caption_chars`，所以**换长度等于换一批数据**，
  事后混跑要先看这一项。

---

## 11. 2026-11-04：结果缓存 + HF 分片预取（按剩余空间）

两件事都围绕同一本账：**省机时要省在推理上**。客户端准备（解码+缩放+base64）
只有 82 ms，一次推理 2.5 s，所以缓存 CPU 那一段的天花板只有 3%。

### 11.1 结果缓存（`tagger/result_cache.py`）

`--cache` / 表单「结果缓存」显式开启。sqlite（WAL，形态照抄 `dedup.HashCache`），
默认落 `run/result-cache.sqlite3`（与指纹同级，**跨任务共享**——放 out_dir 里就只能
命中同一个输出目录，重跑换个目录就白做）。

- **默认关**：首轮 3000 万张命中率为 0，而图哈希本身要 1~2 ms/张（30 TB ≈ 十几小时）。
  只有重跑 / 换输出目录 / 多机重叠素材才回本。
- key = `运行参数指纹 + sha256(图片字节)`。指纹一次算好（`TaggerClient._cache_fingerprint`），
  含 prompt、model/revision/precision/quantization、max_pixels、caption_chars、
  temperature/top_p/max_tokens/seed、escalate 地址与模型、tag 后端+精度、质检 policy 全字段。
  **加字段门槛低、删字段门槛高**：漏一项不是少命中，是换 seed 重跑拿到旧结果还标成新参数。
- 命中时 payload **重新生成**（provenance / ts / 画师注入都按本次运行），
  顶层与 provenance 各写一个 `cache_hit: true`；`attempts`/token 保留首次的真实值。
- 上限默认 8 GB，超了按 `used` 删最旧的四分之一；VACUUM 只在超 2 倍时才做。

**续跑校验（顺带修的老洞）**：`_existing_ok` 现在比对 provenance 的
`model_id / model_revision / precision / max_pixels / prompt_sha / seed`（tag 产物另加
`tag_backend / tag_precision`）。以前只看"文件非空"，换个模型重跑会静默复用上一代产出。
旧产物缺这些字段时按"无法判定"放行，否则升级后第一次运行要整批重打。
★ 不要拿 `temperature` 比：产物记的是重试后实际用的那一档，会让每条重试过的产物无限重打。

### 11.2 HF 分片预取（`hf_source.iter_hf_shards`）

串行时"下载 0.5~2 GB（几十秒）+ GPU 空转"，预取把下载藏进上一片的处理时间里。

- `--hf-prefetch N`（表单「预取分片」，默认 1，0=串行）；窗口 = 当前 1 片 + N 片。
- `--hf-reserve-gb`（默认 0 = 自动：最大分片的 30%，不少于 2 GB）。**每次提交下载前重新量**
  `cache_dir` 所在盘，`剩 − 在途体积 < 这一片 + 余量` 就不提交，退化成串行；
  一片都放不下时明确报错（带剩余/需要/清理建议），不是下到一半写失败。
- 处理完立刻 `cleanup`（已有语义），空间随即释放 → 下一次提交又能过 ——
  这就是"打完一片删一片、按剩余空间下下一片"。
- 提前退出（`--limit` / 异常）时，已下载但没消费的分片在 `finally` 里删掉。
- `list_shards_sized()` 从 tree 接口顺手拿 `size`（原来 `list_shards` 把大小丢了），
  `size` 缺失时给 0（那时不拦，只能下完再看）。

### 11.3 顺带修的两处

- `schema.Provenance` 漏了上一轮加的 `caption_chars`：`_prov_base` 一直在写，
  但清单没有 → `Annotation.from_jsonl` 读回会炸。`tests/test_fixes.py` 的键位一致性测试
  就是为这个存在的，上一轮只跑了 tsc 没跑 pytest，漏了。**加 provenance 字段必须两边同改。**
- Web 的 HF 任务把 `hf_pattern` 写死成 `"*.parquet"`：仓库只有 tar 时会被过滤成 0 个分片，
  报出来是"没有可处理的 .tar"。改成空串（按实际格式挑），并让 `/api/hf/files`
  收 `format` 参数列对应的清单。**Web 端现在真的能跑 tar 分片了。**

---

## 12. 2026-11-04：下载页（分片预下载，与打标解耦）

**动机**：网络差的机器上，任务一边下载一边打标，GPU 有一半时间在等 IO；
而 parquet 流式读对网络的要求更高（每张图都要回源）。所以给一个独立页面，
按硬盘空间把分片提前备好——**用硬盘换速度**，网络好的人不用它也没损失。

### 12.1 缓存布局统一（这是"提前下好能被复用"的前提）

- `hf_source.cache_dir_for(repo)` → `run/hf-cache/<org--name>/`。
  必须按仓库分目录：不同仓库常有同名的 `shard-0000.tar`，平铺会互相覆盖，
  而"文件存在=下载完整"的判定会让它**静默用错数据**。
- 打标任务的 `hf_cache` 默认也用这个目录（原来是 `out_dir/.hf-cache`，
  每个任务各下一份，下载页下的那批永远命中不了）。
- `iter_hf_shards` 对缓存目录里的分片一律按 `temporary=True` 处理：
  上次中断留下的完整 tar 会被 `download_shard` 以 `temporary=False` 复用，
  那样 `cleanup` 就不删了，缓存目录会每片 0.5~2 GB 地涨。
- parquet 也走同一套：`run_parquet` 先查 `hf_cache/<file>`，命中就传本地路径
  （`iter_parquet_samples` 本地文件与 `hf://` 都认，产物与跳过逻辑不变）。

### 12.2 `download_shard` 增强

`on_progress(done, total)`、`should_stop()`、`.part` 的 HTTP Range 续传、
保留仓库内的子目录层级、拒绝 `..` 穿越；新增 `HFCancelled`。
★ `HFCancelled` **故意不继承 `HFError`**：否则会被 `except HFError: raise`
或 `except (HTTPError, OSError)` 当成可重试的网络错误，重试 3 次，
或者把半截 `.part` 删掉（下次就得从头下）。

### 12.3 下载队列（`web/hf_downloads.py`）

- **单线程**：带宽是瓶颈，并发下载只是互相抢，还让"剩余空间"算不准。
- 提交前按 `剩 − 在途 < 这一片 + 余量` 判断，不够就留在 `queued` 并在
  message 里写清缺多少——不是"下到一半写失败"。
- 取消正在下的那片是置 `cancelling`，由 `should_stop` 感知后抛
  `HFCancelled`，**保留 .part**；再点下载从断点续。
- 状态落 `run/hf-downloads.json`（重启后 `running` 归 `queued`），
  **token 只放内存**：私有仓库凭证不进磁盘。
- 单例懒加载（`app.downloads()`），模块导入不起后台线程——测试与命令行
  不该因此多一个线程、多一份状态文件。

### 12.4 接口与页面

- `GET/POST /api/downloads`、`POST /api/downloads/{id}/cancel`、
  `/cancel-all`、`/clear`、`GET /api/downloads/cached?repo=`。
- `/api/hf/files` 现在返回 `sizes`（parquet 也走 tree 接口），
  下载页据此做空间检查；没有 size 就只能"下完再看"，等于没限流。
- 页面 `Downloads.tsx`（侧栏「下载」）：选仓库/格式/版本/token → 读清单 →
  勾选（本地已有的自动跳过）→ 入队；右侧显示剩余空间、余量、队列进度，
  支持单片暂停与全部暂停。

**怎么用**：网络差 → 先在下载页把前几片备好，再去新建任务选同一个仓库，
任务会直接命中本地文件、处理完即删。网络好 → 不用这个页面，保持流式。

---

## 13. 2026-11-04：多连接分段下载（IDM 式）

**动机**：国内单条 TCP 常被压到几百 KB/s，而一个分片 0.5~2 GB——单连接下
10 分钟起步，GPU 一直空转。HF 的 CDN 支持 `Range`，所以把一个文件切成几段
并发拉，是这一层唯一还能拿到的数量级提升。

### 13.1 分段计划（`plan_segments`）

- 段大小对齐 1 MiB，从 `total/conns` 起、不小于 `SEG_MIN_BYTES = 8 MiB`。
- `total < 16 MiB` 或 `conns == 1` 直接单段：小文件多开连接只会更慢
  （TLS 握手 + HF 的 302 都还没摊销完就结束了）。
- 段数上限 `SEG_MAX_CONNS = 16`，再多只会互相抢带宽。
- 服务端不认 Range（探测拿不到 206）→ 返回 `None`，调用方退回单连接。

### 13.2 落盘方式：稀疏文件 + 各段 seek 写

Windows 没有 `os.pwrite`，所以不能让多个线程对同一个 fd 做无锁定位写。
做法是**预分配 + 每线程独立句柄**：`truncate(total)` 先把文件撑到最终长度，
每个线程 `open("r+b")` 后 `seek(段起点 + 已下)` 写自己的区间。

为什么不用"每段一个临时文件再拼接"：拼接要再占一份完整空间（峰值 2 倍），
分段越多越容易把盘撑爆。稀疏文件方案的峰值就是 1 倍。

### 13.3 状态文件的必要性（这是最容易踩的坑）

预分配之后 `.part` 的长度**一开始就等于总长**，原来的"文件存在/长度==total
即完整"的判定直接失效。所以：

- 进度记在 `<name>.part.json`：`{total, segments: [[a,b]...], done: [...]}`，
  每 2 秒落一次（崩溃最坏丢 2 秒的量，而每块都写盘会让线程卡在 fsync 上）。
- 完成的唯一判据：`sum(done) == total`，然后 `rename` 并删状态文件。
- **有状态文件就必须继续走分段**，哪怕这次 `conns` 变了：段边界必须沿用旧计划，
  重新分段会让偏移和已有内容错位——这是最容易"看起来成功、文件是花的"的地方。
- 服务端中途不支持 Range 了（镜像切换）：删掉预分配文件 + 状态，退回单连接从头下。

### 13.4 取消与重试

- 取消 = `should_stop` 为真 → 抛 `HFCancelled`，**保留 `.part` 与状态文件**，
  下次点下载按段续传（每段自己的偏移）。
- 任一段失败 → `abort.set()` 让其余段立刻收手（不继续吃带宽），
  该段内部重试 `max_attempts` 次（2s/4s/6s 退避）。
- `ThreadPoolExecutor(max_workers=len(plan))`，线程名 `hf-seg`，便于排查。

### 13.5 参数与默认值

- CLI `--hf-conns`（默认 **4**，1 = 单连接，上限 16）；`iter_hf_shards(conns=)`
  透传给每个预取任务，所以"预取 N 片"与"每片 M 连接"是正交的两层。
- Web：新建任务页「并发连接」、下载页「并发连接」、`JobSpec.hf_conns`、
  `DownloadRequest.conns`、`DownloadItem.conns`（队列里显示"N 连接"）。
- parquet 走流式时不下载，故 `hf_conns` 只影响 tar 分片与下载页的整片预下载。

**实测口径**：单条被限到 ~300 KB/s 的线路，4 连接通常能到 1 GB/s 左右的下限
（受总带宽与 CDN 节点限制）；如果本来就跑满带宽，多连接不会更快，只会让
延迟抖动更明显——这也是默认 4 而不是 16 的原因。

---

## 14. 2026-11-04：打标管理（多 json → 单体 json）

**动机**：产物是"一张图两个文件"（`<id>-tag.json` / `<id>-nl.json`），
完成信号没有歧义、单条可重跑；但训练侧要的是"一条记录含全部字段"。
这一步以前靠手工脚本，现在给一个页面：扫目录 → 勾选 → 合并成一个大 json。

### 14.1 三个坑（`tagger/tag_manage.py`）

1. **按 key 配对，不是拼接**。直接 cat 得到两倍条数，一半没 caption、一半没 tag，
   训练侧读到空字段不报错，只会静默降质。
2. **字段冲突要计数**。`artist` / `provenance` / `ts` 两边都有且值可能不同；
   静默取一个等于编造数据，所以 `on_conflict`（first/last）+ `conflicts` 计数。
3. **单体 json 可能真的大**。写出是流式的：先写 `[`，逐条 append 并控制逗号，
   最后 `]`；把整表物化再 `json.dumps`，300 万条直接 OOM。
   合并本身仍需按 key 建索引，所以有 `MAX_RECORDS = 2_000_000` 的显式上限，
   超了给出"先分批合并"的提示，而不是半路 OOM。

### 14.2 输入/输出口径

- 输入按**内容**判断形态：JSON 数组 / JSONL / 单个对象（还认
  `{"records": [...]}` 这类一层壳的导出）。
- 输出 `format=full` 保留字段并集（人工核对、二次加工用）；
  `format=train` 只留 `image_id / image_path / caption / tag`，
  与 `merge.build_rows` 的总表口径一致。
- `reorder()` 把这四个字段提到最前：合并顺序取决于扫描顺序
  （`-nl.json` 排在 `-tag.json` 前），不调顺序的话第一条记录以 caption 开头，
  人工核对时连 image_id 在哪都找不到。
- 写盘一律 tmp + fsync + replace：写一半崩掉不会留一个"能被 json.load 读出
  大半"的文件。

### 14.3 接口与页面

- `GET /api/tag-manage/scan?dir=&recursive=`：只按文件名分类、不读内容，
  几万个文件也不卡；`empty` 单独标出来（截断的空 json 默认不进合并）。
- `POST /api/tag-manage/preview`：预览走**真合并逻辑**再取前 N 条——
  随便抽一行只会看到只有 tag 或只有 caption 的半条记录，反而误导。
- `POST /api/tag-manage/merge`：字段名/路径拼错在这里就是 400，
  输出扩展名不是 `.json/.jsonl` 直接拒。
- 页面 `TagManage.tsx`（侧栏「打标管理」）：目录 → 扫描 → 勾选（全选/清空/仅产物）
  → 合并设置（输出路径、完整/训练四项、JSON/JSONL、去重键、冲突策略、
  完整性过滤）→ 合并 → 报告（读入/去重/冲突/跳过/字段并集）+ 预览。

### 14.4 图片路径前缀（`image_root` / `image_ext`）

产物里的 `image_path` 只有 HF 模式才有（`hf://datasets/<repo>/<shard>#<key>`），
本地分发的产物根本没有这个字段，而训练侧必须知道图在哪。写死一个前缀等于把
"数据集放哪儿"焊进代码，所以做成合并参数：

- **留空 = 一个字段都不改**。接手一批已经能用的数据时，默认行为必须是什么都不动。
- 填了才覆盖成 `<root>/<image_id><ext>`（例：`/images/fanbox` → `/images/fanbox/画师-000123.jpg`）。
  尾部斜杠自动去重，扩展名不带点也能用，`/images/fanbox/` 与 `image_ext=png` 都认。
- `preview` 与 `merge` 走**同一个** `rewrite_image_path`：预览若与实际产物不一致，
  等于预览了个别的。
- 报告里记 `image_root`：过两周谁也记不住当时填的是 `/images/fanbox` 还是 `/mnt/d/pony`。
- **已知局限**：产物里没有 `rel_path`，所以 mirror 布局的子目录还原不出来，
  只能拼 `<id><ext>`。真要支持得先让产物带上 `rel_path`（会改变产物形态，
  老产物按"缺字段退回 id.ext"兼容），属于独立的一次改动。

### 14.5 五条增强（同日）

**1) mirror 子目录还原（产物带 `rel_path`）**

`run_batch` 写盘前注入：`if layout == "mirror": payload["rel_path"] = _rel_for_layout(task)`。
- 只对 mirror 写：flat 下子目录已经不存在，记下来反而会让合并侧拼出不存在的层级。
- **为什么不能"有 rel_path 就叠目录"**：`local_tag` 的 `image_id` 本身就是
  "去扩展名的相对路径"（`sub/001`），再叠一次 `rel_path` 的目录会得到
  `sub/sub/001.jpg`。所以 `rewrite_image_path` 先判断 id 里有没有 `/`：
  有就说明它是完整相对路径，直接用；没有才从 `rel_path` 取目录。
- `rel_dir_of` 拒绝三种"不算目录"的形态：空、带协议（`hf://`）、带 `::`
  （manifest 的 `zip名::成员` 老形态）。
- 老产物没有这个字段 → 退回扁平路径，向后兼容。

**2) parquet 产物输入**

`--artifact-format parquet` 的用户一个 json 都没有，只扫 `*.json` 等于这个页面
对他们不可用。现在 `scan` 同时收 `artifacts-*.parquet`（只收这种前缀：
数据集自己的 parquet 没有 `image_id` 列，收进来全是读不了的噪音）。
`_load_parquet` 把 tag / nl 两列的 JSON 字符串各展开成一条记录，交给**同一套**
按 key 合并——拆成独立逻辑等于把配对规则复制一份，两份一定会分叉。

**3) 路径自检（`check_paths`）**

`POST /api/tag-manage/check`：合并后逐条核对 `image_path` 指向的图在不在。
- 分类：`ok / missing / remote / empty`。`hf://`、`http(s)://`、`s3://` 归
  remote——那不是缺图，是"本来就不在这台机器上"。
- `local_root`：`image_path` 写的是训练机路径（`/images/fanbox/x.jpg`），本机
  实际在 `D:\ds\fanbox` 时填它做映射，不必为了自检去改产物。
- **必须先 rewrite 再查**：填了 `image_root` 时最终路径与产物原值不同，
  拿原值查等于白查（这个 bug 在测试里被抓到过一次）。
- IO 密集，用线程池（网络盘上串行查十万条会让人以为卡死）。

**4) 分批合并（分桶两遍法）**

不能"按文件分批、各自合并、再拼"：同一个 id 的 tag 与 nl 极可能在**不同
文件**里，分完就再也配不上对，结果是双倍条数、一半没 caption。
所以第一遍按 `crc32(key) % N` 分桶写 JSONL（同 key 必然同桶），第二遍逐桶
合并。用 crc32 而不是内置 `hash()`：后者带进程级随机化，同一批输入跑两次
会分到不同桶，输出顺序就不可复现。
- 触发：`batch_size > 0` 强制；`0`（自动）时估算超过 30 万条才走。
- 代价：**输出按桶序排列，不再全局按 image_id 排序**。报告里 `batched/buckets`
  写明，页面上也提示——不让它成为隐藏差异。
- 读不了的文件照旧计入 `bad_files`；临时目录在 `finally` 里删干净。

**5) 合并改后台任务（`web/tag_merge_tasks.py`）**

`POST /merge` 立刻返回 `task_id`（202），`GET /merge/{id}` 轮询进度
（`read` 读入 N 个文件 / `write` 写出 N 条）。不复用 `jobs.py`：那是给打标
任务用的，有阶段、GPU 保护、历史落盘、重启恢复——合并是一次性 IO，重启后
重跑即可，背这套约定是纯负担。已结束的任务只留最近 20 个（刷新页面能看结果，
不当历史用）。

**顺带修的**：`NewJob.tsx` 的「推理来源」UI 曾整块丢失（提交时仍在传
`vlm_provider / api_base / api_key`，等于外部 API 模式在页面上没法用），
`npm run build` 的严格 tsc 会因此直接失败。已按 `defaults.vlm_providers`
恢复下拉 + API 模式下的地址/密钥输入。

---

## 15. 2026-11-04：NL 打标支持外部 API（OpenAI 兼容 / Anthropic）

**动机**：本机推理要显卡、要下 4B/8B 权重、首次加载几分钟。手上没有合适的卡
（或只想少量出样）时，应该能直接用云端模型把同一批 NL 跑出来。

### 15.1 方言收在 `tagger/nl_api.py`

主循环 `client._infer_at` 里原本硬编码着 vLLM 的形状（`/v1/chat/completions`
+ `choices[0]` + `completion_tokens`）。加 API 时如果就地 if/else，两套形状会长在
推理主循环里；重试、升级、计数各自还要再分一次支。所以整层拎出来：

| 差异点 | 本机 / OpenAI 兼容 | Anthropic Messages |
| --- | --- | --- |
| 端点 | `{base}/v1/chat/completions` | `{base}/v1/messages` |
| 鉴权 | `Authorization: Bearer <key>` | `x-api-key` + `anthropic-version` |
| 系统提示 | `messages[0].role=system` | 顶层 `system` 字段 |
| 图片 | `{"type":"image_url","image_url":{"url":data_url}}` | `{"type":"image","source":{"type":"base64",...}}` |
| seed | 支持 | **不支持**，带上就是 400 |
| 取文本 | `choices[0].message.content` | `content[]` 里 `type=text` 的拼接 |
| token | `usage.completion_tokens` | `usage.output_tokens` |

三个易踩点：

1. **端点会拼出 `/v1/v1/...`**。用户从文档复制来的地址多半已经带 `/v1`
   （`https://api.openai.com/v1`），老代码无脑 `+ "/v1/chat/completions"`。
   `nl_api.endpoint()` 现在认三种输入：裸域名、带 `/v1`、完整端点（原样用）。
2. **Anthropic 的 `max_tokens` 是必填**，而且没有默认值；不传直接 400。
3. **Anthropic 只收 jpeg/png/gif/webp**。bmp/tiff 在 `blob_from_bytes` 里先转 PNG，
   否则批量任务里表现成"随机一半图失败"，错误只说不支持该 media_type。

### 15.2 API 模式的三个前提变化

- **不读指纹**：`run/fingerprint.json` 是 `tagger serve` 写的，API 模式根本不起服务。
  `jobs._run_vlm` 传空指纹，`run_client` 就地造一个 `{provider, model_id, precision:"api"}`。
  顺带 `max_pixels` 留空 = 服务端不钳制，重试提分辨率不会被钳回基线。
- **不认 `file://`**：云端读不到本机路径。任务创建时直接 400（而不是跑起来每张 400），
  客户端 `TaggerClient` 里还会再兜一次；页面上这个开关在 API 模式下不渲染。
- **模型名必填**：本机的 `tagger` 是 `--served-model-name` 路由名，API 侧不存在。
  `create()` 里拦，页面上按钮置灰。

### 15.3 溯源与缓存

- `Provenance` 新增 `provider`（`local` / `openai` / `anthropic`，**带默认值**
  `local`）。给默认值是为了让旧产物还能读回——新增必填键会让历史 jsonl 在读回时
  TypeError。同一个 model_id 在本地与云端是两套输出，混批之后只能靠这一项分辨。
- 结果缓存的指纹也加了 `provider`：否则"本机 qwen3vl-8b"与"云端同名模型"会互相命中。
- `api_key` 不进产物、不进快照：`_redacted_spec` 抹成 `***`（与 `hf_token` 同口径）。
  密钥来源优先级：表单/`--api-key` > `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`。

### 15.4 入口

- CLI：`tagger client --api-provider openai|anthropic --api-base ... --api-key ...`
  （`--api-base` 留空按来源取默认域名：`https://api.openai.com/v1` /
  `https://api.anthropic.com`；OpenAI 兼容那一支也覆盖 LM Studio、one-api，
  key 可以留空）。
- Web：`JobSpec` / `JobRequest` 加 `vlm_provider` / `api_base` / `api_key`；
  `/api/defaults` 的 `vlm_providers` 给来源清单、默认地址与建议的环境变量名——
  前端不留第二份地址表。
- 页面：新建页 NL 模型面板第一行是「推理来源」。选 API 后本机整块（启动/引擎/
  分档/用量/放行目录/探测）不渲染，换成「API 地址 + API 密钥」；模型名一格共用，
  文案随模式变。右上角状态芯片在 API 模式下显示来源，不再挂本机服务状态。
- 升级模型同样吃这套方言（另一个地址 + 同一把密钥），产物仍记 `escalated`。

---

## 16. 2026-11-04：云端跑批的四个坑（限流/熔断/预检/成本）

§15 让 API 模式能跑，这一节让它在"真的按量付费、真的会被限流"的环境下
不把事情做坏。四条都是上线前就该有的，不是优化项。

### 16.1 错误分三档，不再"400 以上一律不重试"

`_infer_at` 原来把 4xx 全判成不可重试。本机这样写没问题（本机不会限流），
云端等于把最常见的 429 当成永久失败——一整批在图省钱的窗口里全变废品。
`nl_api.classify_status()` 现在分三档：

| 状态 | retryable | fatal | 处理 |
| --- | --- | --- | --- |
| 429 / 529 / 408 / 409 / 425 / 5xx | 是 | 否 | 退避重试，**优先用 `Retry-After`**（上限 60s） |
| 401 / 403 / 404 | 否 | **是** | 这张失败，并且连续 20 条就中止整批 |
| 其它 4xx（400 内容策略等） | 否 | 否 | 只失败这一张 |

`fatal` 是新增字段，专指"与具体图片无关的配置错误"。401 的语义是"key 不
对"，不是"这张图有问题"——两者的区别决定要不要停。

### 16.2 全局熔断：20 条连续配置错误就收工

`_FATAL_BREAK = 20`。成功一张就清零（偶发一张 400 不该被当成密钥错误）。
触顶直接抛 `Stopped`，`run_client` 的处理器会按消息前缀给不同的提示——
之前那句"阈值在 tiers.yaml"对 API 场景完全是误导。

为什么值得单独做：地址少写一个 `/v1` 时的默认表现是**一张张走完全部请求、
一张张失败**，进度条跑到 100% 才告诉你全灭。有了熔断，第 20 张就停。

### 16.3 预检：`POST /api/nl-api/probe`

发一张 8×8 的 PNG 走一次真实调用，一次拿全：地址通不通、密钥认不认、
模型名存不存在、**图看不看得见**（视觉能力）。返回 `latency_ms` 与
`prompt/completion_tokens`，顺带就是一次成本采样。

- 页面：API 地址那一行右边「测试连接」，结果内联显示（`通 · 1234ms · 入 800 / 出 9 token`）。
- 本机模式仍走 `/api/vllm/probe`（看进程与 `/v1/models`），两条路别混。
- 探测永远返回结构化 dict，连不上也是 `ok:false + error`，不抛 500。

### 16.4 成本：输入 token 才是大头

`parse_response` 的返回从 `(文本, 输出token)` 变成 `(文本, 输入token, 输出token)`。
不这么改就只能算到输出那一半，而图片 base64 撑大的 prompt 通常是输出的几十倍。

- 产物：`prompt_tokens` 与 `completion_tokens` 并列（本机模式为 0 时**不写这个
  字段**，老产物形态逐字不变）。
- 汇总：`ClientStats` 加 `prompt_tokens` / `rate_limited` / `cost`，
  `summary_lines()` 多三行（限流次数与费用只在非 0 时出现）。
- 快照：`Job.snapshot()["usage"]` 带同样四项，运行页多两个指标（输入 token、
  被限流），`cost` 非 None 时再加一个「估算费用」。
- **价目表故意默认为空**，放在 `configs/nl_api.json`：

  ```json
  { "prices": { "claude-sonnet-4-5": [3.0, 15.0], "gpt-4o": [2.5, 10.0] } }
  ```

  单位是「每 100 万 token」的 `[输入, 输出]`，按 model_id **前缀**匹配
  （`gpt-4o-2024-08-06` 命中 `gpt-4o`）。查不到就 `None`：宁可只报 token，
  也不编一个数——算错的成本比不报更误导人。价格按月变，所以不内置。

### 16.5 密钥存档与按来源的默认值

- `configs/nl_api.json` 的 `providers` 段：`{provider: {base, key}}`，明文，
  写盘后 `chmod 600`（Windows 上无操作）。页面勾「记住到本机」才落盘，
  取消勾选立刻删除——不留"看不见但还在盘上"的密钥。
- **下发永远是脱敏值**（`sk-a…1234`）：`/api/defaults` 与 `/api/nl-api/keys`
  都只给 `key_masked`，浏览器拿不到明文。
- 每个来源带自己的默认值，不再一套参数打天下：
  `DEFAULT_MAX_PIXELS`（Anthropic ≈1568 长边 / OpenAI detail=high 量级）——
  沿用本地 1024 基线等于白付像素钱还降质；`DEFAULT_CONCURRENCY`（anthropic 2 /
  openai 4）——云端限的是每分钟请求数，与显存无关；`MODEL_HINTS` 只喂
  `<datalist>`，写错模型名的报错是 404，列表不可能穷举账号里的模型。

### 16.6 本节的测试

`tests/test_nl_api.py` 现有 29 条，新增：三档分类、`Retry-After` 解析
（大小写都要认，测试替身给的是普通 dict）、成本前缀匹配与"查不到返回 None"、
密钥存档往返 + 脱敏、429 记 `retryable` 且不计 fatal、401 记 fatal、
`run_batch` 熔断在阈值处停下（1000 张的任务 `seen <= 40`）、
三个新接口（probe 拒绝本机 / 探死端口、keys 不回明文、defaults 带来源元数据）。

## 17. 2026-11-04：底部全局状态条 + 自检转后台 + 计划任务

### 17.1 自检不再是同步请求

`check_paths` 加 `on_progress(stage, done, total)`：`read` 是读入第几个文件，
`check` 是核对到第几条（每 64 条刷一次，避免把锁竞争当主要开销）。端点改
`202 + task_id`，复用 `tag_merge_tasks`（给它加了 `kind` 字段区分 merge/check）。
理由与合并相同：几十万条在网络盘上是分钟级，挂在 HTTP 里就是转圈 + 切页丢掉。

### 17.2 `/api/activities`：一种形态装所有在跑的活

打标（`JobManager`）、数据打包（`DataJobManager`）、合并/自检（`tag_merge_tasks`）
各自有状态，这里只做归一化：`kind/title/detail/done/total/progress/rate/eta_s/href`。
前端不按业务分支，只负责画。只返回进行中的条目，底部条要么有内容要么不占位。

### 17.3 硬件读数（`web/sysinfo.py`）

不装 psutil：CPU 使用率用系统累计计数器做差（Windows `GetSystemTimes`、
Linux `/proc/stat`），差值就是两次轮询之间的平均占用；内存走
`GlobalMemoryStatusEx` / `/proc/meminfo`（Linux 用 `MemAvailable` 而不是
`MemFree`，否则缓存会被算成已用）；GPU 复用 nvidia-smi（利用率/温度/显存），
缓存 2 秒（底栏最快 1 秒一次，不缓存等于每秒拉一个进程，开销会算进 CPU 占用里
自己抬高自己）。

**CPU 温度不显示**：Windows 没有标准的用户态接口，psutil 在 Windows 上的
`sensors_temperatures()` 也直接返回空；Linux 的 thermal_zone 常常报的是主板/
电池而不是 CPU 核心。一个"经常是 --、偶尔还是错的"读数只会让人怀疑服务挂了。

### 17.4 计划任务（`web/plan.py`）

跑到 N 张后停任务 / 关机。状态写 `run/plan.json`，服务重启后仍在；lifespan
里主动 `plans()` 把线程拉起来，否则"设了计划、关页面、重启服务"就永远不触发。
- 先钉 `fired` 再动作：关机返回后进程可能立刻没了，晚写这一行等于下次再关一次。
- 关机留 60 秒缓冲（`shutdown /t 60`，`/a` 可取消）：停止任务后落盘不是瞬时的。
- 关机在页面上二次确认；`dry_run` 供测试。

### 17.5 底部条与设置

`AppShell` 新增 `statusBar`（主区下方全宽，无内容不渲染）。左侧任务进度，
数量统计用 `|` 隔开；右侧 `CPU 13% | GPU 36% 34° | 内存 40% | 显存 7% | 计划`，
内存/显存的 GB 明细放悬浮提示（底栏不够宽，百分比已经够看出"要不要停下来"）。
刷新频率在「设置」页（`/settings`），默认 5s，存 localStorage；选「暂停」后
不再轮询，但硬件与计划入口保留，随时能取消自动关机。


## 17. 预处理上卡（2026-11-04）

打标的 `_decode_batch` 以前一律是 PIL：解码 + timm 的 Resize/Normalize 全在 CPU。
现在多了一档 `preprocess`，可选把**缩放/归一化**搬上卡；**解码仍然留在 CPU**。

为什么解码不搬：生产里是逐张喂，nvJPEG 单张 15.2 ms 比 PIL 的 11.3 ms 还慢，
而且解码完还得回 PIL 做 aug（批 8 实测 0.67×，净亏）。真正贵的是 transform 那 10 ms。
数在 `devtools/perf-baseline.md` §14。

### 17.1 怎么开

- UI：新建任务 →「模型与性能」→「预处理」→ `GPU`（默认 `CPU`）
- CLI：`tagger tag/pack --preprocess tensor`、`tagger client --tag-preprocess tensor`

### 17.2 什么时候该开

墙钟 ≈ `max(CPU 预处理, GPU 前向)`，所以只有 CPU 侧明显更重才赚：
1024² PNG 1.47×；1024² JPEG 1.08×；768×512 PNG 1.06×。

**代价**：重采样从 PIL bicubic 换成 `F.interpolate` bicubic+antialias，是两套核。
像素差 p99 约 1 个灰阶，但阈值附近的 tag 会翻：平滑图实测 40/40 完全一致，
线稿/合成/超小图 15/25 有改判（平均 IOU 0.921）。所以默认不开，线稿类素材别开。
`tag_preprocess` 已进 provenance，事后能查这批是哪种口径出的 tag。

### 17.3 注意

- `preprocess` 已进 `config_fingerprint`：切了档再重跑会真的重算（不进指纹的话
  会被"已有产物"全部跳过，看着像没生效）。
- `_tensor_prep_plan` 是**从 timm 建好的 transform 里读**参数；认不出来
  （RandomResizedCrop / LANCZOS / 无 Normalize）就回落 PIL 并打一行日志，
  不会硬套一套近似参数。
- tensor 模式下 H2D + 缩放 + 归一化算进 `forward` 计时（占的是前向那条流），
  `decode` 只算 CPU 解码。看 `tag_decode` 变小时别以为解码变快了。
- 目前只有 `wd-eva02` 实现；其它后端收了这个参数也不生效。

### 17.4 校验

`pytest tests` 通过（`tests/test_web_stats.py::test_run_vlm_feeds_stats_buckets` 与
`tests/test_web_features.py::test_run_vlm_bridges_run_client` 是**既有**失败：
`_run_vlm` 传了 `paused=`，测试里的 fake 签名还没有这个参数，与本次无关）。
`cd tagger/web/ui; npm run build` 通过（tsc + vite，dist 已更新）。
