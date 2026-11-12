# 打标器交接文档

> 最后更新：2026-11-07（§24 jsonl 产物形态）
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
- **历史任务落盘**（2026-11-08）：每条任务写一份 `run/history/<id>.json`，
  内容是 `Job.snapshot()` 原样存——`Job` 握着锁/线程/进度表，序列化不了。
  列表 = 内存里的活任务 + 盘上的历史，同 id 以内存为准，各取最近 20 条。
  重启时会把还写着 `running/pending/paused` 的记录改写成 `stopped` 并注一句
  原因：不改的话它会一直占着"已有任务在跑"的位子，新建任务永远 409。
  历史条目带 `historical: true`，前端据此把「进度 / 结果」置灰（那两页要读
  `Job.reader`，进程没了就没有），「继续」仍然可用——`restart` 从历史取 spec
  重建，只认 `JobSpec` 现存的字段，旧版本多出来的键会被忽略，不会报"不兼容"。
- **新建任务页记住上次填的**（2026-11-08）：`configs/ui-form.json` 存两份。
  `form` 是页面 state 的逐项快照（键名与 useState 同名），**改一项防抖 700ms
  写盘**，所以中途关掉程序也不丢；`spec` 是提交成功时的 JobSpec，字段语义
  跨版本稳定，用来兜住旧记录。恢复顺序 **spec 先、form 后**：form 是实时快照，
  一定比上次提交的更新，让它压在上面；form 里没有的键再由 spec 补。
  为什么不只存 JobSpec：它要等提交才存在，而逐项保存在输入过程中就要写；
  从 state 现造 JobSpec 得复制 submit 里那一百多行分支，迟早对不上。
  两份都剔掉 `api_key` / `hf_token`（凭据有专门存档）。
- **暂停时强制落盘**（`_flush_sink`）：parquet 产物默认攒满 2000 行才写一片，
  不在这里 flush 的话，用户按完暂停就关窗口，界面上的「已完成 300 张」会随着
  关闭凭空消失、下次还要重打。json 布局逐张写，不受影响。
- 测试隔离：`TAGGER_RESUME_PATH` 环境变量可改落盘位置，`tests/conftest.py`
  把它指到系统临时目录——否则跑一次 pytest 就会把用户页面上的"继续上次任务"
  顶成一条指向 pytest 临时目录的假记录。

代码：`tagger/client.py` 的 `run_batch(paused=...)`、`tagger/web/jobs.py` 的
`pause / resume / resume_last`、`tagger/web/ui/src/components/ResumeBanner.tsx`。

### 依赖检测（侧栏「依赖检测」）

把 `requirements-embedded.txt`（运行环境）与 `requirements-tag.txt`（打标运行时）里的
每一条列成一行，外加一组"可选功能"（onnxruntime / datasets / vllm），可以**单独装、
卸、重装任意一个包**，不必为了一个包重跑 install.bat。

- 清单**现读现算**，页面不存版本表：改了 requirements 刷新就能看到，也不会出现
  "页面说装了、实际没装"这种最难查的错觉。
- 三种状态：已装（绿）/ 缺失（红）/ 版本不符（黄）。**一致时不重复显示版本**
  （包名旁已有 `==2.5.4`），只有对不上才并排 `实装 1.0.30 → 清单要求 1.0.11`。
- 装走 `sys.executable -m pip`，不是 `pip install`：嵌入式环境里 PATH 上的 pip
  很可能指向别的 Python，装完这个环境里什么都没有。
- **torch / torchvision 自动走 PyTorch 索引**（有 nvidia-smi 用 cu124，没有用 cpu）。
  PyPI 上没有它们的轮子，指错了会以"找不到满足要求的版本"收场。
- **同一时刻只允许一个 pip 动作**（`DepManager` 串行 + 409）。两个 pip 同时改一个
  site-packages，会各自读到对方写到一半的目录，最后炸在一个完全无关的包名上。
- **有任务在跑时拒绝装卸**（409）：跑批每隔几秒就要 import 图片解码、pyarrow，
  装到一半的东西会让它以"找不到模块"崩掉，而那种报错看起来像代码 bug。
- `卸载` / `重装` 前端要二次确认：重装的语义是"按清单版本装回去"，本机手动装过
  更新的 torch 时，这一下会把它降级并重下 2.5GB。
- pip 输出逐行进日志（400 行环形），页面每秒轮询；装 torch 的十几分钟里，
  没有输出是最难熬的。可中途取消（terminate 子进程）。
- **附加依赖（pyproject 的 extra）也在这页**：`convert`（本地量化 GGUF）、`quantize`
  （4bit 量化）、`parquet`、`serve`，每组一个分区，组头带「安装本组」（只装这一组
  缺的）。它们原来只长在各自的功能弹窗里，想"先把环境配齐再干活"的人在这一页
  看不到还缺什么——而这一页是唯一能回答"整台机器缺什么"的地方。
  **但它们不进「安装缺失」那个批量**：`serve` 在 Windows 上装了也起不来、
  `llmcompressor` 几百 MB，一次全装就是误伤。
- 附加依赖组里与主清单重名的包（torch / transformers / pyarrow…）挂一个「共用」
  小标：两组改的是同一份 site-packages，在组里点「卸载」会把打标一起弄停。

**重依赖（`heavy`）不能跟着批量装**。vllm 实测会拖进 80+ 个包，并把 numpy
2.5.4→2.2.6、starlette 1.10→1.7、fastapi 0.162→0.136、transformers 4.x→5.17。
用户点「安装缺失」时想的是补几个小包，结果整套环境被掀翻，界面的 fastapi
依赖也不再是清单版本。所以：`install-missing` 会把 heavy 项标成 `skipped`
（看得见、但不动），单独点它那一行的「安装」才行，点之前有二次确认。
`vllm` 的判定就在 `_OPTIONAL` 里的 `heavy=True`。

**pip 的冲突要单独捞出来**：`X requires Y, but you have Z` 这类行**不改退出码**，
pip 照装、返回 0。不捞的话用户只看到一个绿色的"完成"，而 numpy 已经被降级了。
`_pip` 把这类行收进 `conflicts`，面板上用黄色列出。

**「这不是 tagger 自带的 env」**：`_python_kind()` 比较 `sys.executable` 是否在
`ROOT/env` 下。系统 Python 里往往还装着 gradio / numba / scipy，在那里装卸依赖
改的是别人的环境——vllm 那次事故就是在 `C:\Users\...\Programs\Python\Python310`
里发生的。external 时页面顶部挂一条黄条。

**顶部的三个批量按钮**（`install-missing` / `uninstall-all` / `verify`）都走
`_start_many` 这一条队列，`batch` 字段里逐项报进度，失败项在面板上单独列出来：

- **安装缺失**：只装 `missing`，**不碰 `mismatch`**。把版本不符的包装回清单版本
  等于降级（开发机手动装过更新的 torch 时尤其明显），批量做这种事太容易误伤；
  要降级请单独点那一行的「重装」。
- **卸载全部**：只卸清单里、且确实装着的包，清单外的不动。前端强制二次确认——
  卸掉 fastapi/uvicorn 之后这个页面看着还在（模块已加载），下次启动就没了。
- **检查完整性**：逐个在**子进程**里 `import` 一次，不是看 dist-info。
  `dist-info 在` 和 `包能用` 是两件事：下载中断留下半个目录、杀软删掉某个 .pyd、
  CUDA 版 torch 缺 dll，这些情况下 `version()` 照样返回值，页面一片绿，真跑
  打标才炸。用子进程而不是本进程 importlib，是因为 torch 会往服务进程里塞几百
  MB 的库且再也退不回来；子进程还能超时 kill（`VERIFY_TIMEOUT = 60s`）。
  结果落在 `DepManager.verify`（内存），行上以红字覆盖显示「导入失败 · 原因」。
- **分发名 ≠ 导入名**：`open_clip_torch` 的真实模块是 `open_clip`，`Pillow` 是
  `PIL`。少了映射，装好的包会一直红着，用户只会去反复重装。`Item.modules` 会
  给出候选：手写别名表 → `packages_distributions()` 反查 → 连字符换下划线。
  **但只在错误是 `No module named '<这个名字>'` 时才换下一个**：模块存在、
  内部炸了（`_C` 没编译、DLL 缺）是真实故障，换个"兄弟模块"往往能 import
  成功，把故障掩盖成通过（torch 坏了而 torchgen 好好的，就是这种坑）。

代码：`tagger/web/deps.py`（清单解析、状态判定、pip 子进程）、
`tagger/web/ui/src/pages/Deps.tsx`。

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

### 本地量化成 GGUF（模型管理页「本地量化」）

默认引擎 llama.cpp 只吃 GGUF，而**下载一律只下原始权重**（`path` 指的
transformers 仓库，整包 safetensors）。网上现成的 GGUF 一份都不下——同一份
权重两种来路，产出漂了根本查不出是哪一环。所以流程固定两步：

1. 模型行点「下载原始权重」→ 整仓库落到 `models/nl/<key>/`
2. 点「本地量化」→ 选 Q4_K_M / Q6_K → 开始
3. 完成后产物自动登记进 `configs/models.json` 的 `gguf_quants`，新建任务页选同名档位即可

- 脚本用的是 llama.cpp 官方 `convert_hf_to_gguf.py`。它**内置在 `vendor/llama-convert/`**
  （脚本 + `conversion/` + `gguf-py/`，约 2 MB 纯 Python，随发布包走），所以第一次
  转换不用联网；内置那份被删/残缺才回落到 `runtime/llama.cpp/convert/`，再不行才去
  GitHub 取源码包。**不要自己手改**那几份文件
- 依赖是 `convert` 这个 extra（torch + transformers），页面能一键装
- 视觉模型会跑两趟：文本塔写 `--outfile`，第二趟加 `--mmproj`（它只是开关，
  输出名自动加 `mmproj-` 前缀）
