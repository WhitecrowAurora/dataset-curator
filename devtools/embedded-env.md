# 嵌入式运行环境（env/python）

和训练器同一套做法：官方 embeddable Python 解压进仓库，依赖装在它自己的
`Lib\site-packages` 里。不碰系统 Python、不写注册表、不需要管理员权限；
整个 `env\python` 压缩带走、解压即用，所有人跑的是同一份解释器和同一批版本。

---

## 1. 目录

```
install.bat                 ← Windows 一键安装（薄壳，逻辑在 ps1 里）
install.sh                  ← Linux/macOS/WSL 一键安装（建 venv）
tagger.cmd                  ← Windows 便携入口：优先 env\python，没有才退回系统 python
tagger.sh                   ← Linux 便携入口：优先 env/linux，其次系统 python3
requirements-embedded.txt   ← 小依赖全量锁版本

env/
  initialization/                   ← 引导材料（12.3 MB，随仓库走）
    python-3.12.9-embed-amd64.zip   ← 官方 embeddable 包（只 Windows 用得上）
    pip-25.2-py3-none-any.whl       ← pip 引导轮子（纯 Python，跨版本可用）
  python/                           ← Windows 嵌入式环境（151.6 MB）
    python.exe
    python312._pth                  ← 已启用 Lib\site-packages 与 import site
    Lib\site-packages\
      _tagger_repo.pth              ← 指向仓库根，所以任何目录都能 import tagger
  linux/                            ← Linux venv（由 install.sh 创建，与上面并存）
    bin/python
    lib/python3.x/site-packages/_tagger_repo.pth

devtools/setup_embedded_env.ps1     ← Windows 安装的实际实现
```

Windows 与 Linux 各用一棵树，**互不覆盖**：把仓库从 Windows 拷到 Linux 时，
`env/python` 会被 `install.sh` 自动忽略（它检测到 `python.exe` 就只提醒、不动它）。

## 2. 安装与使用

**Windows：**

```bat
install.bat                                  :: 幂等，已装过就只补缺的
install.bat -Force                           :: 换 Python 版本 / 环境坏了
install.bat -WheelDir env\initialization\wheels   :: 离线安装
tagger.cmd --help
env\python\python.exe -m pytest
```

**Linux / macOS / WSL：**

```bash
chmod +x install.sh tagger.sh     # 从 Windows 拷过来会丢执行位
./install.sh                      # 幂等
./install.sh --force              # 重建 env/linux
./install.sh --offline            # 用 env/initialization/wheels
./install.sh --python python3.12  # 指定解释器
./tagger.sh --help
env/linux/bin/python -m pytest
```

为什么 Linux 不用 embeddable 包：Python 官方只提供 Windows 的 embeddable ZIP，
Linux 没有对应物。可移植的替代是 python-build-standalone，但要多下一份 30MB 且
和 glibc 版本绑定，收益不大。所以 Linux 走 venv——依赖同样关在仓库里，
只是解释器本身借用系统的（`install.sh` 会检查 >= 3.10）。

离线机器：先在能上网的机器上下 wheel，再带过去。

```bash
python -m pip download -r requirements-embedded.txt -d env/initialization/wheels

# 离线也要跑打标的话，tag 运行时得一起下（约 2.5 GB）。torch 不在 PyPI：
python -m pip download torch torchvision -d env/initialization/wheels --index-url https://download.pytorch.org/whl/cu126
python -m pip download transformers timm safetensors -d env/initialization/wheels

./install.sh --offline          # Linux
install.bat -WheelDir env\initialization\wheels   # Windows
```

离线 wheel 目录里没有 `torch-*.whl` 时，两个脚本都会**跳过**打标运行时并给一条
警告——装不上可选依赖不该让打包和 NL 也跟着不可用。

## 3. 里面装了什么

只装跑流水线必需的小包（`requirements-embedded.txt` 全量锁版本）：

| 包 | 用途 |
|---|---|
| `httpx` | 客户端请求 vLLM 服务 |
| `PyYAML` | 读 `configs/tiers.yaml` |
| `Pillow` | 图片解码、转码、alpha 铺白底 |
| `pyarrow` | parquet 分片（打包默认格式）的读写 |
| `pytest` | 测试套件 |