- 中间的 F16 跑完就删：它比成品大两三倍，留着只会让 `models/` 莫名其妙地涨
- 代价说清楚：比直接下现成 GGUF 慢得多（0.5B 约 25 秒，8B 十几分钟起），
  磁盘峰值约两倍模型大小

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
解释器 + pip wheel，约 12MB）、`vendor\llama-convert\`（GGUF 转换脚本，约 2MB）、
根目录脚本与 `Tagger.Shell.exe`。

**永远不会包含**：`models\`（42GB）、`work\`、`run\`、`.pytest_cache`、`node_modules`、
`dist\desktop`、`env\python`（除 `-WithEnv`）、**`configs\models.json`**。

`configs\models.json` 是**接收方自己的**覆盖文件（加过的模型、改过的下载目录、代理、
本地量化登记进去的 `gguf_quants`）。它是本机数据，跟着包发出去等于"升级一次就把他们的
配置清空"（我们这边通常只有 `{}`）。`configs\ui-form.json`（新建任务页"上次填的那套"：逐项快照 `form` + 提交过的
`spec`）同理，也一起删。所以 `pack-dist.bat` 拷完 `configs\` 会把这两个删掉，只发 `tiers*.yaml`。
同理：**升级只覆盖代码与默认配置，权重、任务断点与历史（`run\` 整个目录）、产物目录
都不在包里，解压不会影响他们跑到一半的活**。

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
| `artifacts.py` | 成员产物分片：parquet / jsonl 两个 sink + 读侧（见 §24） | 完整 |
| `distribute.py` / `merge.py` / `verify.py` / `report.py` | 分发包、回收对账、完整性复核、批次报告 | 完整 |
| `hf_source.py` | 从 HF 拉 tar/parquet 素材 | 完整 |
| `web/app.py` | FastAPI：任务、预览、目录浏览（含 `.zip`）、缩略图（含 zip 成员）、事件流 | 完整 |
| `web/jobs.py` | 任务簿 + 两阶段（打标→封包）+ 协作式中止 + 事件流；VLM 模式映射 `vlm_backend_for` | 完整 |
| `web/vllm_service.py` | 本机 vLLM 启停/状态/日志尾巴/指纹写入（`/api/vllm*`） | 完整 |
| `web/packaging.py` | 分包/拼接/封包的后台任务（`/api/datajob*`） | 完整 |
| `web/models.py` | **模型清单**：`/api/models` 读写 `configs/models.json`（只存与 tiers.yaml 的差异），下拉与 CLI 共用同一份合并结果 | 完整 |
| `web/downloads.py` | **权重下载**：流式下载 + 进度/网速 + 按文件断点续传 + 取消 + 代理（http/socks5）；删除只允许 `models/` 之内 | 完整 |
| `gguf_convert.py` | **本地量化成 GGUF**：原始 safetensors → `convert_hf_to_gguf.py`（内置在 `vendor/llama-convert/`，缺了才回落下载）→ F16 → `gguf_quant` 压成 Q4_K_M/Q6_K → 登记回覆盖文件。视觉模型多跑一趟导出 mmproj | 完整 |
| `gguf_quant.py` | GGUF 再压（自研 numpy，Q4_K/Q6_K，输入必须是 F16/BF16 的 GGUF） | 完整 |
| `netutil.py` | 回环地址判定：本机请求一律不吃系统代理（代理软件会拦 127.0.0.1） | 完整 |
| （外部）`fox-label worker.py` | **分发包派工/心跳/租约**由同事的服务端项目负责，不在本仓库；我们只提供 `client --progress-file` 这一条出口（见 §37） | 外部 |

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
- **壳子的端口必须固定 27100**：WebView2 的 localStorage 按 origin（含端口）隔离，
  随机端口等于每次换站点，主题/上次任务/轮询间隔这些本机偏好会集体失效。
  `PythonServer.PickPort()` 先试 27100，被占用才退回系统分配
- 输入容器：「读取包内图片」认 `.zip` 与 `.tar` / `.tar.gz` / `.tgz`（HF 上下来的 webdataset
  分片就是 tar）。取图统一走 `pack.ContainerReader`（按容器缓存句柄 + 一把锁）——
  tar 每张重开一次等于全包重扫，慢到像卡死。判断"是不是包内成员"只用
  `InputItem.is_member`，别再写 `kind == "zip_member"`
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
- NL 提示词从 `prompt/*.json` 选（文件名=界面显示名，中文照原样），页面可编辑/新建/打开目录；
  字段见 `prompt/README.md`。**整段替换**内置的那份（不追加），`provenance.prompt_name` 只作备注，
  同源判定仍看 prompt 文本的 sha，所以改名不会让历史产物被判成"参数变了"
- `prompt/默认提示词.json` = 内置提示词在**默认产出规格**下的静态快照，随包发出去当底稿；
  它是死文本，不会跟着「描述长度」「中文比例」变，那两项只对「内置（按参数生成）」生效。
  `pack-dist.bat` 的拷贝白名单里已加上 `prompt\`（漏了会在打包输出里 WARNING）
- 模型权重可在「模型管理」页直接下载：每行一个目录（默认 tag=`models/<后端>`、
  NL=`models/nl/<key>`），进度/网速靠轮询 `/api/models` 的 `download` 字段；
  下完变成「删除 / 打开位置」，取消后留下的半截文件会标记 `partial` 并支持续传。
  **删除有守卫：只允许删 `models/` 之内的路径**，防止 local_dir 填成素材目录。
- 下载代理在「模型管理」页底部，存 `configs/models.json` 的 `proxy` 字段，
  支持 `http://` / `socks5://`（只写 `host:port` 会按 http 处理）。
  **只作用于这页的下载**：`vllm serve` 自己拉权重仍走系统 `HTTP_PROXY/HTTPS_PROXY`。
  socks5 依赖 `socksio`（已装并锁进 requirements-embedded.txt）；
  `HF_ENDPOINT` 换镜像仍然有效，两者可以叠加。
- **默认口径已切到无审查版**（2026-11-07）：`qwen3vl-4b-abliterated` /
  `qwen3vl-8b-abliterated`（prithivMLmods 的 Unredacted-MAX）进全部三档，
  官方 `qwen3vl-4b/8b` 降为对照与回退。原因是训练 diffusion 的图集里暴露内容
  占比不低，原版遇到 NSFW 直接拒答，表现是"整张只出几句干话、一个 tag 都没有"
  且不报错——静默漏标。
- **下载只下原始权重**（2026-11-08 定）：VLM 的下载入口不再分档，一律整包下
  `path` 指的 transformers 仓库（4B 8.27G / 8B 16.33G，单文件 safetensors）。
  网上现成的 GGUF 一份都不下，`download_repo` / `download_files` 两个字段
  已从配置和覆盖文件白名单里移除——留着就是一条"从网上拿成品量化权重"的后门。
  量化档只能由「本地量化」压出来，所以 `gguf` / `mmproj` / `gguf_quants` 全部
  指向 `models/nl/<key>/` 下的本地产物（命名见 `gguf_convert.default_outputs`），
  没压之前启动会明确报缺哪一份。
  ★覆盖文件是整体替换：`quantize._register` 写回时必须把 gguf / mmproj /
  gguf_quants 一起抄过去，漏一个就会让默认引擎报"没配 GGUF"。
  量化默认对象跟着分档走（`_model_source(None)` → resolve_profile），所以三档
  改了模型，量化默认也就是破限版，不需要改代码。
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
- NL 推理默认引擎是 **llama.cpp**（`--engine vllm` 才走 vLLM）。没有
  `llama-server` 时：网页上有「下载并安装 llama.cpp」按钮，命令行是
  `tagger llama install`（`--zip` 喂离线包），或设 `TAGGER_LLAMA_SERVER`。详见 §18
- 测试：`pytest tests -q` → 558 passed

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

`pytest tests -q` 全绿（558）。上一版的"既有失败"已收口：`_run_vlm` 传 `paused=`
之后，`tests/test_web_stats.py` 与 `tests/test_web_features.py` 里的两个 fake
签名没跟上，补上即成——替身必须照抄完整签名，否则岗位里的 `TypeError` 会被
当成任务失败，测试报的是 `seen["args"]` KeyError，离真因隔了一层。
`cd tagger/web/ui; npm run build` 通过（tsc + vite，dist 已更新）。

---

## 18. 2026-11-04：默认引擎改 llama.cpp + 网页一键装 llama-server

原来默认是 vLLM，理由是"引擎决定产出算什么口径，不能按平台偷偷替你选"。
落地下来这条站不住：主力场景是 Windows + 单卡，而那里 vLLM 连起都起不来
（官方只有 Linux 版，要 WSL2），默认值等于把"第一次使用必然失败"写进了默认。

### 18.1 默认值只留一处真相源

空值时 `nl_engine.normalize_engine()` 返回 `llamacpp`。原来 `cli.py`、
`model_loader.serve()`、`vllm_service` 各自写了一份 `"vllm"` 兜底，改默认
必然漏一处——所以那三处改成调同一个函数，不再抄字面量。

要 vLLM 就显式 `--engine vllm`。引擎进指纹，两种引擎的数据不会混批。

### 18.2 llama-server 不是 pip 包

新增 `tagger/llama_runtime.py`：

- 查找顺序：`TAGGER_LLAMA_SERVER`（显式钉版本）> `runtime/llama.cpp/**`
  （本程序装的）> `PATH`。装完**不改 PATH**：改进程的 PATH 对已经跑着的
  解释器无效，写系统环境变量更过分。
- 安装包从 GitHub `releases/latest` 挑：有 N 卡优先 `-bin-win-cuda-`，其次
  vulkan（无 CUDA 时唯一还能吃 GPU 的），最后 cpu；CUDA 主包会连带下同版本
  的 `cudart-*.zip`——缺它 exe 起来只报"缺少 dll"，跟 llama.cpp 看不出关系。
- 下载走 `.part` 再改名（断掉不留"看着像装好了"的半包），解压逐成员校验
  落点（挡 `../`），装完删 zip。
- `status()` 刻意不联网：页面每几秒轮询，摸 GitHub 会让面板跟着卡。

### 18.3 三条路，一份实现

| 入口 | 用法 |
| --- | --- |
| 网页 | 引擎选 llama.cpp 且没有 llama-server 时出现「下载并安装 llama.cpp」 |
| CLI | `tagger llama install`；`--zip <路径>` 喂离线包/镜像，`--force` 换构建 |
| 环境变量 | `TAGGER_LLAMA_SERVER` 指向现成构建 |

入口不同、落点同一个，不会出现"网页装好了、CLI 仍说没装"。

### 18.4 两个必须记的坑

- **`install()` 不能在持锁时调 `status()`**。两者都要同一把 `threading.Lock`，
  不可重入 → 点按钮接口永不返回（页面转圈、日志什么都没有），单测里表现是
  整条命令挂死而不是报错。现在临界区只负责"占位 + 置初值"，返回值在锁外拼。
- **装完要让能力判定失效**。`local_capability` 有 TTL 缓存，装好那一刻它
  还记着"找不到 llama-server"，页面会继续显示起不了服务。`/api/llama` 发现
  已安装就调 `VllmService.invalidate_capability()`（只清"判过不能用"那格，
  否则每轮询一次就白跑一次 nvidia-smi）。

### 18.5 测试

`tests/test_llama_runtime.py` 10 条：查找三档（env 指向不存在时**不回落**——
显式指定就该明确失败）、本程序装的优先于 PATH、解压挡路径穿越、解压响应取消、
`status()` 字段齐、已装不重复下载、资产分类、无官方二进制的平台不给按钮、
`/api/llama` 结构。

连带改了两处既有断言：`tests/test_nl_engine.py` 的默认值（`vllm` → `llamacpp`）、
`tests/test_web_vllm.py` 两条用例显式传 `engine="vllm"`（测的是 vLLM 的启停
与命令行，与默认值无关）。

`pytest tests -q` → 558 passed；`npm run build` 通过。

## 19. 2026-11-04：启动器的「初始化环境」按钮

### 19.1 那句「缺 web 依赖」的真正来源

`pack-dist.bat` 默认不带 `env\python`（约 60MB），接收方要先跑 `install.bat`。
有人直接双击 `Tagger.Shell.exe` 时，`ResolvePython` 找不到 `env\python`，
**静默回退到 PATH 上的系统 Python**，那个解释器没有 fastapi/uvicorn，
`tagger.cli web` 抛 `ConfigError` 退出，界面上看到的就是「缺 web 依赖」。

两件事的修法完全不同（跑 install.bat vs 往某个解释器里装包），却混成了一句报错。

### 19.2 不再静默回退

只有 `env\python` / `env\linux\bin\python` 直接信（它们是 install.bat 的产物，
缺东西时安装脚本的自检会先报）；`.venv` 和 PATH 上的解释器必须先过探针：

    python -c "import fastapi, uvicorn, websockets"

探不过就返回 `null`，抛 `EnvNotInitializedException`（与 `ServerStartException`
并列，界面据此决定给不给安装按钮）。探针 8 秒超时——Microsoft Store 那个
`python.exe` 桩会挂住去弹商店。

### 19.3 一键初始化

错误面板里出现「初始化环境」按钮。只在 `EnvNotInitializedException`，或日志里
含「缺 web 依赖」（装到一半断了）时出现；端口占用、WebView2 缺失之类给了也没用。

`Services/EnvInstaller.cs` 跑的是 `install.bat` 本身，不在 Shell 里再实现一遍 pip：
装什么版本、怎么写 `._pth`、离线从哪取轮子全在 `setup_embedded_env.ps1` 里，
复制一份必然漂移——"点按钮装出来的环境"和"跑 install.bat 装出来的环境"
不一样，是最难查的那类问题。

几个必须踩对的点：

- **完整路径调用 cmd**：`Environment.SystemDirectory\cmd.exe`。裸 `cmd.exe`
  在 PATH 被污染的机器上会命中别人的副本。
- **`/d`**：跳过 `Command Processor\AutoRun`，那里挂着坏路径时 cmd 每次启动
  都会先报「系统找不到指定的路径」。
- **`chcp 65001 >nul & call install.bat`**：用单个 `&`，chcp 被策略拦掉时安装
  照跑（只是日志中文变乱码）。编码链路实测过：PowerShell 输出按 UTF-8 写字节，
  `StandardOutputEncoding = Encoding.UTF8` 能正确解出中文。
- **日志 150ms 批量刷新**：pip 一次刷几百行，逐行 `BeginInvoke` 会把界面卡住，
  而那正是最需要保持可响应的时候。
- 装完自动 `BootAsync()` 重启；失败保留日志给「复制日志」；关窗即取消并收进程树
  （install.bat 幂等，下次接着补）。

### 19.4 已知取舍

不做"启动时静默自动安装"：要联网拉几十 MB、失败原因很多，静默跑的结果是用户
盯着一行「正在启动」等十分钟还不知道在下东西。显式点按钮 = 他知道在等什么。

### 19.5 pip 镜像滞后 ≠ 清单写错

有人报 `ERROR: No matching distribution found for idna==3.20`（可用版本列到 3.15）。
官方 PyPI 上有 3.20，`pip index versions idna` 能看到 3.16–3.20 全在——所以是那台
机器的 pip 走了滞后镜像，不是版本号敲错了。

两处改动：

- `requirements-embedded.txt` 的 idna 改成 `idna>=3.10,<4`。它是 httpx 的传递
  依赖，只做 URL 的国际化域名编码，不参与任何产物计算；钉死在这里的唯一效果是
  让"镜像慢"伪装成"清单错"，把排查引向错误的方向。
- `setup_embedded_env.ps1` 加 `-IndexUrl`（`install.bat` 用 `%*` 透传，所以
  `install.bat -IndexUrl https://pypi.org/simple` 直接可用）。失败提示按成因分开写：
  `No matching distribution` → 换源；`ConnectionError` → `-WheelDir`。原先只有一句
  "网络受限时用 -WheelDir"，把镜像问题也归进网络问题，照着走会白折腾。

判据：pip 报错里 `from versions: ...` 的最高版本明显落后于已知最新版 = 源旧。

## 20. 2026-11-04：目录弹窗跑偏 + 只能选一个盘

### 20.1 弹窗为什么出现在"整页的中间"

`.lx-page-enter` 的入场动画用了 `animation-fill-mode: both`，动画结束后继续保留
`transform: translateY(0)`。**任何非 none 的 transform 都会把这个元素变成
position:fixed 后代的包含块**——于是 FolderPicker 的 `fixed inset-0` 撑满的是
整页高的容器而不是视口，弹窗居中在整页中间，用户在顶部要往下滚才看得见。

两处修：

- `.lx-page-enter` 改 `backwards`：结束后退回元素自身样式（与 `to` 完全一致，
  opacity 1 / 无位移，没有可见跳变），transform 回到 none。这一处同时修好了
  页面里的其它 fixed 弹窗（QuantizeDialog / ShardAssets / Results 抽屉）。
- FolderPicker 再走 `createPortal(..., document.body)`。模态框本来就不该活在
  页面 DOM 里：以后哪个祖先加了 filter / transform / contain，都不会重演。

### 20.2 只能选训练器所在的盘

`_list_dirs` 只会往下走，`parent` 到盘根就成了自己，再没有出口。`/api/fs` 增加
`drives`：Windows 用 `GetLogicalDrives` + `GetDriveTypeW` 枚举固定/可移动/网络/
内存盘，**跳过光驱**（空盘探测会一直等到驱动超时，把浏览拖死）；Linux/macOS
返回空数组，从 `/` 本来就能走到任何挂载点。

前端在地址栏下面渲染一排盘符（`flex-wrap`：这台开发机有 16 个盘，单行会挤成
横向滚动），当前盘高亮。只有一个盘时不渲染，那行只是噪音。

`test_web.py::test_fs_lists_zip_files` 补断言：Windows 上给出的必须都是 `X:\`
形式，否则那排按钮点下去是死路。

### 20.3 盘符列表里混进了断开的网络映射

第一版把 `net use` 留下的鬼影也列出来了。那台开发机 `C:` 到 `Z:` 亮了 16 个，
其中 `O:/P:/Q:/R:/U:/V:` 是**已断开的网络映射**：`GetDriveTypeW` 报 4（网络），
但 `os.path.exists` 是 False——点进去就是一次 TCP 超时。

`GetLogicalDrives` 只反映"映射还挂在注册表里"，不代表目标还在。所以现在：

- 本地盘（2 可移动 / 3 固定 / 6 内存盘）只信类型，直接列；
- 网络盘（4）额外 `os.path.exists` 一次，连不上就不列；
- 光驱（5）仍然不列（空盘探测会等到驱动超时）。

列表整体缓存 60 秒，因为探测不可达的 NAS 正好要等 TCP 超时，而这个接口每点
一次「浏览」就被请求一次。代价是插上 U 盘要等一分钟才出现在那排按钮里。

### 20.4 盘符带上卷标

只有字母的 `C: D: E:` 分不清哪块是哪块——大家都是会给盘起名字的。`drives` 从
`string[]` 改成 `[{path, label}]`，卷标走 `GetVolumeInformationW`：

- 没设卷标的盘返回空串（新分区、内存盘很常见），前端退化成只显示盘符；
- 读失败也归到空串，不抛——一个读不到名字的盘不该让整排按钮消失；
- **必须设 `argtypes`**，64 位下不设的话指针参数按 32 位截断，调用直接崩；
- 只对已经确认存在的盘调用（本地盘 + `exists` 过的网络盘），不会对着鬼影盘
  再发一次网络请求。

前端渲染成 `系统 (C:)`、`开发 (H:)`，`max-w-[11rem] truncate` 兜住超长卷标，
`title` 里给全名。

## 21. 2026-11-04：试过 Windows 原生目录对话框，已回退

> 结局先说：**这条链路最终删掉了**，目录选择仍然走自绘的 FolderPicker。
> 下面记录的是过程与教训，以及为什么"先试原生、失败再回退"这个设计不成立。

### 21.1 为什么非走宿主不可

浏览器里**拿不到绝对路径**：`<input webkitdirectory>` 只给相对路径，
File System Access API 给的是 handle。可后端 `/api/fs`、`/api/preview`
都要按绝对路径读盘。所以自绘的 FolderPicker 在没有宿主时是唯一选择，
在有宿主（Tagger.Shell）时就是多余的一层——系统对话框能看缩略图、
能搜索、能直接跳任意盘。

### 21.2 通道

`window.chrome.webview.postMessage` ↔ `CoreWebView2.WebMessageReceived`：

    前端 → 宿主   { type: 'pick-folder', id, start }
    宿主 → 前端   { type: 'pick-folder-result', id, path }   // path=null = 取消

用 WebMessage 而不是拦自定义 URL scheme：路径里的中文、空格、`#` 塞进 URL
要来回转义，写错一次就是"选了却拿不到"的隐形 bug。带 `id` 是因为通道全局，
两个请求的响应对不上就会写错字段。

宿主侧用 `Microsoft.Win32.OpenFolderDialog`（.NET 8 WPF 自带，不必为了
一个对话框去引 WinForms）。系统框是模态的，必须在 Dispatcher 里弹；
异常也要回一个 null，让前端退回自绘窗口，而不是点了没反应。

### 21.3 三态，不是布尔

`NativePick` 是 `path / canceled / unsupported`：

- `canceled` **绝不能**再弹自绘窗口——用户刚关掉对话框，你立刻给他弹第二个
  就是在耍人；
- `unsupported`（不在 Shell 里，或宿主没应答）才回退。

超时设 60 秒而不是几秒：用户在系统对话框里翻目录本来就慢，短超时会在对话框
还开着的时候把第二个窗口顶出来。旧 exe 没有这条通道，会吃一次 60 秒，所以
超时后把 `hostReplied` 置 false，之后点击直接走自绘。

### 21.4 调用点收敛成 hook

5 个页面、8 个按钮原本各自维护 `picker` 布尔 + `<FolderPicker>`。现在统一成
`useFolderPicker()` 返回 `{ pick(start, apply), dialog }`：先试原生，不行再开
自绘，`dialog` 渲染在页面上即可。Packaging 那页四种目标共用一个
`openPicker(target, index)`，选完按 target 写回不同字段。

### 21.5 第一版"点了没反应"的三个坑

上线后有人报"点浏览呼不出系统对话框，没有任何反应"。代码确实编进了 exe
（dll 里能搜到 `OpenFolderDialog` / `WebMessageReceived`），所以是运行时的问题。
三个坑叠在一起，症状都是"没反应"：

1. **没有握手。** 原先前端发完消息只能干等 60 秒。在这 60 秒里，
   "宿主不认这条通道"和"用户正在系统框里翻目录"在网页看来完全一样——
   而前者要立刻回退，后者绝不能打扰。加了 `pick-folder-ack`：宿主收到就回，
   网页 1.5 秒收不到就直接开自绘窗口，收到就把超时换成 10 分钟。
2. **在 WebView2 事件回调里同步弹模态框。** 原来用 `Dispatcher.Invoke`，
   `ShowDialog` 会把当前线程堵到用户关窗，而这条回调卡在浏览器进程的消息回路上，
   界面就整体假死。改成 `Dispatcher.BeginInvoke`，回调立刻返回，对话框排队去弹。
3. **异常被静默吞掉。** `OpenFolderDialog` 抛异常时原来只写日志、回 `path: null`，
   前端把 null 理解成"用户取消"——于是什么都不做。现在异常原因随
   `{ path: null, error }` 回传，前端进入 `failed` 分支照样回退自绘。

另外自绘窗口顶部会说明为什么会落到它（"当前启动器版本不支持系统对话框" /
"系统目录对话框打不开（原因）"），别再让人以为按钮坏了。

排查入口：`%LOCALAPPDATA%\Tagger\shell.log`，点一次浏览应该出现
"收到目录选择请求，起始目录：..."。没有这行 = 消息根本没到宿主，
先确认跑的是新 exe、并且是在 Shell 里点的（浏览器里本来就没有系统对话框）。

### 21.6 最终决定：整条链路删除

加了 ack 之后第一次点仍然呼不出系统框。用户的原话是"不然会给人一种怎么卡了一
两秒的感觉"——这就是结论：**任何"先试 A、失败再 B"的交互，只要 A 的失败不能
在几十毫秒内被感知，用户感受到的就不是回退，而是卡顿。**

删掉的东西：

- 前端 `web/ui/src/lib/native.ts`（消息封装 + ack 超时）；
- `useFolderPicker` 里的原生分支，现在只 `setOpen(true)`；
- Shell 侧 `core.WebMessageReceived += OnWebMessage`、`PickFolderRequest`、
  整个 `OnWebMessage` 方法，以及 `FolderPicker` 的 `notice` 参数。

保留的东西（是真正的改进，与原生对话框无关）：

- 弹窗 `createPortal` 到 body，不再被 `.lx-page-enter` 的 transform 困住；
- 盘符切换行；
- `animation-fill-mode: backwards`。

如果以后还想接系统对话框，前提是**能同步拿到结果**（比如 WPF 侧直接把选中
路径写进一个剪贴板/文件，由后端读），而不是走"发消息—等回执"这种天然有延迟
的异步路径。

## 22. 2026-11-04：「模型量化」独立页（FP8 / W4A16 / Q4_K_M / Q6_K）

### 22.1 为什么要有独立入口

量化原本只藏在「新建任务 → 精度选 W4A16」的弹窗里。结果是：**用户不能主动
去量化**，只有被精度选择裹挟着才会撞见它。而官方 GGUF 只发 Q4_K_M / Q8_0 /
F16 三档，Q6_K 和 Q5_K_M 得自己从 F16 压（`devtools/nl-quant-ab.md` 5.1），
W4A16 更是没人替你发——所以"量化"本身就是一件要单独做的事。

### 22.2 四条路线不是同一件事

| 档位 | 性质 | 输入 | 产物 | 依赖 |
|---|---|---|---|---|
| FP8 | **运行时** | bf16 | 无 | CC ≥ 8.9 的卡 |
| W4A16 | 离线 | safetensors | 目录 | torch + llmcompressor + GPU |
| Q4_K_M | 离线 | **F16 GGUF** | 单个 .gguf | llama-quantize（纯 CPU） |
| Q6_K | 同上 | 同上 | 同上 | 同上 |

把它们做成一个「量化」按钮会让人选错。页面上四张卡片各自标出"离线/运行时"
和"这台机器能不能跑"，能力探测也分开：缺 torch 不影响 Q4，缺 llama-quantize
也不影响 W4A16。

FP8 那条**没有产物**——vLLM 用 `--quantization fp8` 在加载时转，不落盘。
页面上如实这么写，不假装能产出文件。

### 22.3 GGUF 为什么不从 safetensors 一键转

`convert_hf_to_gguf.py` 不在 llama.cpp 的 release 包里，还要 torch +
transformers。与其在页面里做一个"看起来能一键、实际经常卡在依赖上"的转换，
不如要求用户给一个现成的 F16/BF16 GGUF（Qwen 官方 GGUF 仓库有 F16 档），
把要求写在输入框的 help 里。**输入必须是 .gguf**，传 safetensors 会明确报错。

### 22.4 实现

新增 `tagger/gguf_quant.py`，形态照抄 QuantManager（单例 / 子进程 / 日志尾 /
taskkill 取消），但不复用它的类：产物形态、进度来源（llama-quantize 的
`[ 12/ 291] 4.12`）、注册字段（GGUF 走 config 的 `gguf` 而不是
`quantized_path`）没有一处相同。硬塞进一个类只会得到两个 `if method ==`。

API：

    GET  /api/quantize             三条链路的统一快照 + 可量化模型清单
    POST /api/quantize/gguf/start   {source, target, out_file}
    POST /api/quantize/gguf/cancel

W4A16 继续用原有的 `/api/vllm/quantize/*`，一行没动——弹窗在用，别为了"统一"
去改一条正在工作的链路。

两处值得记的细节：

- **参数校验放在能力探测之前**。否则路径填错的人会被告知"去装 llama-quantize"，
  装完再点还是同样的错。
- **默认产物名带档位**（`<源文件名>-Q4_K_M.gguf`）。两份 GGUF 只差一个后缀位
  摆在同一目录，换错文件跑一天才发现，成本太高。

## 23. 2026-11-09：两条量化路合成一个「量化」菜单（附两个"点了没反应"的修复）

### 23.1 一个按钮、两个引擎分组

模型管理页每行原本并排两个按钮：「压 GGUF」和「量化 W4A16」。它们其实是
**同一个问题的两种答案**——llama.cpp 只吃 .gguf，vLLM 只吃 W4A16，选错引擎
产物就是废的——并排摆着会让人以为要各点一次、或者以为两个都要做。

现在合成一个「量化 ▾」，点开是浮动菜单（`components/QuantizeMenu.tsx`，
形态照抄 PresetMenu：`relative` 容器 + 绝对定位面板 + 点外部/Esc 收起）：

| 分组 | 环境 | 档位 | 落到哪个弹窗 |
|---|---|---|---|
| llama.cpp | Windows 直接能跑 | F16 / Q4_K_M / Q6_K | `GgufConvertDialog` |
| vLLM | 需要 Linux / WSL2 | GPTQ / AWQ | `QuantizeDialog` |

菜单只做**快捷选档**，两个弹窗各自仍然是选档 + 确认 + 装依赖的地方：两个弹窗
分别多了一个 `initialTarget` / `initialMethod` 参数，打开就是菜单里点的那一档。
档位 key 必须与后端对齐（`gguf_convert.TARGETS` / `quantize.QUANT_METHODS`），
真正的清单仍从 `/api/gguf`、`/api/vllm/quantize` 取，后端加档位不至于两边打架。

按钮常驻：权重没下完时置灰（title 说明原因），而不是消失。

### 23.2 「重新量化」点了没反应：done 判定把表单顶回去

`QuantizeDialog` 原来靠渲染条件 `task?.status === 'done'` 决定"显示结果 + 使用
W4A16"还是"显示表单"。而 `QuantManager` 会把上一条任务一直留在快照里，
所以只要这一次会话量化成功过、或者上次成功过，`done` 就一直是 true：

1. 点「重新量化」→ `showForm = true`；
2. 表单区被 `task?.status !== 'done'` 挡住，不出现；
3. 页脚又因为 `(!showForm || task?.status === 'done')` 落回原来那两个按钮。

净效果 = 除了"已有量化权重"那张卡片消失外什么都没变，用户看到的就是"点了没反应"。

修法：**收起表单这件事必须真的改状态，不能靠渲染条件去伪装**。加一个
`useEffect(() => { if (task?.status === 'done') setShowForm(false) }, [task?.status])`，
渲染条件退化成 `showForm` 单一真相源（`showForm` 真 → 表单 + 开始量化；
假 → 结果 + 重新量化/使用）。以后再加状态也别在条件里拼"当前看起来应该是什么"。

### 23.3 「本地量化」下载完就消失：入口长在了错误的那个分支里

`LocalFilesBar` 的工具栏按 `running / downloaded / 其它` 三分支渲染，「本地量化」
只写在**最后一个分支**里——于是它天生属于"还没下完"这个状态：一下完，整个
分支不再渲染，按钮随之消失；把权重移出去（`exists` 变 false）它又出现。
这就是用户报的"移出去再移回来就好了"——那个"好"只是状态过期。

现在这一条入口整个删掉，统一到行头的「量化 ▾」（见 23.1）。

### 23.4 校验

`cd tagger/web/ui && npx tsc --noEmit` 0 错、lint 0、`npm run build` 通过
（服务端直接吃 `dist/`，不重建看不到改动）。

## 24. 2026-11-09：「No module named 'conversion'」与选不到 Q4/Q6

### 24.1 那句报错不是缺 pip 包，是 sys.path

现象：点「开始转换并量化」立刻失败，日志里

    File "...\runtime\llama.cpp\convert\convert_hf_to_gguf.py", line 18
        from conversion import (
    ModuleNotFoundError: No module named 'conversion'

看着像少装了一个包，其实 `conversion` 是**同一个 tar 里的目录**（还有
`gguf-py/`），官方脚本靠 `sys.path[0] = 脚本所在目录` 找到它们。子进程被
包了一层启动器、环境里开了 `PYTHONSAFEPATH`、或从别的 cwd 拉起时，这一项就
没了。本地复现一句就够：

    python -P runtime/llama.cpp/convert/convert_hf_to_gguf.py --help
    # 同样的 ModuleNotFoundError: No module named 'conversion'

修法（`gguf_convert._run_script`）：给子进程显式注入

    PYTHONPATH = <TOOLS_DIR>;<TOOLS_DIR>/gguf-py

`-P` / `PYTHONSAFEPATH=1` 都只影响 `sys.path[0]`，**不影响 PYTHONPATH**，所以
这条兜得住。已验证 `-P`、`PYTHONSAFEPATH=1` 两种情况下脚本都能正常 import。

> ⚠ **2026-11-09 追加：这条对随程序发的内嵌 Python 无效。** 那个解释器带
> `python312._pth`，属于 isolated 模式（`ignore_environment=1`），**PYTHONPATH
> 会被整个忽略**，报错一模一样。真正的修法见 §27。教训是：验证要用*那个*
> 解释器（`env\python\python.exe`）验，系统 python 验过不算数。

另一半是"半套解压"也会长成同一句话（断网 / 盘满 / 进程被杀留下"只有脚本、
没有 conversion 包"）：所以

- `tools_ready()` 三个文件都验（`convert_hf_to_gguf.py` + `conversion/__init__.py`
  + `gguf-py/gguf/__init__.py`），`snapshot().script_ready` 也换这个口径；
- `ensure_tools()` 不齐就重取，重取前先清 `conversion/`、`gguf-py/`，避免新旧
  两版混在一起；
- 跑挂了且日志里缺的是**我们自己那两个包**（`conversion` / `gguf`）→
  `ensure_tools(force=True)` 重取一次再跑，只重试一次。缺 `sentencepiece`
  这类第三方模块不触发重下——下了也没用。

### 24.2 模型管理里选不到 Q4/Q6

精度下拉原来只有 `int4 / int8 / bf16 / fp16 / fp32`，全是 vLLM 的口径。自己
添一个模型、压出 Q4/Q6 之后，这里根本没有那一项可选 —— 于是"自定义模型没法
用本地量化产物"。

现在按两条链路分组（`Models.tsx`）：

| 分组 | 取值 | 含义 |
|---|---|---|
| vLLM（加载方式，不换文件） | bf16 / fp8 / w4a16 | 同一份权重怎么加载 |
| llama.cpp（GGUF 档） | f16 / q4 / q5 / q6 / q8 | 换的是权重文件本身 |

- 已压过的档后面带「· 已压」（读 `gguf_quants` 的键）；选了没压过的档就提示
  "先用行首的「量化」压一份"，别等启动才报缺文件。
- 老清单里的 `int4` 等写法仍留一条 option：不留的话 `value` 对不上，下拉框会
  显示成别的档，用户以为精度被改了。
- 新增条目默认 `f16`（与默认引擎 llama.cpp 的默认档一致），不再是 `int4`。
- 后端 `web/models.py` 的 `_PRECISIONS` 补了 `fp8` / `w4a16`：原来漏了它们，
  页面上选完保存会被**静默归一成 int4**。

顺手把 `test_api_reports_two_targets_and_source` 改成三档口径（`f16` 早就加进
`snapshot()` 了，断言没跟上，一直是红的）。

校验：`pytest tests/test_gguf_convert.py tests/test_web_models.py
tests/test_precision.py tests/test_config.py tests/test_nl_engine.py` 全绿；
前端 `tsc --noEmit` 0 错、`npm run build` 通过。

## 25. 2026-11-09：「我明明看见有权重」→ 那是 GGUF，不是原始权重

现象：点「量化」→ 选档 → 弹窗说"还缺原始权重"，提示里那句"我找过：
H:\tagger\models\nl\qwen3vl-8b"，而那个目录里确实躺着 6 个文件。

真身：目录里是 `*-F16.gguf` / `*-Q4_K_M.gguf` / `*-Q6_K.gguf`，
**没有 config.json、没有 *.safetensors**。本地量化这条路要吃的是 transformers
的原始权重，压好的 GGUF 反过来是它的**产物**。也就是说这条模型早就压完了，
根本不用再走一遍。

改了三处，都是为了让人一眼看出这个区别：

1. `gguf_convert.source_dir()` 的报错不再只说"不在本地"，而是逐个候选目录
   说明里面有什么（`_describe_dir`）：只有 N 份 GGUF / 有 safetensors 但缺
   `config.json` / 空目录。只有 GGUF 那一种会额外补一句"这些是已经压好的量化档，
   直接用就行"。
2. `snapshot()` 增加 `quants`：清单里登记的每档 + 文件真实大小（`bytes == 0`
   = 配置指着的文件已经不在了）。弹窗据此在"还缺原始权重"那张卡片里先列出
   "已经有 Q4_K_M（5.0 GB）"，并在档位下拉里标「本地已有」。
3. 顺手修了两处陈旧文案：弹窗原来写"回到这一行的下载档位，选「原始权重（用于
   本地量化）」"——**那个档位选择器早就不存在了**，下载只有"下载原始权重"这
   一个动作。

另外 `web/models.py` 给每条 VLM 加了 `gguf_ready`（登记过且**文件真在**的档），
模型管理页的「已压」标记和"本地还没有这一档"的提示都改成读它。原来只看
`gguf_quants` 有没有这个键，配置里指向一个被删掉/改了名的文件时照样显示"已压"。

### 25.1 顺带查出的数据问题（未改，等人决定）

`qwen3vl-8b` 的 `gguf_quants.q6` 指向 `qwen3vl-8b-Q6_K.gguf`，磁盘上没有
这个文件；实际那份叫 `Qwen3VL-8B-Instruct-Q6_K.gguf`（旧版本按仓库名命名产物
留下的）。所以这模型选 q6 会报"缺文件"。要么把文件改成清单里的名字，要么把
`tiers.yaml` 的映射指过去——两份都是 6.7G，改配置更省事。

### 25.2 「原始权重已下载」也是假的 + 重新下载入口

同一个坑的另一半：`exists` 只看"目录里有文件"，于是只剩 `.gguf` 的目录照样
显示"原始权重已下载"，而且整个下载入口在 `downloaded` 分支里消失了——想重下
原始权重的人被卡在"已下载"这个状态里，无处可点。

- 后端 `_local_state()` 给 VLM 行加 `raw_ready`（`config.json` + 至少一份
  `*.safetensors`，判据与 `gguf_convert.source_dir()` 一致）。文案据此分成
  「原始权重已下载」与「目录里只有量化后的 GGUF」。
- `downloaded` 分支里补一个下载按钮：
  - 没有原始权重 → 文案「下载原始权重」，点前 confirm（真要拉十几 G）；
  - 已有原始权重 → 文案「重新下载」，**幂等补齐**：`_fetch()` 对下完的文件按
    大小跳过、对半截的走 Range 续传，所以不会白下一遍 16 G。想要真·覆盖就先
    点旁边的「删除」。
- `mergeLocal`（下载轮询只刷本地字段那个）补上 `missing_files / gguf_ready /
  raw_ready / ready`：之前这几个不跟着刷，下完原始权重仍显示"只有 GGUF"。

### 25.3 「下载要点两次」——其实第一次就下上了，只是没人把结果读回来

点下载 → 后端任务确实起来了，但这一行的 `download` 字段还是空的；而轮询
（1.2s 的 `api.models()`）是在 `downloading = 某一行 isRunning` 为真时才启动的，
于是「要等刷新 → 才能开始轮询 → 才会显示在下载」形成死锁：界面上什么都没有。

第二次点之所以"能下"，是它先走了 `onPrepare()`（保存并回读清单），那次回读
才看见第一次已经启动的任务，进度条于是出现——看起来就像"第二下才生效"。

修法：两个下载入口点完 `startDownload` 后**立刻 `return await api.models()`**，
交给 `act()` 的 `onRefresh` 合并进去，轮询随即启动。

## 26. 2026-11-09：下载改多连接分段（大文件）

`web/downloads.py` 原来是"一个文件一条连接、文件之间排队"。16.4G 的
`safetensors` 在国内单条 TCP 上常被压到几百 KB/s，这不是优化问题，是能不能
在一天内下完的问题。

**不新写一套**：`hf_source.download_shard` 里已经有完整的分段实现（预分配稀疏
`.part` 让峰值只占 1 倍空间、每段进度写 `.part.json`、段级重试、
`Range` 探测失败自动退回单连接、取消保留半成品），而且是打标链路在跑的代码。

- 给 `download_shard` 加 `url` 入参：它的默认直链是 `/datasets/...`，模型下载
  要走 `/models/.../resolve/`，路径不同，不能靠改常量。
- `_fetch()`：`size >= 2×SEG_MIN_BYTES`（16 MB）且连接数 > 1 时走
  `_fetch_segmented()`。连接数默认 4，`TAGGER_DL_CONNS` 可调（1 = 关掉分段）。
  小文件（config/tokenizer）保持单连接——多开只是多几次 TLS 握手。
- 两个坑写在代码里了：
  1. `download_shard` 只要见到**非空同名文件**就当"已下完"返回，而单连接时代
     留下的半截文件正是这种形态 → 分段前先把它删掉（升级前下到一半的只能放弃）；
  2. 它的 `.download.lock` 是内部细节，用完清掉，别留在模型目录里。
- 取消：`HFCancelled` 不推进累计字节、`.part` 留着续传。

**只对单文件分段，文件之间仍顺序**：一个仓库的瓶颈永远是最大的那份文件，
并行下 5 个 4G 分片和顺序下（每片 4 连接）的总带宽一样，但后者不会把 20 条
连接一起砸到镜像站上。

测试：`tests/test_web_downloads.py`（大文件必走分段、半截文件被清、锁文件被清、
取消保留 `.part`）。

## 27. 2026-11-09：下载源（镜像站）搬进页面设置

以前只有 `HF_ENDPOINT` 环境变量这一条路，而且改完要重启服务。用户的实际处境
是"每下 16G 就走一次梯子"，镜像站（hf-mirror.com）国内直连，才是那个解。

- 覆盖清单里新增一项 `endpoint`（和 `proxy` 并列，不进分档表、整表保存时原样
  带回）。空 = 没设过。
- **优先级收敛到一处**：`config.download_endpoint()` = 页面设置 > `HF_ENDPOINT`
  > 官方。`web/downloads._endpoint()` 改成调它，于是清单、下载、代理测试、
  分段下载全都自动跟着走——优先级散在各调用点上，迟早出现"设置页显示镜像、
  实际请求还打官方"。
- `PUT /api/models/endpoint`（与 `/proxy` 同构）+ `models.set_endpoint()`：
  只认 http/https、吃掉末尾斜杠、空串 = 跟随环境变量。漏协议（`hf-mirror.com`）
  直接 400——httpx 会把它当相对路径，报出来的错跟"网络不通"分不清。
- 页面「下载代理」那一栏改名「下载网络」，顶部加「下载源」下拉：
  默认 / 官方 / 镜像 hf-mirror.com / 自定义，下面一行显示**当前生效**的地址
  （环境变量在起作用时也说明）。镜像与代理是替代关系，不必同时开。

**没覆盖**：数据集分片下载（`hf_source.HF_RESOLVE` 写死官方的 `/datasets/`）。
那条链路要接镜像得单独改，别顺手一起动——它影响的是打标输入，不是权重。

测试：`tests/test_web_models.py::test_endpoint_setting_wins_over_env`
（优先级 + 末尾斜杠 + 清空回落到环境变量）、`::test_endpoint_rejects_missing_scheme`。

### 27.1 默认值改成镜像站

`config.DEFAULT_ENDPOINT = "https://hf-mirror.com"`。优先级变成
**页面设置 > HF_ENDPOINT > 镜像站**（原来兜底是官方）。

理由是默认值也是一种设计：官方在国内基本要挂代理，十几 G 的权重走按流量
计费的梯子，默认选官方等于默认让人肉疼。要回官方得在「下载源」里显式选。
门禁/私有仓库如果镜像不转发 `HF_TOKEN`，401 的报错里会直接提示切回官方。

## 28. 2026-11-09：暂停后点删除 → WinError 32

现象：`删不掉：[WinError 32] … model.safetensors.download.lock`。

根因是"取消"只是**改状态**：`cancel()` 把 status 置成 canceled 让页面立刻
有反馈，但后台线程还在把当前块写完、断开连接、落分段状态；这段时间
`.part` 和 `.download.lock` 的句柄都开着，Windows 不允许删。

修法两层：

1. `Task.thread` 记住后台线程，`DownloadManager.wait_done()` join；
   `models.delete_files()` 先 `cancel()` 再 `wait_done()`，超时才报
   "下载线程还在收尾，几秒后再点一次"。
2. `delete_dir()` 对 `OSError` 重试（0 / 0.2 / 0.5 / 1 / 2 秒）——Windows 上
   杀软和索引器也会短暂占住刚写完的大文件，一次 `rmtree` 撞上就报同一个错。

顺手：分段下载的 `.download.lock` 在我们这层用完就删（`_fetch_segmented` 的
finally），不再留在模型目录里。

## 29. 2026-11-09：连接数可选 + 起步不再显示"剩 35 小时"

用户实测镜像站：**单条约 0.35 MB/s，总速随连接数近乎线性涨**
（4 条 1.2、8 条 1.8、16 条 5.8 MB/s），16.3 G 在 4 条下要 4 小时、16 条约
45 分钟。原来连接数写死 4，等于白送。

- 覆盖清单加 `conns`（0 = 默认），`config.download_conns()` 统一优先级：
  页面设置 > `TAGGER_DL_CONNS` > 4，钳到 1~32。`web/downloads._conns()` 调它，
  旧的那份"自行 clamp 到 8"的逻辑删掉（两份口径必然分叉）。
- `PUT /api/models/conns` + `models.set_conns()`；「下载网络」面板加一个
  1 / 4 / 8 / 16 / 24 / 32 的下拉，改完下次下载生效（在跑的沿用旧值）。

第二个问题是**看着像卡死**：起步那几秒全花在 TLS 握手、302、分段铺开上，
0.5s 窗口采出来的速度能低一个数量级（实测 0.35 MB/s vs 稳定后 30 MB/s），
按它算 ETA 就成了"剩 127906s"。

- `Task.tick()`：起步 2 秒内不估速（`now - started_at >= 2.0` 才采样）。
- `Task.state()`：`elapsed >= 5` 才给 `eta`，否则 `null`；页面在 `elapsed < 5`
  时显示"测速中…"，之后没有 ETA 就什么都不显示（真停住时不该写"测速中"）。

测试：`test_web_models.py::test_conns_setting_roundtrip`（默认 4 / 设 16 /
清 0 / 99 被拒）。

## 30. 2026-11-09：转换卡在 0% —— 其实卡在**取脚本**，不是转换

现象：点转换，1 分 51 秒还是 `0% · 转换（safetensors → GGUF）`，没有任何日志。

问运行中的服务（`GET /api/gguf?model=...`）看到真相：

    ModuleNotFoundError: No module named 'conversion'
    脚本自检失败（缺模块），重取一份转换脚本后重试
    取转换脚本（llama.cpp v0.9.0）
    下载源码 8/37 MB        ← 卡在这

也就是说：**脚本是半套的**（第 25 节那个坑），新加的自愈把目录清掉重取，
于是去 GitHub 下 37 MB 源码包——国内直连实测 **0.03 MB/s**，要下二十分钟，
界面上就是"0% 一动不动"。

五处改动：

1. 源码包多路兜底（`_SOURCE_ROUTES`）：直连 → `gh-proxy.com` → `ghfast.top`，
   15 秒内不到 3 MB 就换下一条（实测 gh-proxy 1.37 MB/s，37 MB 半分钟）。
2. 这条下载允许用**页面设置的代理**（`_settings_proxy()`）。注意不能用
   "下载源"——那是 HF 的前缀，跟 GitHub 无关。全部路都不通时报错里直接写
   "填个代理 / 手动把三个目录解到 runtime/llama.cpp/convert"。
3. 阶段要自曝：取脚本时 `stage` = "获取转换脚本（首次要下 ~37 MB）"。
4. **运行中也显示日志**（对话框最近 4 行）——0% 停住时，日志里写着在干什么。
5. 子进程加 `-u` + `PYTHONUNBUFFERED=1`（原来 stdout 块缓冲，进度行攒到 8 KB
   才吐），主循环从"阻塞读 stdout"改成"读线程 + 0.2s 轮询"：
   - 加载 16 G 权重的几分钟里**也能取消**（原来只能等下一行输出）；
   - 超过 30 秒还没产物，stage 改写成"加载原始权重，前几分钟没有进度是正常的"。

现状：这台机器的 `runtime/llama.cpp/convert` 已用新逻辑装好
（`tools_ready() == True`），重启服务后直接就能转，不用再下那 37 MB。

测试：`test_gguf_convert.py::test_source_download_falls_back_to_mirror`、
`::test_source_download_reports_actionable_error`。

### 22.5 前端

`pages/Quantize.tsx`，导航在「模型管理」下面。轮询 1.5s（有任务）/ 5s（空闲），
点「开始」后立刻 `tick` 一次刷新，不等下一个 5 秒——否则那 5 秒会被当成卡住。

### 22.6 缺 llama-quantize 时糊掉这一块，而不是让按钮灰着

`install.bat` **不装 llama.cpp**（它是 GitHub release 的 200MB zip，不是 pip 包；
默认装会让"一键安装"多一个可能被墙的失败点）。所以第一次打开量化页，
GGUF 那两档一定是不可用的——这时把整块表单 `blur-[2px] opacity-40` 并盖一层
说明卡，比让"开始量化"灰着更直白：不可用的是**整条路线**，不是某个字段填错了。

说明卡里的按钮直接 `location.hash = '/deps'`。为此在依赖中心顶部补了
「llama.cpp 运行时」一块（复用 `LlamaSetup`）——原来这个安装入口只藏在
「新建任务 → 引擎选 llama.cpp」下面，从量化页跳过去会扑空。

### 22.7 手填量化类型

预设只放 Q4_K_M / Q6_K（这两个有 A/B 数据支撑），但 llama-quantize 认几十种。
下拉里加了「自定义…」，选中后出现输入框，可以填 Q5_K_M / Q8_0 / IQ4_XS / F16。

后端 `resolve_flag()` 统一归一：预设走 `TARGETS` 拿 flag，其余过
`^[A-Za-z0-9_]{2,24}$` 放行并转大写。**这个值会原样进 argv**，虽然不经过
shell，但让页面能往里塞任意字符串没有任何好处——带空格、分号的一律拒。

页面上同时显示等价命令行（`llama-quantize "src" "out" Q5_K_M`），想自己在终端
跑的人直接复制。它和按钮发的是同一个值（`ggufFlag`），不是另写一份说明。

### 22.8 GGUF 改成自己实现（22.6 / 22.7 的外部工具路径已废弃）

用户的原话是"我们自己实现量化"。之前的做法是调 llama.cpp 的
`llama-quantize.exe`——那等于要求用户为了压一个模型先下 200MB 运行时，
和"外面没有量化好的能下载，所以我们在本机自己算"的初衷是反的。

现在整条链路在 Python 里跑完，不碰任何外部程序：

    tagger/gguf_file.py   GGUF 读写（元数据原样搬运，只重写张量类型和偏移）
    tagger/kquant.py      Q4_K / Q6_K 的量化与反量化（numpy 向量化）
    tagger/gguf_quant.py  编排：逐张量量化、进度、取消、写 .part 再改名

**元数据为什么不解析**：GGUF 的元数据有十几种类型（字符串/数组/嵌套数组），
Qwen 这类模型还塞了 tokenizer 词表和 chat template。解析再编码，任何一个
类型写错，llama.cpp 就直接拒绝加载；而量化根本不需要理解它们。所以
`Reader.metadata_bytes()` 原样返回那一段字节，写回时照抄。

**Q4_K_M 的「M」**：不是纯 4 bit。`v_proj`（value 投影）保留成 Q6_K，其余走
Q4_K——照抄 llama.cpp 的规则。v 投影对 attention 输出影响最大，混成 4 bit
掉得最明显。

**精度**（正态输入，量化→反量化的相对 RMS）：

    Q4_K  0.078      Q6_K  0.022

4 bit 的理论下限约 0.087（step/√12），所以 Q4 这个数是对的，不是"差不多就行"。
代价是没做 ggml 的迭代最优 scale（`make_qkx2_quants`），同 bit 下比官方略差。

**速度**：约 0.03 s/MB 输入，8B 模型（16 GB F16）约 8 分钟。比
llama-quantize 慢一个量级，但只跑一次。

**取消/失败**：写 `.part` 再 `replace()`。中途取消时目录里不会出现一个
看起来能用、实际半截的 .gguf——那种文件拿去加载，报的错离真正原因很远。

**只实现了三档**：Q4_K_M / Q4_K / Q6_K。`resolve_flag` 不再转发任意类型名
（以前是原样丢给子进程），不认识的直接拒——自己的代码没写的档位，报错比
产出坏文件好。下拉里的「自定义…」和手填输入框随之删掉。

**页面**：22.6 那块"缺 llama-quantize 就模糊 + 跳依赖中心"的遮罩已删除，
GGUF 面板现在总是可用，底部一行显示"引擎：内置 numpy（Q4_K / Q6_K）"。
依赖中心里的 llama.cpp 区块仍然保留——那是 NL 引擎要用的 llama-server，
和量化无关了。

**输入仍必须是 GGUF**。safetensors → GGUF 要处理 tokenizer 词表、chat
template、视觉塔层名映射，那是格式转换，不是量化。两件事分开做，免得把
一个大坑伪装成一个按钮。要补的话是下一个独立任务。

### 22.9 `d` 的 127/63 倍约定（唯一一处"自洽但不可用"的坑）

第一版把归一化因子乘进了 `scales`，让 `d` 保持原始量级：

    d = max_scale          scales = round(scale/d * 127)     解包 d * sc / 127

自己写的文件自己读，round-trip 全过，误差 0.078，看起来没问题。但拿它去解
**官方发布的 Q6_K**，RMS 直接 0.99、相关系数 0.0——整体小了 127 倍。Q4_K
同理，差 63 倍。

ggml 的约定是相反的：

    d = max_scale / 127    scales = round(scale / d) ∈ 0..127   解包 d * sc

`d` 承担缩放，`scales` 只存整数比例，解包时不再有任何除数。Q4_K 的 `dmin`
也是 `/63`。改过来之后：

| | 解官方文件 | 自压后解回 |
|---|---|---|
| Q6_K | rms 0.018 / corr 0.9998 | rms 0.022 / corr 0.9998 |
| Q4_K | rms 0.072 / corr 0.9974 | rms 0.079 / corr 0.9969 |

左边那一列是**唯一能证明位布局对的东西**——所以 `tests/test_kquant.py` 里加了
`test_dequantize_matches_official_gguf`，在本机有官方 GGUF 时会真的反解对比
（没有就 skip，CI 不受影响）。自己造数据 round-trip 永远发现不了这类错误。

**另一个坑：Reader 原来 `fh.read()` 全读进内存。** F16 的 8B 文件 16 GB，
还没开始算就 OOM。改成 `mmap`，切片和 `struct.unpack_from` 用法不变；
`quantize_file` 结束时显式 `close()`，否则 Windows 上句柄一直挂着。

### 22.10 真实产物验证：8B F16 → Q6_K，llama.cpp 认不认

跑了一遍完整链路（`Qwen3VL-8B-Instruct-F16.gguf` 16.4 GB → Q6_K）：

    耗时      4.7 分钟（不是估的 15 分钟：大头 token_embd/output 在前面，后面全是小张量）
    产物      6.26 GB
    张量      399 个，其中 327 个 Q6_K、72 个保持 F32

那 72 个是 `blk.N.attn_{k,q}_norm.weight`——**每个只有 128 个元素**（per-head 的
QK norm），128 不能被 256 整除，按设计跳过。

但这一版还有两个偏差，都已修（见 22.12）：1D norm 被误量化、Q4_K_M 的
`ffn_down` 没做分层升档。

**用 llama.cpp 官方的 `gguf` Python 包验的**（`pip install gguf`，纯 Python）：

    GGUFReader 解析成功，399 个张量
    gguf.quants.dequantize 反解 blk.0.attn_k.weight
    corr = 0.9998    rms = 0.0224

这比自己读自己强得多：`GGUFReader` 和 `quants.dequantize` 就是 llama.cpp 仓库里的
参考实现。它能解析、反量化又对得上，说明这份文件在 llama.cpp 眼里是合法 K-quant。

**规则对齐之后，产物和官方 Q6_K 字节级相同**：

    大小      6725900384 == 6725900384
    类型分布  {Q6_K: 254, F32: 145} == 官方
    逐张量    类型差异：无
    反量化    corr 0.9998 / rms 0.0224

大小分毫不差，说明张量顺序、块数、对齐和偏移全部落在同一套算法上——这比
"误差够小"强得多，是"同一份文件"级别的吻合。（前两版分别差 0.46 GB 和
0.95 MB，都在这一步被揪出来：第一次是单位算错，第二次是 1D norm 被误量化。）

**官方 Q6_K 里没有 Q8_0 升级**——`output.weight` 就是 Q6_K，`ffn_down` 全是
Q6_K。我一度以为 `output` 会被升到 Q8_0，那是凭印象，逐张量查过官方文件后
推翻了。

产物留在 `models/nl/qwen3vl-8b/Qwen3VL-8B-Instruct-Q6_K-ours.gguf`，不需要可以删。

### 22.12 两条规则对齐官方（1D 不量化、ffn_down 分层）

**1D 张量一律不量化。** 第一版只判 `n_elements % 256 != 0`，于是 4096 元素的
`attn_norm` / `ffn_norm` 被压成了 Q6_K——它们能被 256 整除。后果比"省几 MB"
严重：norm 是逐层的缩放因子，误差顺着残差流一路累积到 logits。
官方 Q6_K 里 145 个 F32 全是 1D 张量，我们只有 72 个，差的就是这 73 个。

现在条件是 `len(dims) == 1` 直接跳过。

**Q4_K_M 的 `ffn_down` 分层升档。** 拿官方 Q4_K_M 数了一遍，36 层里这 18 个是
Q6_K：

    0,1,2,3 | 6,9,12,15,18,21,24,27,30 | 31,32,33,34,35

规则：`i < n/8 || i >= 7n/8 || i % 3 == 0`。首尾层离输入/输出最近，误差影响最大；
中间每三层留一个更高精度。`_plan()` 里已实现，`test_q4_k_m_layer_rule_matches_official`
拿这串数字钉死——期望值是从官方文件里数出来的，不是推导的。

**Q4_K_M 的 `output.weight` 必须留在 Q6_K。** 这条是用户问出来的：`_plan()`
里只判了 `attn_v` 和 `ffn_down`，`output.weight` 一路走到 `return "q4k"`。
官方的 Q4_K_M 里 `output` 是 Q6_K、`token_embd` 是 Q4_K——区别在于 embedding
之后还有几十层在洗，lm_head 之后什么都没有，4 bit 的噪声直接进 logits。

Q6_K 档不受影响：那一档官方就是全部 Q6_K，也没有任何 Q8_0 升级。

### 22.11 顺带发现：`llama install` 现在装不上

跑验证时顺手试了 `python -m tagger.cli llama install`，失败：

    RuntimeError: GitHub 上这个 release（v0.9.0）没有匹配 Windows/AMD64 的安装包

查了一下，`ggml-org/llama.cpp` 的 latest release（v0.9.0）现在**只有一个
`nightly-tag.txt` 资产**，二进制不在 release 里了（`tags/nightly` 也是 404）。
所以 `llama_runtime` 里那套"读 latest release 的 assets 找 exe"的逻辑已经失效，
装不了 llama-server。也用不了 `--zip` 之外的自动路径。

要修的话是独立一件事：要么改指 nightly 构建的实际地址，要么让用户手动下 zip
再 `--zip`。这条不影响量化（已经不依赖 llama.cpp 了），但影响默认 NL 引擎。

---

## 22. 2026-11-04：install.bat 不装 torch，第一次打标必然失败

**现象**：别人按标准流程跑完 `install.bat`，新建打标任务，任务直接失败：

```
tag 后端不可用：pixai tagger 需要 transformers/torch/Pillow：
  pip install torch torchvision transformers timm
```

**根因**：`requirements-embedded.txt` 的注释里写着"torch/transformers 按需
自己装"，`install.bat` 也就没装。但**默认 tag 后端是 pixai**，它必须要
`transformers` + `torch` + `timm`。于是"按需"这两字在现实里等于"必然漏装"：
用户没有任何理由知道这句话该去哪敲，报错又只在他已经等着结果的时候出现。

顺带发现开发机自己也没有 `transformers`——不是某个人装漏了，是清单从来没
包含过它，只是开发机早年手动装过其余几个，把缺口掩盖了。

**修法**：

1. 新增 `requirements-tag.txt`（tag 反推运行时）：`torch` / `torchvision` /
   `transformers` / `timm` / `safetensors`，版本钉死（环境是产出的一部分，
   换 torch 版本可能换内核与数值）。**这一版钉的版本是错的，以 §23 为准。**
2. `install.bat` / `install.sh` **默认装这套**，`-SkipTag` / `--skip-tag` 跳过。
   只想打包或跑 NL 的人不该等这十分钟。
3. **torch 的 wheel 不在 PyPI**（CUDA 版在 `download.pytorch.org`），所以分
   两批装：`torch`/`torchvision` 走 PyTorch 索引，其余走普通索引。分批评
   的依据是包名前缀，版本号仍然只有清单一处。有 `nvidia-smi` 用 `cu124`，
   没有用 `cpu`；换 CUDA 版本用 `-TorchIndex`。
4. **装不上只警告，不中断**。这一条是刻意的：torch 装不下时打包与 NL 仍然
   可用，把它变成硬失败等于让人为了一个可选依赖连界面都进不去。离线模式下
   wheel 目录里没有 `torch-*.whl` 同样只警告并跳过。
5. `pack-dist.bat` 的复制清单加上新文件（漏了的话发布包里 `install.bat`
   找不到清单，表现是"又回到手动装"）。
6. `tagger doctor` 多一行 tag 运行时体检：缺包直接说缺哪个、怎么补。这几个
   包是懒加载的，缺了不影响命令启动，只会在真正跑任务时炸——而那通常是
   "别人机器跑了一夜、早上全失败"的场景。
7. 报错文案从 `pip install torch ...` 改成"重跑 install.bat / 见
   requirements-tag.txt"：让人照着自己机器上的装法走，而不是猜路径。

8. `prune-env.bat`：删除组补上 `transformers` / `tokenizers` / `regex`（以前
   prune 后它们留在发布包里白占一百多 MB，而 tag 后端已经因为缺 torch 跑不了），
   恢复提示改指 `requirements-tag.txt`。

**验证**：`install.bat -SkipTag` 实跑退出码 0（脚本分支与提示都对）；
`pip install --dry-run -r requirements-tag.txt` 在 PyPI 上五个版本全部可解析。
`cu124` 索引的 dry-run 本机网络太慢没跑完，版本组合按 PyTorch 官方配套表
（torch 2.5.1 ↔ torchvision 0.20.1）。

**别忘了**：老环境不会自己变好。已经装过的人要重跑一次 `install.bat`（幂等），
或者看 `tagger doctor` 那一行缺什么补什么。

---

## 23. 2026-11-04（续 §22）：安装脚本装齐依赖，打包维持精简

§22 只补了 pixai 需要的那五个包。这一轮把"装齐"做实：`install.bat` /
`install.sh` 装**代码里 import 到的全部第三方包**，`pack-dist.bat` 与
`prune-env.bat` 保持上一轮的样子不动（发布包仍然瘦，接收方跑一次
`install.bat` 就补齐）。

### 23.1 先扫再列，别凭印象

用 AST 扫 `tagger/**/*.py` 的顶层 import，减去标准库与 `tagger` 自身，得到
真实依赖全集：`PIL` `datasets` `fastapi` `httpx` `huggingface_hub`
`llmcompressor` `numpy` `onnxruntime` `open_clip` `packaging` `pyarrow`
`pydantic` `safetensors` `timm` `torch` `torchvision` `transformers`
`uvicorn` `yaml`。对照两份清单，缺的是：`datasets` `onnxruntime`
`open_clip_torch`（`huggingface_hub` 由 transformers 带进来）。

### 23.2 §22 钉的版本是错的，这次按本机实测

§22 我凭印象写了 `torch==2.5.1` / `torchvision==0.20.1` / `timm==1.0.11`。
本机 `env\python` 里实际是：

```
torch 2.14.1+cu126   torchvision 0.29.1+cu126   timm 1.0.30
safetensors 0.8.0    datasets 5.1.0
```

差了一整个大版本。照 §22 那份清单装，会把开发机上本来能跑的 torch **降级**，
而且 cu124/cu126 一换，权重与内核行为都跟着变。现在清单按实测值钉死，
默认索引也从 `cu124` 改成 `cu126`（实测就是 cu126；换版本仍用 `-TorchIndex`）。

**教训**：钉版本要读 `site-packages/*.dist-info` 或 `import x; x.__version__`，
不能凭记忆——钉错的版本比不钉更难发现，因为 `pip install` 会安静地降级。

`transformers` / `onnxruntime` / `open_clip_torch` 本机从没装过，所以这三行
给范围（`>=4.46,<6` / `>=1.20` / `>=2.30`）而不是精确值：钉一个没验证过的
精确版本，报错会和"缺包"长得一模一样。等有人真跑通了再收。

### 23.3 自检区分"必须"与"可选"

`setup_embedded_env.ps1` 装完会探一遍：`torch` / `torchvision` /
`transformers` / `timm` / `safetensors` 缺任何一个 → 警告（打标会失败）；
`onnxruntime`（cl-tagger-v2 后端）、`open_clip`（图文一致度打分）、
`datasets`（HF 输入源）缺了只打一行"可选缺失"，不影响判定——否则一次网络
抖动就会让人以为整个环境坏了。

### 23.4 仍然故意不装的

- `llmcompressor`：4bit 量化那条路，几百 MB，对 torch 版本卡得死，只在做
  量化那一次需要。
- `vllm`：只有 Linux 能装，而且它会强制换掉上面钉好的 torch 版本——
  默认引擎已经是 llama.cpp，没必要为了备选引擎砸掉主路径。

### 23.5 验证

- `install.bat -SkipTag` 实跑退出码 0（文案与分支都对）
- `pip install --dry-run -r requirements-tag.txt`（PyPI）→ 全部可解析
- `pip install --dry-run torch==2.14.1 torchvision==0.29.1 --index-url
  https://download.pytorch.org/whl/cu126` → 可解析
- `pytest tests -q` 全绿；三个 `.bat` 仍是纯 ASCII

---

## 24. 2026-11-07：jsonl 产物形态（第三种落盘）

`--artifact-format jsonl`（WebUI「打标产物 → 产物形态」第三项）。与 parquet
**同一个 sink 接口**（`add / flush / close / rows_per_file`），一行一条，
一片一个 `artifacts-NNNNN.jsonl`。

### 24.1 为什么还要一种

| 形态 | 文件数 | 依赖 | 坏一行的后果 | 体积 |
|---|---|---|---|---|
| 逐图 json | 6000 万个小文件 | 无 | 只坏那一张 | 1×（但有簇浪费） |
| parquet | 降 4 个数量级 | pyarrow | 整片读不出来 | 最小（zstd） |
| jsonl | 降 4 个数量级 | 无 | 只坏那一行 | ≈ parquet 的 1.5~2 倍 |

嵌入式环境里 pyarrow 是可选依赖，而分片是**不可分割的二进制**：小半截坏掉
整片都读不了，还得有专门的读取器才能看见里面是什么。jsonl 用标准库写、
用 less/grep 看，跨机器搬运不挑环境。

### 24.2 三条不许动的口子

1. **行结构逐字段一致**：`image_id / tag / nl / ts`，`tag`/`nl` 仍是
   `json.dumps(payload)` 的**字符串**，空串表示该侧没产出。合并的对账逻辑
   全建立在 payload 上，拆成嵌套对象等于把那段逻辑复制两份。构造函数
   `artifacts._row()` 是唯一出处，两个 sink 都调它。
2. **写整片再原子替换**（`*.jsonl.tmp` → `replace`）：崩在写一半不会留下
   半截分片被 merge 当成正常产物。`newline="\n"` 写死，跨平台字节一致。
3. **编号各形态独立**：`_next_seq(out_dir, suffix)` 只数同后缀的片，
   parquet 编到 00007 不影响 jsonl 从 00000 起。

### 24.3 读侧（一处改、四处生效）

`artifacts.find_artifacts(root)` + `artifacts.iter_artifact_rows(path)` 按后缀
分派，下面这些都改成了它，所以新增形态只改这两处：

- `merge._iter_shard_records`（对账）
- `report.collect_points`（报告）
- `tag_manage.kind_of / scan / _load_shard`（整理页；`artifacts-*.jsonl`
  才算分片，`failures.jsonl` / `manifest.jsonl` 不算）
- `verify.list_shards / _iter_jsonl`（复核；`_row_members` 与 parquet 共用）

`load_done_ids(out_dir, fmt="all")` 默认**两种形态取并集**：续跑只关心
"这张做过没有"。换了形态重跑时若只认本形态，做过的图会被重做，而重做出来
的重复行在 merge 里会变成要人工裁决的内容冲突。

### 24.4 坏行口径（和 parquet 不同，故意的）

- 末尾那行没有换行符 → 写到一半被打断的残留，**跳过**（`return`）。
- 中间的行不是 JSON → **报错并带行号**。静默跳过会让 merge 悄悄少几张图。
- `load_done_ids` 里又宽一档：坏行一律跳过。那是"提示"不是数据，
  宁可多打一张，不该让一次启动因为一个残行整个失败。

### 24.5 验证

- `pytest tests/test_artifacts_jsonl.py`（这个文件**不 importorskip pyarrow**，
  其中一条用 `builtins.__import__` 模拟没装 pyarrow，把"纯标准库也能跑"
  这条卖点钉住）
- `tests/test_artifacts.py` 里补了"parquet 与 jsonl 给同一结论"与
  "done_ids 取并集"
- `cd tagger\web\ui; npm run build`（tsc --noEmit 一起过）

### 24.6 定名与默认值（同日追加）

**本地统一走 jsonl，一包一文件**：`artifacts-<分包名>.jsonl`。

- 名字取 `TaskRow.shard`（分包 zip 的路径）的 stem，经 `safe_stem()` 清洗
  （Windows 九个非法字符/空白换 `_`，**中文保留**，首尾点线去掉，截 60 字）。
- **按包切而不是按 job 切**：成员机常把几十个 zip 丢进一个输入目录、写同一个
  产物目录，`MultiShardSink` 惰性为每个来源包建一个 `JsonlSink`，换包时先刷
  上一个（崩了只丢当前这一包）。两个目录里同名的 `dist-01.zip` 会退成
  `artifacts-dist-01-2.jsonl`，不合并。
- **续写默认开**：同名文件已存在就往后 append，暂停后继续不多出一片。
  `--force` 例外——它会重做已完成的图，续写就重复，所以 `append=False`，
  已存在则退成 `-2`，**绝不覆盖**。
- 定名模式下 `rows_per_file` 只决定"多久落一次盘"，**不换文件**；编号分片
  （`artifacts-00000.jsonl`）仍是 `stem=""` 时的行为，脚本与测试用它。
- 默认值三处一起改成 jsonl：`cli.py --artifact-format`、`jobs.JobSpec`、
  `app.JobRequest`。**parquet 只留给服务器封包分发**（`pack-*.parquet` /
  `dist-*.zip`），本地不再默认产它。
- 回传页（`/api/artifacts`）不再只认 `artifacts-*`：成员的流程是"本地合并成
  自己的总表 → 发总表 → 服务器再合一次"，所以 `merged.jsonl` 必须能选到；
  只排除 `failures.jsonl` / `manifest.jsonl` / `state.jsonl` 这三类非产物。

### 24.7 已知边界

- 一个 client 进程传多个 `--shard` 时是**一条任务流**，但按包切文件是按
  `TaskRow.shard` 分的，所以仍然一包一文件 ✓。
- 定名模式的落盘是 append，不是原子替换：崩在写盘途中只会在**末尾**留残行，
  读侧本来就跳过"没有换行符的最后一行"，所以不需要额外清理。
- 旧的 `artifacts-00000.jsonl` 若与新命名的片同目录，合并会拿到两份同样的图；
  换新命名时把旧片挪走或换产物目录。

---

## 25. 2026-11-07：冲突与决策（同 id 两份不同内容）

同一个 `image_id` 被打了两遍、内容还不一样（换模型 / 换 prompt / 新旧版本），
合并会报"内容冲突"并**取第一份**。以前只能靠人去翻 `duplicates` 里的文件名，
现在有页面可以逐条比、逐条批。

### 25.1 什么算冲突（口径）

只收**内容不同**的组合。内容相同的是幂等重复（重跑没清目录），取一份即可 ——
把人塞进待办只会让他以为真出了问题。扫描口径与 `merge.collect` 一致：
`tag` 取 payload 的 `tag`，`nl` 取 `caption`，逐图 json 与两种分片都认。

### 25.2 决策存哪、按什么匹配

`work/conflicts/decisions.json`，键 `image_id|kind`，值是**选中那份内容的
sha1 前 16 位**，不是下标。理由：重扫一次候选顺序就可能变（多了一个文件、
路径排序变了），按下标存会把决定悄悄套到另一份内容上；按哈希匹配要么命中
要么明确失效。

`merge.collect(..., decisions=picks)` 命中时按批的取那一份，记进
`MergeStats.resolved`，**不再算失败** —— 否则人点了按钮、合并照样报错，
那按钮就白点了。

### 25.3 三种策略

`manual`（默认，什么都不自动做）/ `newest`（`ts` 最新）/ `longest`（正文字符
最多）。后两种在页面上只是"一键批量建议"，落进 decisions 文件，合并时才生效 ——
所以点完还能逐条反悔。

### 25.4 位置

- 后端 `tagger/conflicts.py`：`scan / strategy_pick / decide / auto_decide /
  picks_for_merge`，索引落 `work/conflicts/conflicts.json`（页面不用重扫）
- 页面「冲突与决策」（侧栏，紧邻「整理」）：`GET /api/conflicts`、
  `POST /api/conflicts/scan`、`POST /api/conflicts/decide`、
  `POST /api/conflicts/auto`
- 内存里只留冲突项，正常数据不存内容 —— 否则百万条会把内存吃光

---

## 26. 2026-11-07：安装链路补齐（llama.cpp + 依赖清单漏项）

上一轮实测"新机器按 README 走一遍能不能用"时发现的三个洞。都不是新功能的
问题，是**装完以为能用、其实不能用**的那类。

### 26.1 llama.cpp 进了 install.bat / install.sh

`llama.cpp` 是默认 NL 引擎，但它不是 pip 包：`env\python` 装完仍然没有
`llama-server.exe`，点「启动服务」直接失败。以前只靠"网页上有个按钮"提醒，
等于把最后一步留给用户自己发现。

现在两个安装脚本都会在依赖装完后调一次 `tagger.cli llama install`
（与网页按钮、CLI 共用 `tagger/llama_runtime.py` 同一份实现，同一落点，
不会出现"网页装好而 CLI 说没装"）：

- 先查 `status()["installed"]`（纯本地、不联网），已装就不重下 570MB
- 下不了只警告不中断 —— 和 tag 运行时同一个理由：只影响 NL
- 新增开关 `install.bat -NoLlama` / `install.sh --no-llama`
- 自检末尾会报**模型权重在不在**，因为 `tiers.yaml` 默认指向 abliterated
  破限版，那是几 GB 的独立下载，装完环境并不等于能打标

### 26.2 依赖清单补了四个包

以前它们靠"开发机手动装过"活着，干净机器上会炸。`tests/test_requirements_cover.py`
现在会扫 `tagger/**.py` 里所有 import（含函数体内的延迟 import），
清单没覆盖就失败 —— 这类漏项靠人眼复查必漏。

| 包 | 为什么必装 | 缺了的表现 |
|---|---|---|
| `pyzipper` | AES-256 zip（7-Zip/WinRAR 默认） | `_open_zip` 的兜底路径没有 try，直接 ImportError |
| `py7zr` | 7z 解包 | `_names_7z` 静默返回空 → "这个包里一张图都没有" |
| `huggingface_hub` | `cpu_tagger` 按仓库名下模型；HF token | `TaggerUnavailable`，几个 CLIP 打分模型加载不了 |
| `psutil` | 拆包时按可用内存砍并发 | 被 `except` 吞掉，内存不足时整机被拖死而不是报错 |

`rarfile` 仍然不进清单：它只是外壳，真解压要外部 `unrar.exe`，
`describe_backends()` 会照实报"需要外部 unrar"，不伪装成可用。
`llmcompressor` 同理（`pyproject` 的 quantize extra）。

### 26.3 prune-env.bat 不再删掉能用得上的东西

`G_HF` 原来会删 `huggingface_hub` **和它的硬依赖** `filelock/fsspec/tqdm/
requests` —— 裁完得到一个 `import huggingface_hub` 会抛 ImportError 的树。
`KEEPNAMES` 是**精确匹配**（`for %%K in (...) if /i "%~1"=="%%K"`），
所以前缀组里的名字必须逐个确认不会把依赖连坐删掉。

同时把 `pyzipper/py7zr` 及其解压后端、`psutil`、`huggingface_hub` 的依赖
加进 KEEPNAMES，自检也扩到 `psutil, pyzipper, py7zr, huggingface_hub`。

### 26.4 pack-dist.bat

- 新增 `-WithLlama`：默认**不带** `runtime\llama.cpp`（743MB，且 CUDA dll
  与接收方驱动绑定），由接收方的 install.bat 自己下；只有离线目标或必须
  钉死构建时才用。`runtime\llama.cpp\_dl\` 里那 570MB 原始 zip 永远排除。
- `HANDOFF.md` 从根目录清单里去掉：它实际在 `devtools\`，那条 `if exist`
  一直是死的（`if exist` 会把"文件挪走了"变成静默跳过）。
- `pack-readme.txt` 补了第 4 步（下权重）和两条排错（GGUF 缺失、llama 没装）。

### 26.5 验证

- `pytest tests/test_requirements_cover.py`（4 条，新增）
- 全量：653 通过，4 条失败仍是既有的（`hf_downloads` / `tag_golden` /
  `web_models` ×2），与本节无关
- `prune-env.bat` 用脚本复现其匹配逻辑做过 dry-run：55 项待删，关键包全保留
- `pyzipper==0.4.0` / `py7zr==1.1.4` / `huggingface_hub==2.4.0` /
  `psutil==7.2.2` 已按本机实测版本钉死，`pip install` 可解析

## 27. 2026-11-09：转换脚本内置，以及 `conversion` 报错的真正原因

### 27.1 现象

「本地量化」点下去：下载 37 MB 源码包 → 日志写"转换脚本就绪" → 紧接着

    File "...\runtime\llama.cpp\convert\convert_hf_to_gguf.py", line 18
        from conversion import (
    ModuleNotFoundError: No module named 'conversion'

而 `tools_ready()` 明明是 True（三个文件都在）。于是 §24.1 的"自愈重下"又跑
了一遍，重新下了 37 MB，同一个错误。用户看到的就是同一个错误刷两次。

### 27.2 真因：内嵌解释器是 isolated 模式

    > env\python\python.exe -c "import sys; print(sys.flags)"
    sys.flags(... ignore_environment=1, isolated=1, safe_path=True ...)

目录里放着 `env\python\python312._pth`（内容：`python312.zip` / `.` / `Lib` /
`Lib\site-packages` / `import site`）。**一旦有 `._pth`，Python 进入 isolated
模式**，两条后果，任何一条都足以让转换跑不起来：

1. `ignore_environment=1` → **PYTHONPATH 被整个忽略**，父进程里拼得再对也不进
   `sys.path`。§24.1 的修法在这里等于没写。
2. `safe_path=True` → **脚本自己所在的目录也不进 `sys.path`**（`sys.path[0]` 被
   拿掉），于是官方脚本第 18 行 `from conversion import ...` 找不到同目录的包。
   它只给自己补了 `gguf-py`（第 15 行），从没管过 `conversion`。

一句复现（用的是*那个*解释器，不是系统 python）：

    > python -c "import sys,os; e=dict(os.environ); e['PYTHONPATH']=r'...\convert'; \
        print(subprocess.run([sys.executable,'-c','import conversion'],env=e).returncode)"
    1        # 依旧 No module named 'conversion'

### 27.3 修法

**a) path 只能在子进程里补** —— 新增 `tagger/convert_runner.py`：

    sys.path[:0] = [工具目录/gguf-py, 工具目录]   # 先补，再跑
    sys.argv = [脚本, *其余参数]
    runpy.run_path(脚本, run_name="__main__")

`_run_script` 改成
`python -u convert_runner.py <工具目录> <src> --outfile ... --outtype f16`。
参数是列表直传（不过 shell），带空格/中文的路径不用转义。**以后别往子进程塞
PYTHONPATH 了**，对这个解释器它永远是空的。

**b) 脚本内置** —— `vendor/llama-convert/`（`convert_hf_to_gguf.py` +
`conversion/` + `gguf-py/`，2.1 MB / 143 文件，纯 Python，随仓库与发布包走）：

- `gguf_convert.tools_dir()`：**内置优先** → 回落 `runtime\llama.cpp\convert`；
  `_complete()` 三个文件都验，`ensure_tools()` 见内置齐了就直接返回，绝不下载；
- 内置那份"缺模块"**不触发**重取（重下也修不好，只会白等 37 MB）；
- `snapshot()` 多一个 `bundled` 字段，页面/日志能看出这次用的是哪份；
- `pack-dist.bat` 无条件带上 `vendor\llama-convert`（跟 `-WithLlama` 无关：要的是
  2 MB 的 .py，不是 743 MB 的 CUDA 构建），并且**缺了直接 abort**——否则接收方
  第一次量化就得去 GitHub 拉 37 MB，正是国内最容易卡住的一步。

### 27.4 校验

- `pytest tests/test_gguf_convert.py -q` → 16 passed（新增 3 条：内置目录齐、
  内置缺失时回落、runner 能把 `conversion` 放进 `sys.path`）
- 真跑一次内置脚本：`python -u tagger/convert_runner.py vendor/llama-convert --help`
  → 正常打出 usage（`import torch`、`from conversion import ...`、`import gguf`
  全过），这就是这条链路的实机证据
- 打包：`pack-dist.bat` 的 preflight 会先查 `vendor\llama-convert\convert_hf_to_gguf.py`

## 28. 2026-11-09：内置脚本跑通了，然后卡在 sentencepiece

### 28.1 现象

§27 修好之后第一次真跑（qwen3vl-8b-abliterated），已经能看到脚本从
`vendor\llama-convert\` 启动、`conversion` / `gguf` / `torch` 全 import 通过，
然后：

    File "...\conversion\qwen.py", line 67, in set_vocab
        self._set_vocab_sentencepiece()
    File "...\conversion\base.py", line 2548, in _create_vocab_sentencepiece
        from sentencepiece import SentencePieceProcessor
    ModuleNotFoundError: No module named 'sentencepiece'

### 28.2 为什么漏了

`convert` 这个 extra 原来只写了 `torch` / `transformers` / `safetensors`，
而 `sentencepiece` 是**特定架构的 tokenizer 才需要**的：权重目录里有
`tokenizer.model` 时（Qwen 系、Llama 系都是），脚本走
`_set_vocab_sentencepiece`，否则走 BPE/tiktoken 那条。所以"我这台机器转成功过
一次"完全不代表别人不缺——机型不同，只是这次终于撞上了 Qwen。

更糟的是**发现得太晚**：这个 import 在读完 config + tokenizer 之后才发生，
用户已经等了几分钟，日志看着又像"少装了个普通 pip 包"。

### 28.3 修法

1. `pyproject.toml` 的 `convert` extra 加 `sentencepiece>=0.2.0` ——「依赖」页那
   一键装的清单是**直接读 pyproject** 的，写在这里两处同时生效。
2. `requirements-embedded.txt` 钉 `sentencepiece==0.2.3`（1.7MB 轮子）：转换脚本
   已经内置，这一颗补齐之后"第一次量化"才真正开箱即用；`prune-env.bat` 的
   PREFIXES 不含它，裁环境不会把它删掉。
3. `gguf_convert._REQUIRED` 加 `sentencepiece`：`capability()` 在**点按钮之前**
   就把缺包报出来（页面上是禁用按钮 + 原因），不再让人等几分钟后炸。
4. `gguf_convert._missing_module()`：日志末尾里认出 `No module named '<X>'`
   的那几个（sentencepiece / mistral_common / torch / transformers），
   `_run_script` 据此抛"缺 X 没装 + `python -m pip install X` + 装完要重跑"，
   而不是一句 `转换失败（退出码 1）`。

### 28.4 校验

- 实机装 `sentencepiece==0.2.3` 后，用真实权重目录只跑词表：
  `convert_runner.py vendor/llama-convert <model dir> --outfile x.gguf --vocab-only`
  → `EXIT=0`，`n_tensors = 0, metadata only`，产出 5.9MB 词表 GGUF。说明
  tokenizer 这条路径彻底通了（真转仍要十几分钟，没在这轮跑完）。
- `pytest tests/test_gguf_convert.py -q` → 18 passed（新增 2 条：缺包要点名、
  缺包要在长跑之前拦住；另 1 条断言 extra 里必须有 sentencepiece）

## 29. 2026-11-09：附加依赖组搬进「依赖检测」，缺依赖给两条路

### 29.1 问题

`convert` / `quantize` / `parquet` / `serve` 这四组依赖原来只长在各自的功能弹窗里
（「本地量化」一个按钮、「量化」一个按钮）。想"先把环境配齐再干活"的人打开
「依赖检测」，看到的是 core / tag / optional 三组——**这一页回答不了"附加依赖缺什么"**，
而那恰好是唯一能回答"整台机器缺什么"的地方。sentencepiece 那次就是活例子：
页面上一切正常，一转换才炸。

### 29.2 改法

后端 `deps.py`：

- `_EXTRA_GROUPS`（key / 标题 / 说明）从 `pyproject` 的 `[project.optional-dependencies]`
  现读，作为 `kind: "extra"` 的分区拼到 `snapshot()["groups"]` 末尾；
- 每组多两个字段：`extra`（`install-extra` 的参数）和 `missing`（组头角标用它）；
- 与主清单重名的包标 `shared: true`（torch 之类在两组都出现，卸载是同一个
  site-packages）；
- **不进** `install_missing`：那个批量是"补几个小包"，混进 llmcompressor（几百 MB）
  和 vllm（Windows 装不了）就是误伤。每组自己一个按钮。
- `verified`（导入自检）原来只贴给主清单的行，extra 行也要贴，否则附加依赖里
  永远看不到「导入失败」。

前端：

- `Deps.tsx`：组头 `trailing` 放「安装本组 N」，只在 `kind === 'extra'` 时出现；
  行上「共用」小标；
- `GgufConvertDialog` / `QuantizeDialog`：缺依赖时**两条路并排**——就地点
  「一键安装（xxx 依赖组）」，或者「去「依赖检测」里装」（`location.hash = '/deps'`，
  切路由后弹窗随页面卸载，不用手动关）。报错文本命中 `依赖 / pip install / 没装 /
  ModuleNotFoundError` 时，错误框下面也给同一个入口——§28 那种"跑挂了才发现缺包"
  就靠它落地。

### 29.3 校验

- `pytest tests/test_deps.py tests/test_gguf_convert.py -q` → 35 passed
  （新增 2 条：附加依赖组在快照里、组内缺数与行状态一致、共用标记；以及
  llmcompressor **不在**核心分组里）
- `/api/deps` 实测：`core / tag / optional / convert / quantize / parquet / serve`
  七个分区，convert 组 4/4（torch、transformers、safetensors、sentencepiece，全带 `shared`）
- 前端 `npx tsc --noEmit` 0 错、`npm run build` 通过（dist 已重建，服务端直接吃）

## 30. 2026-11-09：mmproj 把 F16 覆盖了；新建任务页的"能不能选"

### 30.1 「视觉投影文件（mmproj）不存在」的真身

现象：选 qwen3vl-8b-abliterated + Q6，报
`视觉投影文件（mmproj）不存在：...\mmproj-...-F16.gguf`；而模型目录里只有一个
**0.47G** 的 `-Q6_K.gguf`（正常 6.3G），没有任何 `mmproj-*.gguf`。

根因在 `gguf_convert._convert` 的第二趟：

- 官方脚本的 `mmproj-` 前缀**只在 `--outfile` 是个目录时**才自动加
  （`conversion/base.py:3175`，`MmprojModel.prepare_metadata` 的 `is_dir()` 分支）。
  传文件路径时它只走 `gguf.fill_templated_filename(name, outtype)`，而那个函数
  对**没有占位符**的名字原样返回。
- 我们第二趟传的是 `--outfile <model>/xxx-F16.gguf`，也就是**第一趟的 F16**。
  于是 mmproj（1.08G）直接覆盖了 15.6G 的 F16；接着量化把这 1.08G 的 mmproj
  当文本模型压，产出 0.44G 的"Q6"——和实测的 0.47G 对得上；`mmproj-*.gguf`
  则从头到尾没存在过，直到起服务才炸。
- 代码里那句 `else: 没有生成 mmproj——起服务后图片会无法识别` 只写日志不报错，
  整条链路看起来是"成功"的。

修法：

1. 第二趟 `--outfile` 直接用 `task.mmproj` 的路径（脚本对无占位符的原样落盘）；
2. 视觉模型**没导出 mmproj 直接报错**：llama-server 不带它也能起来，但每张图都
   被当纯文本，模型开始"看图说话"地编内容，比报错难查得多；
3. `_run` 的 finally：失败/取消时按 mtime 删掉**这次真写过**的产物（半截文件比
   没有更坏——它会让页面/清单都以为这一档好了）；
4. `_quantize` 加体积闸：产物 < F16 的 15% 判坏并删（Q4≈30%、Q6≈40%、Q8≈53%）；
5. `models._gguf_tiers_state`：有原始权重当基准时，GGUF < 20% 归入 `broken`，
   不再算"已压"，页面上说"文件不完整，重压一次"。

实机校验：用修好的命令对同一份权重单跑第二趟 →
`mmproj-qwen3vl-8b-abliterated-F16.gguf` **1.08G 正确落盘**（此前从未存在过）。

### 30.2 新建任务页：模型与精度都置灰，但都留了出口

- 后端 `_defaults().vllm_models` 补 `gguf_ready` / `gguf_broken` / `raw_ready` /
  `quantized`，口径与模型管理页共用 `models.weight_summary`（两页各算各的就会
  互相打脸）。
- **模型**从 `<select>` 换成一列卡片：不可用的整行灰掉、点不动，右侧给「下载」
  或「去量化」直接跳模型管理。原生 `<option>` 里放不了按钮，而"没下 / 没压"
  的每一条都需要一个可点的出口。`SettingRow` 因此新增 `wide`（独占整行）——
  右侧那 260~360px 的窄列塞不下一列卡片。
- **权重精度**：`ChoiceGrid` 新增 `optionDisabled` / `onBlocked`。没有对应文件的
  档置灰，但**仍然可以点**——点了在下面挂一条"这档还没压，去模型管理点量化"
  加跳转按钮。真设成 HTML `disabled`，用户只看到一个按不动的灰块，反而更困惑。
- **「这套权重还能不能用」改成按当前引擎判**（llama.cpp 看 `gguf_ready`、vLLM 看
  `raw_ready` / `quantized`），不再用 `ready`——那个是按 tiers.yaml 里写的默认档
  （f16）算的：只压了 Q6、也用 Q6 跑的人，原来会被挂一条"权重未下载"。

### 30.3 校验

- `pytest -q` 全量通过（新增：mmproj 第二趟必须写自己的文件名、半截 GGUF 不算
  ready、`/api/defaults` 带 gguf_ready）
- 前端 `npx tsc --noEmit` 0 错、`npm run build` 通过（dist 已重建）

## 31. 2026-11-09：Linux-only 的包在这台机器上不再能点「安装」

### 31.1 问题

`vllm` 在 `optional` 组和 `serve` 这个 extra 里都有一份，而它的轮子只有 Linux：
Windows 上 `pip install vllm` 会装出一个缺 C 扩展的壳，`import` 时报
`No module named 'vllm_C_stable_libtorch'`，还会顺手把钉死的 torch 换掉。
原来的页面把它当普通"缺失"包：红色角标、「安装」亮着、「安装缺失」把它算进
"缺 N 个"里，点下去必然失败一次，失败原因还指向一个跟用户操作无关的模块名。

### 31.2 改法

后端 `deps.py`：

- `_PLATFORM_ONLY = {"vllm": ("linux",)}` + `Item.platforms` / `Item.supported`
  （`sys.platform.startswith(...)` 判定）。键按分发名归一（小写、下划线当连字符），
  所以 `requirements` 和 pyproject 两条路径共用同一张表——只改一条的话，
  「安装本组」在 Windows 上照样会装。
- `describe()` 输出 `supported` / `platforms`；`install` / `reinstall` / `install_missing`
  / `install_extra` 一律先过 `supported`（原来 `install_extra` 是把 `serve` 这个名字
  硬编码进去的，现在按表走，加新包不用再改那种 if）。
- 快照里 `summary.blocked` 与每组 `blocked`：**缺、但本机装不了**的条数。
  `missing` 仍保持原样（行上的"缺失"角标和它一致），页面用 `missing - blocked`
  算按钮上的"要装几个"。
- `_verify_one` 对装不了的包直接判过：那不是环境坏了，判成失败会让「检查完整性」
  整批返红，用户照着修却无从修起。

前端 `Deps.tsx`：

- 行的副标题第一优先显示「仅 linux 可用」（压过"导入失败"与版本号），
  「安装」/「重装」按钮 `disabled` 并给同样口径的 `title`；
- 顶部「安装缺失」与组头「安装本组 N」都按 `canInstall = missing - blocked` 计数，
  全都不剩时按钮置灰，`title` 说"缺的 N 个只有 linux 版"。否则用户看到
  "缺 1 个"却点了报错。

### 31.3 校验

- `python -m pytest tests/test_deps.py -q` → 21 passed（新增 4 条：平台标记、
  `blocked` 计数与行状态一致、Windows 上 `install`/`reinstall` 被拒、
  `serve` 组不发起 pip）
- `snapshot()` 实测（win32）：`summary.blocked = 1`，`optional` 与 `serve` 两组
  各 `missing 1 / blocked 1`
- 后端 `ast.parse` 通过；前端 `npx tsc --noEmit` 0 错、`npm run build` 通过（dist 已重建）

### 31.4 顺带扫出来的同类问题（"点了必然失败"）

1. **网页装 torch 用的是 `cu124` 索引，而清单钉死的是 `2.14.1+cu126`。**
   实测索引目录：`whl/cu124/torch/` 最高只到 **2.6.0**、`whl/cu128` 也没有
   2.14.1（只有 `cu126` / `cu130` / `cpu` 有）。也就是说有 N 卡的机器在依赖页
   点「安装 / 重装 torch」必然 `No matching distribution found`。
   `setup_embedded_env.ps1` 改成 cu126 那次（§见 torch 版本钉死那节）只改了安装
   脚本，网页这一处漏了。修法：
   - `deps.py: TORCH_INDEX_GPU` → `.../cu126`，注释写清"按 CUDA 版本分目录、
     各放各的"；
   - 新增测试 `test_torch_索引贴着清单里实测的_cuda_版本`：从
     `requirements-tag.txt` 里读 `torch 2.14.1+cu126`，断言网页索引里含同一个
     `cu126`——两处再各写各的就会红；
   - 手抄命令的地方一并纠正：`prune-env.bat` 的 `cu124`、`embedded-env.md` 与
     `setup_embedded_env.ps1` 里的举例（原来举 cu124 / cu128，两个都装不了
     2.14.1）。
2. **本来就有拦截、不用动的三处**（口径都是"平台/能力不够就置灰并说明"）：
   llama.cpp 下载按 `snap.supported`（macOS 无官方二进制）；vLLM 启动按 `capOk`
   （`nl_engine.check_engine` → `model_loader.check_environment`）；量化按
   `available` + `missing_deps` 给「一键安装」（走的正是 `install-extra`，
   现在也过平台表）。
3. **留给以后的问题（不是 bug，是 knob）**：`_torch_index()` 只按"有没有
   `nvidia-smi`"分档，不看驱动版本。装了 cu126 但驱动太老，pip 阶段一切正常，
   要等 `import torch` 才报 driver 版本不足。要不要探测驱动版本、或把
   「检查完整性」的报错翻成人话，等真有人撞上再说。
   → 已按"要修"处理，见 §31.5。

校验：`python -m pytest tests/test_deps.py tests/test_web_features.py
tests/test_requirements_cover.py -q` → 42 passed。

### 31.5 驱动版本：有显卡也可能装不了 cu126

**问题**：索引选对了还不够。`nvidia-smi` 存在只说明"有卡"，不说明驱动支持
CUDA 12.6。驱动只到 12.4 的机器上，cu126 的轮子 pip 照样装得上，直到
`import torch` 才报 `CUDA driver version is insufficient`：pip 阶段一片绿、
页面显示"已装"，第一次打标才炸，两句之间没有任何线索。

**改法**：

- `deps.py` 新增 `torch_plan() -> (索引, 一句话)`：
  没卡 → CPU（无提示）；有卡但读不出驱动 CUDA → CPU + 提示；驱动 CUDA < 12.6 →
  CPU + "驱动只支持 CUDA 12.4……想用显卡把驱动升到 CUDA 12.6 以上"；够新 → cu126。
  退 CPU 而不是照装，是因为"能跑、只是慢"至少可用，且原因能写在页面上。
- `_driver_cuda()` 跑一次 `nvidia-smi` 解析表头的 `CUDA Version:`，结果**缓存
  60 秒**——依赖页在 pip 跑的时候每秒拉一次快照，不缓存会把轮询变成进程风暴。
- 快照多一个 `torch_note`：页面底部用 warning 色显示那原因；`_one()` 里装 torch
  时也往日志里打一条（事后翻日志能看出当时为什么装的是 CPU 版）。
- `devtools/setup_embedded_env.ps1` 同一口径：显式 `-TorchIndex` 优先；否则按
  驱动 CUDA 版本退 CPU 并 `Write-Warning`（原来只判 `nvidia-smi` 存不存在）。

**校验**：`pytest tests/test_deps.py -q` → 26 passed（新增 4 条：无卡 / 驱动太老 /
驱动够新 / 快照把 note 带出去）；本机实测 `_driver_cuda() = (13, 2)` → 走 cu126
（13.x 驱动能跑 12.6 的构建）；ps1 `Parser::ParseFile` 无错误，`12.4 → tooOld=True`、
`13.0 → False`；前端 `npx tsc --noEmit` 0 错、`npm run build` 通过（dist 已重建）。

## 32. 2026-11-10：从旧项目抽「post id → 画师」全表

**背景**：需求方（An）要 danbooru 源把画师写进 caption 正文（`@name`）、参考 tag
全量喂给模型、图里文字保原语言。旧项目 `MatchaCuratio/NewBie-Training-XML-Caption`
里有 8 份标注 json（最大 26 GB），画师在 `<artists>` 元素里——**不是 tag 串第一个**
（Danbooru 的 `tag_string` 是字母序，画师走 `tag_string_artist`）。

**做法**：`work/build_artist_table2.py` 流式读（字节级状态机切 JSON 对象 + HTTP
Range 断点续传；v1 直连 26 GB 中途被 reset 白跑一次），合并成
`work/danbooru_artists.parquet`：

| 项 | 值 |
|---|---|
| 唯一 id / 有画师 | 9,465,535 / 9,226,534（97.5%） |
| 体积 | 69.7 MB |
| 与第二批 768 分片抽样 8000 条比对 | **95.2%** 能查到画师 |

e621 排除，gelbooru 用 `sources` 单独标；空 `artists` 的行故意保留（区分"没标注"
与"表里没这条"）。字段说明见 `work/danbooru_artists_README.md`。

**踩到的坑**：远端执行机（121.225.25.33）**没有任何 Python**（`py.exe` 只是启动器，
全盘常见位置都没有解释器），GitHub 又不通，只能在开发机跑；源文件是 JSON 数组、
单条 3.6 KB，28 GB 整读不可能，必须流式。

### 32.1 分片（在那台机器上切）

远端后来从华为镜像装了 `C:\py312`（3.12.10）+ `pyarrow/huggingface_hub`（清华源），
画师全表经 scp 传过去（66 MB / 9.8 s），`work/shard_artists.py` 按 **tar 成员名**
（不是 manifest 字节数推算）切片：

| 项 | 值 |
|---|---|
| 分片 | 844（对应 768 分发，88 片已在 HF 上） |
| 行数 / 命中 | 8,005,010 / 7,763,950（**96.99%**） |
| 体积 | 104.4 MB（每片约 124 KB） |
| 耗时 | 17 分钟（单进程；瓶颈是磁盘，不是 CPU） |

产物在 `K:\anlia\_artist\shards\`，每个 parquet 带 `shard/rows/matched/built_at/source`
metadata 供加载时配对校验。**坑**：`Start-Process` 起的进程会随 SSH 会话结束被杀，
只有 21 个文件就停；换 `Invoke-CimMethod Win32_Process Create`（WMI）才活得下来。

### 32.2 工具侧：画师表 + 注入 + prompt v4

- `artist.py`：`ArtistRule.table`（单个 parquet 或目录）+ `ArtistTable`
  （目录形态只读当前分片那一份，id 从 `8349000` / `8349000.jpg` 都能取）。
  **分片要配对才用**：拿 parquet 文件级 `shard`/`rows` metadata 与请求的分片名
  比对（`shard_stem` 会把本地路径 / `hf://…#member` / `dist-0003.zip::成员`
  归一到同一个 stem），对不上直接拒答 + 报一次 `[artist][警告]`。错片的列名、
  行数、类型全都合法，只有编号是旧的，照用等于静默张冠李戴。
- `client.py`：`_artist_names` → 表优先、目录名兜底、都没有就留空；多画师**只注入
  第一个**（`@a`），全量留在 json 的 `artists` 字段，并标 `artist_source=table`。
- 默认值改成开：`ArtistRule.enabled=True`（`from_dict` 缺省也是 True），
  `configs/tiers.yaml` 同步；web 表单本来就是 `artist=True`。
- **tag 串也带画师**：`@name` 放在 tag 列表最前、`tags` 里给 `1.0`（训练时 tag 是
  条件，只写进 caption 等于没绑上）。NL 与 TAG 各一个开关（`inject_caption` /
  `inject_tags`），关掉时 `artist_tag` 字段仍在，只是不进文本。web 侧两个开关
  都要能从表单进 `JobSpec`（`JobRequest` 少一个字段就是"表单发了、任务收不到"）。
  注意 `@name` 不是 danbooru 词汇，下游若拿 WD 词表做校验会被过滤，
  这个约定要和训练侧对齐。
- prompt **v3 → v4**：① caption 段加"整体画风/媒介"一条（v3 一句都没有）；
  ② 加"引号内文字保持原语言、不翻译不罗马化"（v3 只写 English only，模型会把
  中文招牌译掉）；③ 新增 `<REFERENCE>` 段，把源数据原始 tag 喂进去（`_ref_block`，
  没 tag 时一个字不加）。`TaskRow.ref_tags` 从 manifest 的 `tags/tags_original/
  danbooru_tags` 读。
- `quality.py`：`_cjk_ratio` 默认剔除引号内引用——否则英文 caption 正确引用中文
  招牌会被判成中文，进而跳过 tag 覆盖率、按 0.45 缩字符线。
- `configs/tiers.yaml`：`artist.table` 注释了两种写法；web 任务经
  `_artist_context` 把配置里的 table 传进 `ArtistRule`。
- 测试：`tests/test_artist_table.py` 17 条（查表/串片/回退/多画师/参考段/引文不判中文），
  全量 `pytest -q` 通过。

## 33. 2026-11-10：画师整流器 + 在线画师表 + 画师随图走

**背景**：画师输入实际有四种形态（目录名、danbooru 表、封包产物里的 artist 列、
手工整理的表），分隔符/命名/有无 id 全不一样；而"图片和画师打在一起"这件事，
打包产物其实早就有位置（parquet 的 artist 列、webdataset 的 `<stem>.json`），
缺的是**封包时不去查表**和**打标时优先级反了**。

### 33.1 整流器 `tagger artist-import`（新文件 `artist_import.py`）
任何格式 → 一种 schema：`id:int64 / artists:list<str> / sources:list<str>`
+ metadata（`shard`/`rows`/`built_at`/`source`），与 `work/shard_artists.py` 同构。

- 吃 csv / tsv / jsonl / txt / parquet；`id,names` 与 `names,id` 两种列序都认
  （按"哪边是纯数字"判断，不靠列序）。
- 多画师按 `& , ; | 、` 切；**不按 `/` 切**——`hatsune_miku_(cosplay)` 会被劈成两个。
- 清洗走 `clean_artist_name`（与注入侧同一口径），别名走 `AliasTable` 且只映射
  **切分后的每一段**（整段查在 `a & b` 上永远不命中）。
- 同 id 不同画师**默认失败**，`--on-conflict first|last` 才覆盖。失败在写盘之前，
  不会留半份表。
- `--split-by-dir <tar目录>`：按分片**实际成员**切片，每个成员写一行（没画师也写
  空列表），所以 `rows` == 成员数，`ArtistTable` 的配对校验直接能吃。

### 33.2 在线画师表 `hf://datasets/<repo>/<文件或目录>`
`ArtistTable` 现在三种形态：本地单表 / 本地分片目录 / 在线（单表或分片目录）。
带 `.parquet` 是单表，其余当分片目录——猜错的代价是整批静默无画师，所以写法
必须有唯一解。用 `hf_hub_download` 按需拉，缓存落 `run/hf-cache/<repo>--artist`。
token 走 `HF_TOKEN` 环境变量，**不进 ArtistRule**（那字段会进任务快照与历史记录）。
远程拉不到要记住（`_failed`）：仓库里没有这片就是没有，否则每张图重试一次
等于几十万次 404；本地相反（文件可能边跑边拷）。

**组合矩阵（现在是 4/4）**：本地分片 / 在线 tar × 本地表 / 在线表 全部对得上。
唯一仍不支持的是 **HF parquet 流式 + 画师表**：那条路径的 `task.shard` 是仓库里的
`pack-00000.parquet`，不是 webdataset 分片名。画师随图走（33.3）之后这条路不需要表。

### 33.3 画师随图走（封包烧表 + 包内优先）
- `plan_inputs(artist_table=...)`：表优先、目录名兜底，来源写进 `TagItem.artist_source`
  （`table` / `path`）。`packaging.run_pack` 与 `split` 都把配置里的 `artist.table`
  传进来。
- 产物带来源：pipeline 的 meta 加 `artist_source`/`artists`，`pack.py` 的 parquet
  加 `artist_source` 列，`distribute` 的 manifest 也加。
- 打标侧优先级改成 **包内 table 口径 > 查表 > 包内 path 口径 > 空**。
  理由：包内 table 值是逐图确认过的，再按分片查一次只会把它换成依赖分片命名的
  推测值；数据一重新切片，分片名对不上就全错。目录名没有这个特权。
- 分包阶段拿不到分片名（分包完才知道谁在哪片），所以那里只对全表生效，
  检测到分片目录会打一条日志说明，不静默。

### 33.4 校验
`tests/test_artist_import.py` 15 条 + `tests/test_artist_table.py` 37 条
（新增 20 条：在线地址解析/按需拉取/失败不重试/配对校验/包内优先级/封包查表）。
全量 `pytest -q` 878 passed、`read_lints` 0。CLI 冒烟：
`python -m tagger.cli artist-import --in a.csv --out all.parquet` → 2 id / 3 名字。

### 33.5 webui：画师表从"只能改 yaml"变成表单字段
之前 `artist.table` 只有 `configs/tiers.yaml` 一个入口，页面上的画师开关只管
"要不要注入"，管不了"名字从哪来"——换数据集就得改 yaml，而改 yaml 会把上一个
任务的口径一起改掉。现在三个页面都能填，**空 = 回落配置**：

- 打标页（`NewJob.tsx`）：本地与 VLM 两个分支各一个「画师表」输入框，
  共用同一个 state；`/api/preview` 也带上 `artist_table`，否则预览按目录名算、
  实际跑却查表，名字对不上（而这个预览正是"发现命名不对"的唯一防线）。
- 数据打包页（`Packaging.tsx`）：分包与封包各一个。分包阶段它影响 image_id 的
  排序键（画师是 `(artist, rel_path)` 的第一项），封包阶段它决定"跟着图封进去
  的画师是谁"，两边都必须与打标那次一致。
- `/api/datajob/preview` 同样接表，否则分包预览会少报一大截画师覆盖。

后端链路：`JobRequest.artist_table` → `JobSpec.artist_table` →
`_artist_context`（`spec 覆盖 yaml`）→ `ArtistRule.table`；打包侧
`SplitParams/PackParams.artist_table` → `rule.table`。
`/api/defaults` 返回配置里的值，前端只拿来做**占位提示**、不预填——预填之后
"空 = 跟配置"这个语义就失效了。

校验：`npx tsc --noEmit` 0 错、`npm run build` 通过（dist 已重建）、
全量 `pytest -q` 884 passed。冒烟：`/api/preview?artist_table=...` 的
`samples[0].artist` 从空变成表里的值。

## 34. 2026-11-10：新建任务页的参数按引擎归属（vLLM / llama.cpp）

**背景**：「批调度」那块把两套引擎的开关摆在一起，只有槽位做了条件渲染。查下来的
归属是这样的（依据 `model_loader.build_command` 与 `nl_engine.build_command` 两条
命令的实际拼装）：

| 前端字段 | vLLM | llama.cpp |
|---|---|---|
| 上下文长度 | `--max-model-len` | 只当每槽上限，且被 `CTX_PER_SLOT=3072` 截断 |
| max_num_seqs | `--max-num-seqs` | 等价于 `-np`（2026-11-12 起，见 §36；此前是假生效） |
| max_num_batched_tokens | `--max-num-batched-tokens` | 不生效（llama-server 的 `-b/-ub` 是另一套口径，未接） |
| 显存占比 | `--gpu-memory-utilization` | **没有这个语义**：显存只能靠槽位/上下文/KV 量化间接控 |
| 槽位 slots | 传了会被后端直接拒绝 | `-np` |
| file:// 放行目录 | `--allowed-local-media-path` | **没有这个开关**，图片只收 base64 |
| `-ngl 99`、`-ctk/-ctv q8_0` | — | 固定值，不是可调项 |

**真问题不是"少了几个框"，是 max_num_seqs 在 llama.cpp 下的假生效**：前端照传 →
进 `profile.max_num_seqs` → 进 `fingerprint_env` → 被 `client.resolve_concurrency`
当成"服务端愿意同时处理多少条" → 决定任务并发。可 `nl_engine.build_command` 根本
不读它，真实并发是 `slots`（16G 卡自动 12）。于是改这个框能改指纹、能改在途数，
唯独改不动 llama-server：12 槽的机器按 8 条发，白丢约三成吞吐。

**做法**：

- 指纹补 `engine` 与 `slots`（`ServerHandle` 加两个字段；`start_server` 从刚拼好的
  命令里回读 `-np`，不另算一份 `slot_plan`，避免两个真相源）。`resolve_concurrency`
  按 `engine` 取值：llamacpp 读 `slots`，其余读 `max_num_seqs`，无 `engine` 的旧指纹
  行为不变。
- 前端：`max_num_seqs` / `max_num_batched_tokens` / 显存占比 / file:// 四项只在 vLLM
  引擎下渲染；折叠摘要与「模型」行的「默认并发」按引擎改读槽位；分档下拉里的
  「并发 8」也是 vLLM 口径，llamacpp 下不显示。启动/预览请求不再把这三项发给
  llamacpp（`local_media_root` 同样只给 vLLM）。
- 后端：llamacpp 收到这三个参数时往服务日志写一行"已忽略"，不再静默吞掉。

`-ngl 99` 那条注释解释了为什么显存占比无法"接上"：`-ngl` 是"多少层上卡"，一旦
小于总层数就退化成 CPU 推理（PCIe 往返让 decode 掉一个数量级），没有"调小一点、
温和换显存"的中间档。

**校验**：`npx tsc --noEmit` 0 错、`npm run build` 通过（dist 已重建）；
`pytest tests/test_web.py tests/test_cli.py tests/test_client.py tests/test_nl_engine.py
tests/test_throughput.py tests/test_web_vllm.py` → 142 passed / 0 failed
（新增 `test_client_concurrency_uses_slots_for_llamacpp`、
`test_fingerprint_separates_engine_and_its_concurrency_field`）。

## 35. 2026-11-10：按《抽样与重标注规范 v0.2》对齐标注协议（§5 / §6 / §7）

规范原文由主创给出（`image_dataset_sampling_annotation_v0_2.md`，共 8 节）。
本轮只做**标注协议**部分，抽样配额（§1–§3）另做。

### 35.1 参考 tag 按类别分组（§7.1）

`ref_tags` 以前是空格平铺进 `<REFERENCE>`。平铺时 `hatsune_miku` 与
`long_hair` 在模型眼里是同一类东西，实测会写成 "hatsune miku hair" 这种外貌词。

- 新增 `tagger/tag_categories.py`：读 `models/*/selected_tags.csv` 的 category
  列建映射（`TAGGER_TAG_VOCAB` 可覆盖路径），按
  `character → copyright → artist → general → meta → unclassified` 渲染成
  `character_tags: ...` 这类行（字段名带 `_tags`，与规范 §7.1 一致）。
- `client._ref_block` 改成始终分组；**词表里查不到的 tag 归 `unclassified_tags`**，
  不再冒充 general——规范写明"不可把未分类的任意冒充 artist"。
- `rating`（`general`/`sensitive`/`explicit`/`questionable` 这四个词本身在词表里
  是 category=9）直接不注入：它是审核等级，不是画面事实。
- `PROMPT_VERSION` 升 **v5**。

★**词表覆盖是残缺的**：`models/wd-eva02/selected_tags.csv` 16473 行里
general 11601、character 4868、rating 4，**copyright / artist / meta 是 0 条**。
所以 `vocaloid`、`sakimichan` 这种真 tag 现在落 `unclassified_tags`。
要补全三类只有两条路：本地 `work/danbooru_artists.parquet`（946 万条）能补
artist 一类；全五类得从远端导出全量 danbooru tag 表。

### 35.2 图中文字结构化 `visible_text`（§6 + §8）

- prompt 在 `<TAGS>` 之后、`<CAPTION>` 之前加 `<VISIBLE_TEXT>` 块，要求输出
  `[{"text_exact","language","script","legibility","bbox?"}]`；没有文字输出 `[]`。
  ★放在 caption**之前**是刻意的：自回归先写下原文，后面 caption 引号里才引得住。
- 解析：`_TAG_BLOCK` / `_CAPTION_BLOCK` 的对齐 lookahead 都补了
  `(?=<VISIBLE_TEXT>)`，否则新块正文会流进 tag 段被洗成假 tag。
  `parse_output_full()` 返回三元组，`parse_output()` 保留二元组给老调用方。
- 解析是**降级**的：JSON 被 max_tokens 切断时，先按 `"text_exact": "..."` 逐条
  捞回，捞不到才按行；宁可少 language/script 字段，也不丢已经抄对的原文。
- `OutputSpec.visible_text: bool = True`（`tiers.yaml` output 段同名开关），
  **进缓存指纹**；`max_tokens` 的 `need_tokens` 预留 160 token。
- ★**质检口径**：`nl_tokens` 是整段回复的 usage，而 `nl_max_tokens` 是给
  caption 定的。不扣掉这一块，每张带字的图都会被判 "NL 过长" 反复重试。
  `_estimate_visible_tokens()` 按"每条 20 token 结构开销 + 中日韩一字一 token
  + 其余四字符一 token"扣减后再进 `QC`。
- 产物：nl payload 顶层 `visible_text` **必写**（空就是 `[]`——"这张没字"与
  "这版没跑"不能混成同一个缺字段）；`pack.py` parquet 与 `merge.build_rows`
  都加同名列，**存 JSON 字符串**而不是 `list<struct>`：每条的键集不同
  （bbox 可选），pyarrow 会按第一行建 schema，第二行多一个键就写不进去。

### 35.3 语言配比（§5）

- 抽签键从 `hash(seed:image_id)` 改为 `hash(source:image_id:seed)`，
  `source` 取 `TaskRow.shard`。理由：某个来源的 id 成段时（Pixiv 的 pid、
  内部连号），只按 image_id 抽会让那一整段集体中签——全局 25%、拆到目录
  变成"这个 90% 那个 0%"，分层配比作废。
- `tiers.yaml` 的 `caption_zh_ratio` 由 0.3 改 **0.25**（规范 §5 的 75/25）。

### 35.4 校验

`pytest tests -q` → 全绿（含新增 `tests/test_tag_categories.py` 11 条、
`tests/test_visible_text.py` 12 条）；`read_lints` 0 诊断。

### 35.5 还没做的（按规范顺序）

1. **§1–§3 抽样配额**：MONET 3000 万按 6 个来源定额（laion 1030 万 /
   coyo 660 万 / …）、Fine-T2I 100 万按五个目录定额；需要"候选 manifest →
   分层选样 → quota_original/quota_final 记账"的独立模块，仓库里目前
   **零配额代码**（只有 `--limit` 与 `skip=` 截断）。
2. **§8 字段名**：最终表现在叫 `caption` / `tag`，规范要求
   `nl_final` / `tags_final`。改名会断既有下游，需要定"改名还是加别名"。
3. **§7.5 原 tag 顺序与类别**：产物只留 `tags_original`（名字列表），
   类别/顺序没落盘。
4. **§4 旧字段退出**：属流程与 manifest 过滤，当前管线没有"最终表排除旧
   Caption"这一步。

## 36. 2026-11-10：抽样配额子系统 + 最终表双字段名

### 36.1 最终表同时认两套字段名（§8）

规范用 `nl_final` / `tags_final`，现有下游读 `caption` / `tag`。做法是**并列**，
不是改名：

- `merge.FINAL_NAMES = legacy | v02 | both`，`apply_final_names()` 在写盘那一步
  加别名，`final_field()` 是读侧的统一取值口（两套列名都认）。
- 默认 `both`：一份表两种读法，谁都不用改代码。代价是 caption 复制一份
  （3000 万条约十几 GB）。要省体积就 `tagger merge --final-names v02` 重出一份
  ——merge 不碰模型，重出是分钟级。
- 配置 `merge.final_names`，命令行 `--final-names` 覆盖。
- ★`build_rows()` 保持旧键名不动：别名只在写盘那一步加，否则"内层一套、
  外层一套"迟早对不上。

### 36.2 抽样配额（§1–§3）：`tagger/sample.py` + `tagger sample`

仓库此前**零配额代码**（只有 `--limit` / `skip=` 截断）。新子系统只管
"从候选 manifest 选 id"，不下载、不解码、不打标——规范要求的中间产物正是
"每个来源一份候选 manifest"。

三层纯函数：

| 函数 | 职责 | 关键口径 |
|---|---|---|
| `resolve_quotas` | 来源维度定 `quota_original` / `quota_final` | 可用不足削顶；缺额按**余量比例**跨来源补；补进来的量单记 `borrowed`，`quota_original` 永不被改写 |
| `dedupe_clusters` | 同簇只留一条 | 跨来源全局比 `(score, id)`，留最高分（§2） |
| `pick` | 60% 质量 / 25% 长尾 / 15% 随机 | 质量段按分数排序取头部（不是加权随机，理由见函数注释）；长尾按 stratum 频次**升序轮转**；随机段用 `hash(seed:source:id)` 排序 |

- ★**去重必须在选样之前**：先选后去重会把某来源配额悄悄打掉一截，报告里
  `selected` 却还是原数，账对不上。
- ★选中键用 `(source, id)` 而不是 `id`：跨库同图会撞 id，用 id 当键后到的
  会覆盖先到的，选中数凭空少一截。
- 配置 `configs/tiers.yaml` 的 `sample:` 段带两组 profile（`monet` 3000 万
  按 6 个来源、`fine-t2i` 100 万按 5 个目录，数字逐项来自规范 §2/§3），
  profile 之间不共用 `quota_total`（两批是独立交付物，合计算谁都不对）。
  `columns` 做深合并，profile 只覆盖 score/cluster 两列。
- 用法：`tagger sample --profile monet --input monet/laion=work/cand/laion.parquet
  --input monet/coyo=work/cand/coyo.parquet --out work/sampled/monet.jsonl`。
  输出行剥掉内部键（`_` 开头），加 `sample_source` / `sample_quota` 两列记账。

### 36.3 校验

`pytest tests -q` 全绿。新增 `tests/test_sample.py` 22 条、
`tests/test_final_names.py` 7 条。CLI 冒烟（两份候选、配额 20+40、其中一组
只有 30 条）实测：缺额补到有余量的那组、合计守恒、三类警告都出得来。

### 36.4 还没做

1. **§4 旧字段退出**：最终表还没有"排除旧 Caption"的过滤步骤。
2. **§7.5 原 tag 类别与顺序**：只落了 `tags_original`（名字列表）。
3. **分类词表补全**：`wd-eva02` 缺 copyright/artist/meta 三类（见 §35.1）。

### 36.5 ★口径修正（同日）：抽样只做"按配额挑够数"

主创看过之后指出：**规范文档是他与 ChatGPT 对话的产物，他本人只要求
"按表里的配额、从各来源挑够数"**；§2 那段"60% 质量加权 / 25% 长尾保底 /
15% 均匀随机 + 分层 + sscd 去重 + 最低质量过滤"是 ChatGPT 加的，文档自己
也写着"初始可测试策略（需抽样审计后定稿）"。

我上一版把这四条全设成了默认行为，等于不打招呼就替人定了样本分布。已改回：

| 项 | 改前 | 改后 |
|---|---|---|
| 挑法 | 60/25/15 分层 | **均匀随机**：`hash(seed, source, id)` 排序取前 N |
| `dedupe` | 默认 true | **默认 false**，`--dedupe` 显式开 |
| `mix` | 默认按 60/25/15 | **默认 false**，`--mix` 显式开 |
| `min_score` | 0（本就是关） | 仍 0；`needs_score=False` 时**连分数列都不读** |

连带的好处：默认只读 `id` / `source` 两列，数据集自己的 parquet 直接喂给
`--input` 即可，不需要先导出一份候选清单（§36.4 的旧待办因此作废）。

代码层面对应：`SampleSpec.mix` 由 tuple 改成 **bool**（比例移到 `mix_ratio`），
`needs_score` 属性决定要不要读分数列，`read_candidates` 按开关决定拉哪几列。
`pick()` 的默认分支就是三行——排序、切片。

★教训：规范文档是讨论稿，其中的"策略"级条目要先分清**是需求还是建议**。
"初始可测试策略，需审计后定稿"这种措辞就是明确的"别当默认值用"。

## 37. 2026-11-10：分发包派工接入（fox-label v3）

服务端由另一位同事开发，v3 源码交付在 `server/fox-label-v3-src-20261110/`
（`server.py` / `dispatch.py` / `worker.py` + 314 项测试）。我先前按自拟契约写的
`tagger/dispatch/` 骨架（含 27 条测试）**已全部删除**——看到真源码后确认契约对不上，
留着就是第二套错实现。

**结论：协调层用人家的 `worker.py`，我们只补一个进度出口。**

| 项 | 真实 v3 |
|---|---|
| 端点 | `POST /api/v1/{handshake,heartbeat,done,fail,release,status}` |
| 身份 | `user_id` 人填 + handshake **一次性签发**的 `secret` + 自动生成的 `device_id` |
| 派工 | handshake 一次给 active + prelock；heartbeat 每次顺带补派 |
| TTL | beat 120s / active 360s / prelock **240s** / 进度 6h 告警、24h 强收 / 租约寿命上限 168h |
| 下载 | 服务端只给 `repo`+`package`+`size`+`sha256`，地址按 `HF_ENDPOINT` 拼，`curl -C -` 续传 |
| 完成 | `/done` 只收 `stats` 字符串，服务端截断 200 字写进 `packages.note`（要结构化结果得服务端加字段） |

我们的接线（`worker.py` 用 `--cmd` 起子进程）：

```bash
python worker.py run --cmd "python -m tagger.cli client --shard \"{shard}\" --out-dir \"{out}\" --url http://127.0.0.1:8000 --mode both"
```

- `--cmd` 是 `str.format` + `shell=True`，**路径里有空格就会断**，占位符务必自己加引号；
- 心跳要的进度由 `client --progress-file <out>/.fox-progress` 提供，写
  `{"done","total","phase"}`；节流 1s、收尾那条必写。`total` 是"数据源已吐出的条目数"
  （tar/parquet 流式读，跑前数不出来），服务端只用 `done` 算速率与停滞。
- 不传 `--progress-file` 时行为与以前完全一致（CLI 老路径零改动）。

**已复现的服务端缺陷（未修，已反馈）**：全新 data dir 下 `status` 与 `serve --server-id`
直接崩 `no such table`。根因：`Store.__init__` 只在 `db.sqlite3` 已存在时才 `init_schema()`
（server.py:229-230），而 `cmd_serve` 写 `server_id` 那句（1455-1458）在 `init_schema()`
（1462）之前。按 README §9 的 v3 快速开始（`serve --server-id` 打头）照抄就能撞上。

**校验**：`tests/test_cli.py` 13 条通过（新增 2 条：`--progress-file` 可选开关、
进度文件键名与节流/收尾）。全量 pytest 见本轮结果。


## 38. 2026-11-12：pixai 补批处理 + tf32（默认后端的性能缺口）

**起因**：默认 tag 后端是 pixai（`configs/tiers.yaml` 的 `tagging.backend`），
但它跑的是三个后端里**最差的一条路径**——wd-eva02 身上的每一层优化它都没有：

| 优化 | wd-eva02 | pixai（改前） |
|---|---|---|
| `_predict_many` 攒批 | 有 | **无** → 落到基类逐张循环 |
| tf32 开关 | 有（`_load` 里调） | **无** → `allow_tf32` 保持 PyTorch 默认 False |
| 解码预取 / CUDA Graph | 有 | 无 |

第二行是硬伤：PyTorch 1.12 起 `cuda.matmul.allow_tf32` 默认 False，只有
`WDEva02Tagger._load` 会打开它。选 pixai 时 tensor core 全程闲置。
再叠加分辨率——pixai 是 1008²/patch14 = 5184 token，eva02 是 448² = 1024 token，
注意力按平方算是 25 倍——"pixai 稍慢"这个印象其实被低估了。

**改动**（`tagger/cpu_tagger.py`）：

1. `_apply_precision_flags()` 从 `WDEva02Tagger` **上移到 `TagBackend`**：
   它只碰 torch 全局量，与"哪个后端"无关。`device` 只有子类存，所以用
   `getattr(self, "device", "cpu")` 取；CPU 上恒为 False。
   ★docstring 里记了一条隐患：这是**进程级**开关，将来若在一张卡上同时挂
   两个不同精度的后端，后加载的那份说了算。
2. `PixAITagger._load` 在建好 pipeline 之后调它（device 此时才确定）。
3. `PixAITagger._predict_many`：一次喂多张。**必须把 `batch_size=` 一起传给
   pipeline**——它的默认值是 1，即使喂 list 也会在内部逐张循环。少了这个参数，
   代码看起来像批处理而吞吐一点不变，是"改了没效果"里最难查的一类。
4. bf16 对 pixai **降级为 tf32**（`_bf16_downgraded` 在 `__init__` 里判定，
   所以进指纹、能被单测断言）。理由：bf16 的本义是权重也转半精度，而 pixai
   走 trust_remote_code 的自定义管线，权重转换没实测过；静默转半精度可能让
   远端代码某层 dtype 不匹配，错误埋在 pipeline 内部、表现成"这张图 tag 少了"。
   降级后权重仍 fp32（零漂移），只是打开了 tensor core。
5. 批内一张坏图**不再带走整批**：批量调用抛异常时退回逐张重跑（单张仍抛）。
   `tag_batch` 可到 16，一次异常原本会让执行器把 16 个 future 全判失败。
6. 顺手拆出 `_threshold_arg()` / `_flatten()`，单张与批量共用同一套口径。

**口径没变的部分**：阈值语义、keep_categories 过滤、`max_tags` 截断、结果
排序与四舍五入都在基类 `predict_batch` 里，批/单张两条路产出一字不差
（`test_pixai_batch_matches_single_image_path` 守着）。

**校验**：`pytest tests -q` → **全绿（exit 0）**。`tests/test_cpu_tagger.py`
新增 11 条（该文件 41 → 52）：批只发一次调用且带 `batch_size`、批量与单张同结果、坏图回退、
单张不回退、阈值三种形态、keep 过滤、decode/forward 拆分、tf32 只在 GPU 开、
fp32 恒关、bf16 降级、工厂透传 batch。`read_lints` 0 诊断。

**还没做（下一步）**：多卡共卡。现状 `client.py` 的 `service_urls` 虽已支持
多卡 NL，但 `tag_backend` 是**同一个对象**发给所有 client（`client.py:3155`），
后端内部有全局锁——tag 完全不随卡数扩展；且 `resolve_device("auto")` 恒回
`cuda:0`。要动的是「每个 client 各建一份后端、绑自己的卡」，不是调度器。


## 39. 2026-11-12：tag 多卡（每卡一份权重，按批等分）+ WebUI 设备多选

**目标形态**：不是"多卡推同一张图"（张量并行），而是**数据并行**——每张卡各
加载一份 tag 权重，各自负责自己的图，滚动往前推。

**为什么不做"卡数动态调度"**：之前讨论过"偶数卡 NL/tag 各半、先跑完的去帮忙"，
按实测成本比（VLM 1~3 s/张 vs tag 17~40 ms/张，1:50~1:150）算下来，50/50 的
起点会让 tag 那张卡在整批 97% 的时间空转；而"改派一张卡去跑 NL"要重新加载
8B 权重（30~60 s + 5~7 G 显存）。共卡（tag 和 NL 挤同一张卡，只吃 1~2% 算力）
不用做这件事就拿到同样结果，所以先做共卡。

**后端**（`tagger/cpu_tagger.py`）：

1. `resolve_devices(pref)` → 设备清单。接受 `None/"auto"`、`"cpu"`、
   `"cuda:0,cuda:1"`、`["cuda:0","cuda:1"]`，去重保序。
   ★`auto` **不铺满所有卡**：多一张卡就多一份权重（显存 + 启动时间），
   而"机器有几张卡"和"我想拿几张跑 tag"是两件事，铺满必须显式点名。
   ★显式写了不存在的 `cuda:N` 时在这里就报错（"这台机器只有 2 张卡"），
   而不是让它一路走到 `model.to()` 得到 "Invalid device ordinal"。
2. `MultiDeviceTagBackend`：**不是** `TagBackend` 的子类，不加载权重、不碰
   torch，只做切分/派活/对位。name/model_id/precision/preprocess/thresholds/
   keep/max_tags 全部转发第一份——这几项在多卡之间**必须一致**，否则同一张图
   会随"落在哪张卡"而变，属于产物层面无法归因的污染。
   - 用**线程**不用进程：模型对象不可 pickle（transformers pipeline），
     Windows 只能 spawn（每卡多付一次 import torch + 读权重）；torch 前向在
     C++ 侧执行时释放 GIL，线程之间不互相抢占。
   - **连续等分**而不是轮转：同一张卡的图在内存里连续，wd-eva02 的解码预取
     才能跨块生效；拼回来"按段序接上"就是原顺序（调用方靠位置对回 image_id）。
   - 空段不派活：图比卡少时不会给后面的卡发一次 0 张的调用。
   - `batch_size` 报的是**整批**容量（每卡 × 卡数）：`local_tag._chunk_size`
     拿它算一次取多少图，报单卡值会让每张卡只分到 1/N，把攒批收益折掉。
3. `build_tag_backends(backend, devices=...)`：单卡（或无 devices）直接返回
   `build_backend` 的原实例——**多卡是多出来的一层，不是新口径**。

**接线**：

- `client.run_client`：`resolve_devices` → `build_tag_backends`；退出时补一句
  `tag_backend.close()`（包装器自带线程池，`client.close()` 关不到它，漏了
  会在退出时挂着 N 个空转线程）。
- `web/jobs.py`：`_build_backend` 与 `_run_vlm` 都走 `spec.tag_device_list()`，
  兼容规则只写一处（旧快照只有 `device`）。VLM 引擎把清单拼成逗号串传给
  `--tag-device`，CLI 只有一条解析路径。
- `web/app.py`：`/api/defaults` 新增 `devices`，按**本机实际卡数**生成
  （`auto` + `cuda:0..N-1` + `cpu`）。写死 cuda:0/cuda:1 的结果是 3 卡机器选不到
  第三张，用户会以为"程序不支持多卡"。
- 前端：`ChoiceMulti`（新控件，aria-pressed 的勾选清单，不是 radio；`auto`/`cpu`
  与具体卡互斥，且不允许清空到零）。设备从单选变多选，批大小的 help 在多卡下
  改成"每张卡各 N 张，一次共 N×卡 张"。

**注意（部署侧）**：共卡要求同一张 GPU 上能同时放下 NL 服务与 tag 权重。
llama.cpp 按需分配，没问题；**vLLM 默认 `gpu_memory_utilization=0.9`**，
24 G 卡只剩 2.4 G，pixai fp32（486M ≈ 2 G）+ 1008² 的激活可能不够——真在
vLLM 上开多卡前，先把该值降到 ~0.85。

**校验**：`pytest tests -q` → **全绿（exit 0）**。新增 `tests/test_tag_multi_device.py`
16 条（设备解析 6、切分 1、分发 5、工厂 3、JobSpec 兼容 1）；`tsc --noEmit` 0 错误，
`npm run build` 通过。web 侧 4 个测试文件的 monkeypatch 目标同步改为
`build_tag_backends` / `resolve_devices`（原来打的是已删掉的 `build_backend` /
`resolve_device`）。

**仍未做**：真实数字。仓库至今没有 pixai-on-GPU 的 ms/张（只有 CPU 的 16 s），
多卡到底能快多少、共卡对 NL 的干扰有多大，都要实测；测出来再决定要不要
把 tag 挪到专门的卡上（那时才轮到调度器）。


## 40. 2026-11-12：包级并行（每张卡各吃一个封包）+ WebUI 切换

**两种多卡分工**（`JobSpec.tag_mode`，默认 `batch`，与加开关之前完全一致）：

| | 含义 | 适合 |
|---|---|---|
| `batch` | **一个**包在跑，每凑一批等分给各卡（`MultiDeviceTagBackend`） | 包少图多 |
| `pack` | **N 个**包同时跑，一个包完整归一张卡 | 包多；日志能按卡定位 |

批内切分在后端层（只回答"一批怎么算"）；包级并行是调度（回答"谁先拿下一个
包"），跨 NL / tag 两条线，所以单独一层：`tagger/pack_parallel.py`。

**lane 之间不共享任何可变状态**：各自一个后端、一个 WorkIndex、一个 sink、
一份 stats。协调全靠盘上已有的跨进程机制——WorkIndex 每实例独立 sqlite 连接
+ 文件锁、`failures.jsonl` 的文件锁、jsonl 按"包名 + 进程号"分片。这套东西
本来就在支撑"几十个分包跑同一个产物目录"，所以**没有新造协调协议**，也就
不存在"漏图 / 重复落盘"这类新失败模式。唯一共享的是 `MemberReader`——它本来
就带锁（打标线程与 HTTP 缩略图线程并发读同一个包），直接复用。

**调度层**（`pack_parallel.py`）：
- `group_by_pack`：zip/tar 成员按 `source` 归包；散图按父目录归堆（散图目录
  因此会退化成 1 个包，由调用方判断要不要退回批内切分）。
- `split_by_weight`：**LPT**（重的先放、每次给最轻的 lane）。不用轮转——包体积
  差 10 倍很常见，轮转会让某条 lane 收尾时还剩一个大包，其余全空转，墙钟由
  最慢那条决定，等于把 N 卡退回 1 卡。包比卡少时后面的 lane 拿到空列表。
- `run_lanes`：线程池 + `halt` 事件。任一条抛异常（含 `Stopped`）就置 halt，
  其余在**下一个包边界**停手（Python 没有安全的杀线程手段，且这与 guard /
  停止按钮的既有语义一致），最后把第一条异常原样上抛——`Stopped` 被吞掉的话，
  点停止会显示成"任务失败"。
- `merge_tag_stats` / `merge_client_stats`：**绝对求和**而不是累加（刷新进度会
  多次调用，累加会让完成数越跑越多）；`wall_ms` 取 max（并行下加起来不是墙钟）。

**接线**：
- 本地引擎：`_build_backend_lanes` 每卡一份**不包装**的后端（套上
  MultiDeviceTagBackend 会让每条 lane 又去抢所有卡）；线程内各自 `tag_items`
  （它自己按 out_dir 开 WorkIndex + sink）。`job.stats` 在 `finally` 里合并，
  中止时界面也不会显示"0 完成而盘上已有产物"。
- VLM 引擎：每条 lane 一份 `copy.copy(args)`，只改 `shard`（分包）、`url`
  （只留主地址、清空 `urls`）、`tag_device`。其余（ops/table/guard/out_dir）
  共用——产物目录必须同一个，否则封包会漏掉别的 lane 写的部分。
  `run_client` 内部自建 ClientStats，只能从 hook 拿引用，跑完再合并。
- 适用范围：**本地分片输入 + 本地推理服务**。HF 仓库分片是流式拉的（一包几个
  GB，按包分给多卡只会让下载和算力互相等），外部 API 也没有"每卡一个端点"这
  回事——这两种情况自动回落 batch 并在日志里说明。
- 兜底：包数 < 2、卡数 < 2 都自动回落 batch，同样写一行日志。

**WebUI**：Tag 面板新增「多卡分工」（批内切分 / 每卡一个包），选卡数 < 2 时置灰。
`/api/defaults` 新增 `tag_mode`（读 `tagging.mode`）。旧方案/旧快照没有该字段
一律回落 batch。

**校验**：`pytest tests -q` → **全绿（exit 0）**；新增 `tests/test_pack_parallel.py`
12 条（分组 2、LPT 2、并发中止 2、统计合并 3、后端清单 2、JobSpec 1）。
`tsc --noEmit` 0 错误，`npm run build` 通过。

**注意**：包级并行的收益取决于"包数 ≥ 卡数"和"NL 服务每卡一个"。若只有 1 个
`--url`，N 条 lane 会挤在同一个服务上，NL 那侧仍是串行——这时 batch 反而更好。

## 41. 2026-11-12：用户报的两个 serve bug（槽位参数无效 / 选错卡）

来自真实工单（一台 smi 顺序为 [GTX1050, V100, V100]、用 CUDA_VISIBLE_DEVICES=2
选卡的机器）。两条都是真 bug，且都属于"看起来生效、其实没生效"那一类。

### 41.1 --max-num-seqs 对 llama.cpp 完全无效

链路：_load_profile → esolve_profile(max_num_seqs_override=) → 只落到
profile.max_num_seqs（vLLM 命令行与指纹用）→ 而 
l_engine.build_command 的
-np 只认 slots_override，cmd_serve 又没传它。于是用户传 12，日志里仍是
按显存推的 2。

修法（不是"再透传一个参数"，而是让两个引擎共用一个用户输入）：

- TierSpec / RunProfile 加 slots 字段；esolve_profile 的取值顺序是
  **分档表 slots > 用户覆盖 > 分档 max_num_seqs**，即 --max-num-seqs 12
  在 llama.cpp 上也得到 12。分档表写 slots: 则优先（那是给 llama.cpp 单调的）。
- 
l_engine.slot_plan 取值顺序：override（多卡每卡并发）> profile.slots
  > SLOT_TIERS 显存推算。
- cmd_serve 透传 --slots（新参数，优先于 --max-num-seqs），并说明优先级。
- 启动日志新增一行 [tagger] 槽位: N（-np）  卡: ...：以前只有命令行里那串
  -np N 能看出来，用户找不到自己传的数就以为没生效。

### 41.2 primary_gpu() 不看 CUDA_VISIBLE_DEVICES

query_gpus() 列的是 nvidia-smi 的**物理序**，CUDA 认的是环境变量重映射后的
**设备序**。两者不一致时（很常见），分档、槽位、精度全都按错的那张卡算：
4GB 卡 → (0.0, 2) 档 → 2 个槽位，而模型实际跑在 16GB 上，能力只用掉 1/6。

- 新增 hardware.visible_gpus()：按 CUDA_VISIBLE_DEVICES 过滤并**保持列表顺序**
  （2,1 时第一张是 2 号）；ll/空 = 全部；UUID 与拼错的号跳过；全对不上才退回
  全部（宁可给个能跑的档，也不报"没有显卡"）。primary_gpu() 改为基于它。
- 所有 query_gpus()[0] 的判断点改走 isible_gpus()：cli._load_profile、
  cmd_guard、llm_service.plan、llm_service.start、_probe_capability。
  多卡面板（multi_gpu）**仍然**用 query_gpus：它要列出物理卡让人挑。

**口径**：CUDA_VISIBLE_DEVICES 里写的是 **nvidia-smi 的 index**（这也是用户
踩坑的地方——他以为要写 torch 的序号）。子进程隔离沿用原做法：start_server
把自己看到的这张卡的物理 index 写进子进程环境，子进程眼里它变成 0。

**校验**：pytest tests/test_serve_slots_gpu.py 11 条（槽位 4、选卡 7）；
真实分档表上 --max-num-seqs 12 → slots=12 → -np 12。