**另一份清单 `requirements-tag.txt`（打标运行时，默认安装）：**

| 包 | 什么时候需要 | 体积 |
|---|---|---|
| `torch` + `torchvision` | 所有 torch 后端（pixai / wd-eva02） | ~2.5 GB |
| `transformers` `timm` `safetensors` | 同上 | ~200 MB |
| `onnxruntime` | cl-tagger-v2 后端（**不需要 torch**） | ~15 MB，连带 protobuf/sympy 约 50 MB |
| `datasets` | HuggingFace 数据集输入源 | ~50 MB |
| `open_clip_torch` | 图文一致度打分（缺了只少一个分项） | ~10 MB |

这些**默认由 `install.bat` / `install.sh` 装好**：默认 tag 后端是 pixai，
没有它们第一次跑打标就会失败。只想打包或跑 NL 时用 `-SkipTag` /
`--skip-tag` 跳过（省十分钟）。torch 的 wheel 不在 PyPI，所以脚本把它单独
提到 PyTorch 索引去装：有 `nvidia-smi` 用 `cu126`（本机实测组合），没有用
`cpu`；换 CUDA 版本用 `-TorchIndex https://download.pytorch.org/whl/cu130`。

**索引目录和清单版本必须对得上**：PyTorch 是按 CUDA 版本分目录的，每个目录
只放自己那份——`cu124` 最高只到 2.6.0、`cu128` 也没有 2.14.1，拿它们去装
钉死的 `torch==2.14.1` 会直接 `No matching distribution found`。网页上的
「安装 / 重装」用的是同一口径（`deps.py: TORCH_INDEX_GPU`），
`tests/test_deps.py` 有一条断言把它和清单里记的 `+cu126` 绑在一起。

**仍然按需装的**：

| 包 | 什么时候需要 | 体积 |
|---|---|---|
| `llmcompressor` | 4bit 量化（`quantize_worker.py`），对 torch 版本卡得死 | 几百 MB + 编译依赖 |
| `vllm` | NL 生成服务（仅 Linux / WSL2），会强制换掉钉好的 torch | ~1 GB + CUDA |

装大包时**装进同一个环境**，不要另开 venv：

```powershell
# 手动重装 tag 运行时（install.bat 里那一步的等价命令）
env\python\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126
env\python\python.exe -m pip install -r requirements-tag.txt

# 只有 CPU 也能跑 tag（慢）
env\python\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
```

装完确认没把别人的环境搞坏：

```powershell
env\python\python.exe -c "import torch; print(torch.__version__, torch.cuda.is_available())"
env\python\python.exe -m pytest
```

## 4. 已验证的事实

- Python `3.12.9`，Windows x64，MSC v.1942；
- `env\python\python.exe -m pytest` → **102 passed**（全套测试在这套环境里通过）；
- `.\tagger.cmd --help` → 正常列出 `doctor / serve / smoke / guard / discover / manifest /
  upload / buckets / artists / split / client / merge / verify / report / abtest`；
- `pyarrow 26.0.0` 在**没有 numpy** 的情况下可正常写/读 parquet
  （pyarrow 16+ 已把 numpy 变成可选），所以这套环境不需要额外装 numpy。

## 5. 为什么不用 venv

| | venv | 嵌入式（本方案） |
|---|---|---|
| 依赖 | 本机必须先有同版本 Python | 自带一整套，不依赖本机 |
| 移植 | `pyvenv.cfg` 里写死绝对路径，换机器要重建 | 整目录拷走即可，只需重写一个 `.pth` |
| 版本漂移 | 每人 `pip install` 到不同版本 | 锁版本清单 + 现成环境 |
| 权限 | 通常无问题 | 无问题（不碰系统目录） |

代价：`env\python` 有 150 MB，不能当源码看待。建议打包分发（zip/tar）或做增量同步，
不要指望别人手工 `git clone` 后还能原样还原依赖。

## 6. flash-attention：要不要装？

**结论：对打标没有实际加成，不建议在打标机装。**

### 现在实际跑的是什么

| 环节 | 注意力在哪 | 谁决定实现 |
|---|---|---|
| NL 生成 | vLLM 进程内部 | vLLM 自选（Linux + NVIDIA 上一般是 FLASH_ATTN / FLASHINFER / TRITON_ATTN）。`model_loader.build_command()` 里没有 `--attention-backend`，我们没参与 |
| tag 反推 · pixai | `transformers.pipeline` | 代码里没有 `attn_implementation`，transformers ≥4.36 + torch ≥2.1 默认 `sdpa` |
| tag 反推 · wd-eva02 | timm `create_model` | timm 的 Attention 默认 `fused_attn`，走 `F.scaled_dot_product_attention`（也是 sdpa） |
| tag 反推 · cl-tagger-v2 | onnxruntime 计算图 | 与本机 torch 无关 |

所以「现在是不是 SDPA」——**是**，tag 侧两条路都等价于 SDPA。

### 为什么装 flash-attn 没用

1. **位置不对。** NL 侧最有价值的 attention 优化发生在 vLLM 里，而 vLLM 只跑
   Linux/WSL2；我们在 Windows 的 `env\python` 里装 `flash_attn`，那个进程根本
   不参与推理。
2. **SDPA 本来就会调 flash 内核。** 从 torch 2.0 起，`scaled_dot_product_attention`
   在 CUDA 上会按 dtype/头维自动选 flash、mem-efficient 或 math 三个后端之一——
   那个 flash 内核是**编译进 PyTorch 的**，不是 `flash_attn` 这个包。单独装
   `flash_attn` 换来的是 fp8、varlen、paged KV、自定义 mask 这些能力，我们一个都用不上。
3. **精度不兼容。** FA2/FA3 只吃 fp16/bf16；现在 tag 侧是 fp32（pixai 走
   pipeline 默认 dtype，wd-eva02 也没转）。要开 FA 就得换 bf16，tag 概率会整体
   偏移——0.35 阈值下的取舍全变，而"跨机同源"正是这套系统在死守的东西
   （fingerprint 里专门记了 `precision`）。
4. **不是瓶颈。** tag 侧是 batch=1 的 ViT 分类头，开销大头在图像解码、预处理、
   patch embed 与 MLP。同一份代码里 wd-eva02 比 pixai 快约 7 倍（2.3s vs 16s 每张），
   说明换后端比换 attention kernel 划算几个数量级。
5. **Windows 上装不上。** flash-attn 没有官方 Windows 轮子，要 CUDA toolkit + MSVC
   自己编，还得和 torch 版本严格对齐。

### 如果还是想提升 tag 速度，按性价比排序

1. **换后端**：pixai → wd-eva02（现成的，`--tag-backend wd-eva02`）；
2. **批处理**：现在是一张一张 forward，`cpu_tagger` 每次只喂 1 张图；
3. **bf16/半精度**：需要先做一次与 fp32 的对齐验证（同一批图，tag 集合差异 < x% 才放行），
   否则等于悄悄换了打分口径；
4. **onnxruntime GPU**：cl-tagger-v2 走 ONNX，图优化 + GPU EP，稳定且不改数值口径；
5. `torch.compile`：Windows 上支持有限，收益不确定，不建议现在碰。

## 7. 已知限制

- `_tagger_repo.pth` 里是**绝对路径**。别人把仓库放到别的盘/目录，必须重跑一次
  `setup_embedded_env.ps1`（脚本会按当前位置重写它）。
- `env\python` 里的包版本由 `requirements-embedded.txt` 钉死；装大包时 pip 可能会
  顺带升级 `certifi` / `typing_extensions` 之类的公共依赖。装完跑一遍
  `env\python\python.exe -m pip freeze | findstr /i "httpx pyarrow pytest"` 确认没被顶掉。
- 这个环境**不用于跑 vLLM**（Windows 原生不可用），服务在 Linux/WSL2 上，
  客户端只用 httpx 连过去。
