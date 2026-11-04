# 性能基线：解码 / 前向 / 并发首次实测

> 这份文档记录的是**测出来的数**，不是估的。所有数字来自
> `devtools/bench_tag_gpu.py` 与 `devtools/bench_concurrency.py`，
> 在同一台机器上可重跑。改任何一处性能相关代码前，先跑一遍再对比。

---

## 0. 环境

| 项 | 值 |
|---|---|
| GPU | RTX 4070 Ti SUPER 16 GB，cc 8.9（Ada） |
| 驱动 | 595.79 |
| torch | 2.14.1+cu126 / torchvision 0.29.1+cu126 |
| timm | 1.0.30 |
| 模型 | `eva02_large_patch14_448`，16473 分类头（无权重，与 wd-eva02 同架构同 FLOPs） |
| 输入 | 1024×1024 JPEG → 448×448 |
| 迭代 | warmup 5 / iters 20（`--quick` 为 2/5） |

⚠️ 权重未加载：速度只取决于结构与形状，与权重数值无关；但**精度**结论（fp32/bf16
差异）必须用真权重再验一次——这一步已在 2026-11-02 补做，见 §10。

---

## 1. 解码与预处理（每张）

| 路径 | ms/张 | 相对 PIL |
|---|---:|---:|
| PIL 解码 + 缩放（现状） | **14.67** | 1.00× |
| torchvision 解码（CPU） | **6.98** | **2.10×** |
| nvJPEG 解码（CUDA） | 8.20 | 1.79× |
| nvJPEG + 缩放/归一化 全 GPU | 25.56 | 0.57× |

**结论：nvJPEG 在单图场景下是负收益，不要做。**

原因不是 nvJPEG 慢，而是单张 1024² 的体量太小：每次调用都要走一遍
H2D 拷贝 + 上下文建立 + 同步，固定开销吃掉了并行收益。全 GPU 预处理
（26 ms）比现在（15 ms）更差，因为它把一个本来和 CPU 重叠的步骤变成了串行等待。

真要上 GPU 解码，只有一种情况站得住：**批量解码**（一次 8~16 张）。
那时固定开销被摊薄，才可能赢过 CPU。这需要 tag 侧先改成批处理——见第 2 节。

**零风险收益：把 PIL 换成 torchvision 的 CPU 解码，解码环节直接 2.1×。**
torchvision 本来就是 `[tag]` 可选依赖，不需要新装东西，也不改数值口径。

> ⚠️ **2026-11-02 更正：这条结论是错的**，2.1× 只量了解码本身。接进真实链路
> 要多一次"转回 PIL"的拷贝，端到端只剩 **1.04×**；而整条预处理留在张量上
> 虽然能到 1.65~2.20×，却换了一套重采样实现（tag IOU 0.987）。详见 §11.1。

---

## 2. 前向（每张 ms = 单次前向 / batch）

| 配置 | ms/张 | 相对 fp32 b1 |
|---|---:|---:|
| fp32（tf32 off，**当前默认**） batch=1 | 58.45 | 1.00× |
| fp32 + tf32 batch=1 | 44.07 | 1.33× |
| bf16 batch=1 | 31.30 | 1.87× |
| fp32 batch=4 | 48.44 | 1.21× |
| fp32 + tf32 batch=4 | 38.02 | 1.54× |
| **bf16 batch=4** | **15.58** | **3.75×** |
| bf16 batch=1 + CUDA Graph | 18.70 | 3.00× |
| bf16 batch=4 + CUDA Graph | 15.01 | 3.74× |

**结论：**
- **`torch.backends.cuda.matmul.allow_tf32` 默认是 False**（PyTorch 1.12 起），
  等于白白丢掉了 fp32 上 1.33× 的 tensor core 性能。改一行就能拿回来，
  代价是数值精度（TF32 尾数 10 bit）——必须做掉落验证再上。
- **bf16 是最大的一档**：单张 1.87×，配合 batch=4 到 3.75×。
- **batch 的收益只在低精度下才明显**：fp32 加 batch 只有 1.21×（GPU 已经被
  算力打满），bf16 加 batch 才到 3.75×（此时 GPU 算得快，批处理摊薄了固定开销）。

---

## 3. 发射次数与 CPU 瓶颈（这就是"launch 数高不高"的答案）

用 `torch.profiler` 数了一次前向的 kernel 发射：

| batch | 算子 dispatch | CUDA kernel 发射 | GPU 忙 | CPU 忙 |
|---:|---:|---:|---:|---:|
| 1 | 72 | **1600** | 70.2 ms | 47.2 ms |
| 4 | 72 | 1480 | 175.9 ms | 76.2 ms |

**关键发现：kernel 发射次数几乎与 batch 无关（1600 → 1480）。**

这意味着：
- batch=1 时，一张图要发 1600 次 kernel，每次固定成本约 3 µs → **约 4.8 ms
  纯粹是发射开销**，占总时长 8% 左右；
- 但真正致命的是 **CPU 侧 dispatch**：实测入队 31 ms / 总 33 ms（**98%**），
  说明 fp32→bf16 之后 GPU 已经等不到活了，**CPU 跟不上**；
- 这也解释了为什么 CUDA Graph 在 batch=1 上有效：33.15 → 18.70 ms（1.77×），
  它把 1600 次发射压成 1 次图回放；
- 而 batch=4 时 CPU 占比降到 53%，CUDA Graph 就只有 15.58 → 15.01（几乎无用）。

**一句话**：`launch 数高` 确实存在（1600 次/张），但它在 batch=1、低精度时
才变成瓶颈。**加 batch 和上 CUDA Graph 解决的是同一件事**，选一个就行；
两个都上收益不叠加。

> 注：GPU 忙的绝对值被 profiler 自身放大了（70 ms > 墙钟 33 ms），
> 只用于看比例与 kernel 数，不要当绝对时间用。

---

## 4. 客户端并发（假服务端 300 ms/张，32 张）

| 并发 | 墙钟 | 理论下界 | 每张 | 客户端开销/张 | 吞吐 |
|---:|---:|---:|---:|---:|---:|
| 1 | 11.57 s | 11.03 s | 361.4 ms | 16.6 ms | 2.8 张/s |
| 2 | 6.49 s | 6.23 s | 202.7 ms | 7.8 ms | 4.9 张/s |
| 4 | 3.98 s | 3.83 s | 124.4 ms | 4.6 ms | 8.0 张/s |
| 8 | 2.75 s | 2.63 s | 86.0 ms | **3.6 ms** | 11.6 张/s |

另外：**进程启动 + 配置加载 ≈ 1435 ms**（固定成本，与图片数无关）。

**结论：客户端开销 3.6 ms/张，是推理延迟（300 ms）的 1.2%。**
即使把 Python 全部换成 Rust，也只能省下这 3.6 ms 里面的几分之一——
**客户端不需要重写**。真正该做的是把并发开到 8 以上（现在默认 1），
这是 4.2× 的吞吐差。

（1435 ms 的启动成本在"一次跑几千张"的场景里可以忽略；但如果将来做
在线逐张调用，这 1.4 秒会变得很刺眼，那时再查它花在哪。）

---

## 5. pack 转码并行（60 张 1024²，q95）

| 线程 | 墙钟 | 每张 | 吞吐 | 加速比 |
|---:|---:|---:|---:|---:|
| 1（现状） | 0.88 s | 14.7 ms | 68.2 张/s | 1.00× |
| 2 | 0.62 s | 10.4 ms | 96.2 张/s | 1.41× |
| 4 | 0.50 s | 8.4 ms | 119.4 张/s | 1.75× |
| 8 | 0.45 s | 7.5 ms | 134.0 张/s | **1.96×** |

**结论：线程池只值 1.96×，不是核数倍的 8×。**

我之前凭"Pillow 会在 C 层释放 GIL"推断接近线性，**实测不成立**——Pillow 只在
解码/编码的像素循环里释放 GIL，`Image.open` / `convert` / `save` 的
Python 侧调度仍然串行，加上内存带宽限制，8 线程只有 2×。

所以 pack 要提速有三条路，按性价比：
1. **线程池（1.96×，半天工作量）** —— 先做，白捡；
2. **换成 torchvision 解码 + 线程池** —— 解码部分再快 2×，可能把总加速推到 3×；
3. **进程池或 Rust 扩展** —— 才能接近线性，但复杂度上一个台阶。

30M 张的账：14.7 ms/张 ≈ **122 小时（5 天）单线程**；
开 8 线程 2× → 2.5 天。这一条比"换语言"更值得先做。

---

## 6. 与我上一轮判断的差异（保留，便于回溯）

| 上一轮的说法 | 实测 | 结论变化 |
|---|---|---|
| nvJPEG 可能省 5~10% | 单图反而慢 44% | **不做**，改 torchvision CPU 解码 |
| pack 开并发接近 8× | 1.96× | 仍然要做，但别期待数量级 |
| Python 开销占 1~5% | 3.6 ms / 300 ms = **1.2%** | 与判断一致，重写不值得 |

---

## 7. 建议的下一步（按收益/成本比）

| # | 动作 | 预期 | 风险 | 工期 |
|---|---|---|---|---|
| 1 | 客户端默认并发 1 → 8 | **4.2×** 吞吐 | 无 | 1 行 |
| 2 | `pack` 解码换 torchvision | 解码 2.1× | 无 | 半天 |
| 3 | `pack` 开线程池 | 1.96× | 无 | 半天 |
| 4 | tag 侧 batch + bf16 | 前向 **2.36×**（CUDA Graph 3.80×），端到端 1.53× | bf16 有 0.07% 真改判 → 只做 opt-in。**已落地**，见 §10 |
| 5 | tf32 开关 | 1.12×（b1）/ 1.47×（b4） | 实测 0 改判 → **已设为默认**，见 §10 |
| 6 | CUDA Graph（batch=1 场景才有意义） | 1.77× | 形状固定才成立 | 2~3 天 |
| 7 | nvJPEG / DALI | 负收益 | — | **不做** |

第 4 项的验证方法（必须做，否则等于悄悄换了口径）：

```python
# 同一批 200 张图，fp32 与 bf16 各跑一遍，比较
#   · tag 集合的交并比（阈值 0.35 下的最终集合，不是原始概率）
#   · 概率的绝对漂移中位数
# 交并比 < 0.98 就别上 bf16——那说明阈值附近的选择变了。
```

---

## 8. 怎么复跑

```powershell
env\python\python.exe devtools\bench_tag_gpu.py                 # 全部
env\python\python.exe devtools\bench_tag_gpu.py --quick         # 冒烟
env\python\python.exe devtools\bench_tag_gpu.py --batches 1 2 4 8 --size 1008
env\python\python.exe devtools\bench_concurrency.py             # 并发 + pack
env\python\python.exe devtools\bench_concurrency.py --latency 1.2
```

跑之前确认环境里有 CUDA 版 torch（PyPI 的默认轮子是 CPU 版）：

```powershell
env\python\python.exe -c "import torch; print(torch.__version__, torch.cuda.is_available())"
# 如果打印 True 之外的值，重装：
env\python\python.exe -m pip install --force-reinstall --index-url https://download.pytorch.org/whl/cu126 torch torchvision
```

---

## 9. 本轮已落地（基于上面的测量）

### 9.1 分段计时：`--stats-json`

`tagger client` 现在按九个阶段计时：
`read / tag / tag_decode / tag_fwd / b64 / http / parse / gate / write`。

> `tag_decode` 与 `tag_fwd` 是 `tag` 的内部拆分（§12.2）。没有拆分能力的
> 后端（pixai / onnx）只报 `tag`，不会凭空给出解码占比。

```powershell
tagger.cmd client --shard work/dist/x.zip --url http://127.0.0.1:8000 `
    --out-dir work/out --artifact-format parquet --stats-json work/stats.json
```

```json
{"stages":{
  "read":  {"avg_ms":0.13,"count":6},
  "b64":   {"avg_ms":0.02,"count":6},
  "http":  {"avg_ms":16.14,"count":6},
  "parse": {"avg_ms":0.12,"count":6},
  "gate":  {"avg_ms":0.11,"count":6},
  "write": {"avg_ms":1.47,"count":6}}}
```

注意 `count` 会大于图片数（重试会把 http/parse/gate 重复计一次），这正是判断
"是不是在靠重试刷过质检"的依据。**各阶段不互斥**（tag 与 http 并行），
所以不要把它们当占比相加。

### 9.2 pack 并行：`Packer.prepare()` / `write()` + `pack_stream()`

转码（准备）可以并行，归档写入必须串行——tarfile/zipfile 的句柄内部会 seek，
多线程共用会写串字节。现在两者分开了：

```python
from tagger.pack import Packer, PackOptions, pack_stream

packer = Packer(out_dir, PackOptions(max_records=50000, formats=("webdataset",)))
for sealed in pack_stream(packer, samples, workers=8):
    print("封口", sealed.name, sealed.count)
packer.close()
```

`samples` 是 `prepare()` 的参数字典序列，产出顺序与输入一致（包内顺序可复现）。
实测 8 线程 **1.96×**，不是核数倍。

⚠️ `pack` 目前**没有 CLI 入口**（`Packer` 只是库，命令还没接）。所以这一步
现在只对调用方生效；等接上 `tagger pack` 命令时，直接传 `workers=N` 即可。

### 9.3 parquet 产物：`--artifact-format parquet`

千万级时"每张图两个小文件"会成为真瓶颈。换成攒片后文件数降 4 个数量级：

```powershell
tagger.cmd client ... --artifact-format parquet --parquet-rows 2000
```

- 产物是 `artifacts-00000.parquet`（列：`image_id / tag / nl / ts`），
  tag 与 nl 两列存的是**和 json 文件完全相同的 payload 字符串**，
  所以 merge 的对账口径一字未变；
- 续做（不加 `--force` 时的默认行为）靠启动时读一遍 `image_id` 列建集合，
  只付一次成本；
- `tagger merge` / `tagger report` 已能直接读这种目录；两种形态并存也行，
  内容不同会按"内容冲突"报出来。

实测同一条数据走 json 与走 parquet，merge 的完整率、report 的 tag 数/字符数
完全一致（见 `tests/test_artifacts.py`）。

### 9.4 精度掉落的验证脚本：`devtools/verify_dtype.py`

```powershell
# 真权重（要结论时用这个）
env\python\python.exe devtools\verify_dtype.py --checkpoint <WD 目录> --threshold 0.35

# 自检（下一份 timm 预训练权重，验证流程本身）
env\python\python.exe devtools\verify_dtype.py --arch vit_base_patch16_224 `
    --pretrained vit_base_patch16_224.augreg2_in21k_ft_in1k `
    --size 224 --classes 1000 --images 8 --threshold 0.1
```

自检结果（ViT-B/16，ImageNet-1000 头，合成图，阈值 0.1）：

| 对照 | 平均 IOU | 最低 IOU | IOU<0.98 的图 | 概率漂移中位 |
|---|---:|---:|---:|---:|
| tf32 | 1.0000 | 1.0000 | 0 | 0.000108 |
| bf16 | 0.9995 | 0.9980 | 0 | 0.002051 |

**这不能直接当作 WD tagger 的结论**（词表、头、阈值都不同），但它证明方法可用，
且量级上：bf16 的概率漂移中位 2e-3，判定阈值是 0.35——离边界还有两个数量级
的余量。上生产前用真权重 + 真实素材再跑一遍拿最终数。

---

## 10. 真权重 + 真实素材的最终数（2026-11-02）

前面 §2 的数是在**没有权重**的情况下测的（结构同、FLOPs 同）。这一节补的是
真权重 `ashen-sensored/wd-eva02-tagger-2026-canary`（`model.safetensors` 1.28 GB）
配真实素材 `sucai/3_amu`（79 张 PNG，非合成图）。

### 10.1 前向（真权重，`--rounds 3` 取中位，warmup 8 / iters 30）

```powershell
env\python\python.exe devtools\bench_tag_gpu.py --skip-decode `
    --checkpoint models\wd-eva02 --batches 1 4 --rounds 3
```

| 配置 | ms/张 | 相对 fp32 b1 |
|---|---:|---:|
| fp32 b1（原默认） | 62.6 | 1.00× |
| tf32 b1 | 56.0 | 1.12× |
| fp32 b4 | 54.6 | 1.15× |
| tf32 b4 | 42.6 | 1.47× |
| bf16 b1 | 36.1 | 1.74× |
| **bf16 b4** | **26.5** | **2.36×** |
| bf16 b4 + CUDA Graph | 16.5 | **3.80×** |

**§2 的 3.75× 只在 CUDA Graph 下成立**，不是单纯 batch+bf16 的收益。
（§2 当时无权重测到 bf16 b4 = 15.58 ms，与今天复测的 26.5 ms 对不上，见 10.5 待办。）

测这个数有三个坑，都踩过：
1. **必须预热**。不做 warmup 直接计时，同一配置能差出 2 倍，全在 GPU 降频上；
2. 别在同一个 `timeit` 里按"精度名"挑模型——把 `tf32` 映射到 bf16 模型上跑，
   会得到"tf32 和 bf16 一样快"的假结论；
3. **同机有别的任务在抢 CPU/GPU 时，数字会飘**。bf16 b4 在不同轮次里量到过
   18.5 ms 与 26.5 ms，差 1.4×。本文一律引用**保守值 26.5 ms**。

### 10.2 精度掉落（79 张真实素材，阈值 0.35，边界带 ±0.01）

`devtools/verify_dtype.py --checkpoint models/wd-eva02 --images-dir <素材> --batch 4`

| 对照 | 平均 IOU | 最低 IOU | IOU<0.98 的图 | 翻转 tag | 其中贴阈值 | 边界带内 IOU |
|---|---:|---:|---:|---:|---:|---:|
| tf32 | 0.9997 | 0.9730 | 1 | 1 | 1 | 1.0000 |
| bf16 | 0.9875 | 0.9375 | 30 | 38 | 36 | 0.9994 |

基数：79 张共选中 3002 个 tag（每张 27~49 个），全库 130 万个 logits 里
**只有 0.023% 的漂移超过 0.01**。

怎么看这两个数：

- **tf32：没有任何一个 tag 真的改判**。唯一的 1 个翻面是 0.3499→0.3501 这种，
  剔掉边界带后 IOU 是 1.0000。概率最大漂移 0.0027，比阈值到最近 tag 的距离
  小两个数量级。**可以当默认**。
- **bf16：有 2 个翻面脱离边界带**（79 张里），例如
  `lightning_bolt_symbol` 0.3712 → 0.3251。这是真改判，量级 0.07%，
  但它是**不可归因**的：同一批图换张卡、换个驱动也可能变。
  所以 bf16 是"愿意用 0.07% 的 tag 抖动换 3.48× 前向"的选项，不进默认。

这也是 `devtools/verify_dtype.py` 判据被改掉的原因：原来只看 IOU，一个贴阈值
的 tag 就能把整张图判成 0.93，从而把**边界抖动**误判成**精度掉了**。现在输出
把翻面拆成"贴阈值 / 脱离边界带"两类，判定看后者。

### 10.3 端到端：解码已经超过前向

79 张 PNG 实测，一张图的耗时拆解：

| 段 | ms/张 |
|---|---:|
| 解码 + 预处理（PIL，PNG） | **39.4** |
| 前向 fp32 b1 | 64.4 |
| 前向 bf16 b4 | 18.5 |
| 过滤循环（16473 个 tag） | 2.5（优化前是这里的好几倍） |

于是端到端（含解码，实测 106 ms 与算式吻合）：

| 配置 | 端到端 ms/张 | 相对 fp32 b1 |
|---|---:|---:|
| fp32 b1 | ~104 | 1.00× |
| tf32 b4 | ~84 | 1.24× |
| bf16 b4 | ~68 | **1.53×** |
| bf16 b4 + CUDA Graph | ~58 | 1.79× |

**注意 2.36× 掉到 1.53× 的原因不是前向变慢了，而是解码 39 ms 没变**：
前向从 63 → 26，省下的 36 ms 里有 39 ms 的份额仍被解码占着。
所以下一个真正的瓶颈是**解码**：§1 已测过 torchvision CPU 解码比 PIL 快 2.1×，
接上去之后 bf16 b4 的端到端能到 ~48 ms（2.2×）。§1 的结论（别用 nvJPEG）
不变——单图固定开销吃掉并行收益。

另外：**解码这 39 ms 是 PNG 的数字**（`sucai/3_amu` 是 1 MB 级 PNG）。
§1 用 1024² JPEG 测出来是 14.67 ms。真实素材以 JPEG 为主时，解码占比会小一半，
bf16 的端到端收益会回升到 1.8× 附近。换素材之前不要把 39 ms 当普适值。

### 10.4 本轮落地

| 改动 | 位置 | 效果 |
|---|---|---|
| `--tag-precision fp32\|tf32\|bf16` | `tagger/cpu_tagger.py` / `tagger/client.py` | 默认 **tf32**（0 改判，1.12~1.47×） |
| `--tag-batch N` + `predict_batch()` | 同上 + `_BatchTagExecutor` | 单线程攒批，**不额外等待**；batch=1 时行为与逐张跑一致 |
| **修 bug：`--tag-device` 空转** | `WDEva02Tagger._load` | 原来模型和输入都没搬到 device，`cuda:0` 一直跑 CPU；现在真的上卡 |
| 过滤摊平 | `_filter_plan()` | 每张 16473 个 tag 的类别判断/阈值查表，从每张十几 ms 降到 2.5 ms |
| 判据修正 | `devtools/verify_dtype.py --margin` | 区分边界抖动与真改判 |

`--tag-batch` 只在 wd-eva02 生效，且**客户端要有并发才会攒到批**：
`tagger client --concurrency 1 --tag-batch 4` 等于没开，因为队列里永远只有一张。

复跑：

```powershell
# 前向（真权重 + 真实素材）
env\python\python.exe devtools\verify_dtype.py --checkpoint models\wd-eva02 `
    --images-dir <素材目录> --images 79 --batch 4

# 端到端：tf32 默认 vs 显式 fp32，看产物与 --stats-json 里的 tag 段
tagger.cmd client ... --tag-backend wd-eva02 --tag-model-dir models\wd-eva02 `
    --tag-device cuda:0 --tag-batch 4 --stats-json work/stats.json
```

### 10.5 待办

- [ ] **bf16 b4 与 CUDA Graph 的耗时定稿**。同一条命令在**机器空闲**时连跑 3 次
      取中位。现在量到 18.5 / 26.5 ms 两个值（差 1.4×），怀疑跑测时同机有其他
      任务在抢 CPU/GPU；CUDA Graph 那一档（bench 里 16.5 ms）也只有一轮数据。
      定稿前文档与 CLI 提示一律引用保守值 26.5 ms（2.36×）。
- [x] ~~接 torchvision CPU 解码~~ → **评估后不做**，理由见 §11.1。
- [x] **CUDA Graph 接进 tag 路径** → 已落地，见 §11.2（数值与 eager 完全一致）。
- [x] **JPEG 素材复测解码占比** → 见 §11.4（注意：素材是转码的，真实 JPEG 素材仍待补）。
- [x] **把 precision 写进 provenance** → 见 §11.5，顺带修了 `tag_backend` 一直是
      "(缺失)" 的 bug。

---

## 11. 第二轮：解码 / CUDA Graph / 溯源（2026-11-02）

### 11.1 torchvision 解码：**算完账之后决定不做**

§1 说"torchvision CPU 解码比 PIL 快 2.1×"，这个数是对的，但它只量了解码本身。
接进真实链路要多一步"转回 PIL 才能喂 timm 的 transform"，那一步把收益吃干净了：

| 路径 | PNG（1129 KB） | JPEG（211 KB） |
|---|---:|---:|
| PIL open + convert（现状） | 16.73 ms | 6.43 ms |
| torchvision decode（到张量） | 14.21 ms | 3.23 ms |
| **torchvision decode + to_pil** | 16.01 ms | **6.16 ms** |
| → 相对现状 | **1.04×** | **1.04×** |

**所以"换解码"这条路对当前预处理链是死胡同**：省下的 3 ms 全花在回程拷贝上。

真正有收益的是**整条预处理留在张量上**（解码 + 缩放 + 归一化都不回 PIL）：

| 格式 | 现状（PIL 解码 + timm transform） | 全 torch | 加速 |
|---|---:|---:|---:|
| PNG | 32.83 ms | 19.88 ms | 1.65× |
| JPEG | 17.56 ms | 7.98 ms | **2.20×** |

**但代价是换了一套重采样实现**：像素最大差 0.276（归一化后），tag 集合平均
IOU 0.987 / 最低 0.9688（JPEG 组 0.9932 / 0.9630）。也就是说它会像 bf16 一样
改变约 1% 的选中 tag，而且是**像素级**的差异（PIL bicubic 与 torch bicubic 的
抗锯齿/边界处理不同），不是数值噪声。

按本项目一贯的口径（bf16 因为 0.07% 改判就只做 opt-in），这一步不做默认。
真要做，必须像 §10.2 那样单独验一遍、并且做成开关。

顺带抄到的一个事实（**下次改预处理必须知道**）：这个权重的 timm transform 是

```
Resize(size=(448,448), interpolation=bicubic) → CenterCrop(448) → Normalize
```

`Resize((448,448))` 是**直接拉伸成正方形**，不是短边缩放 + 裁剪（CenterCrop
因此是空操作）。手写预处理时按"短边缩放"抄，等于换了一套口径。

### 11.2 CUDA Graph：接进 tag 路径，数值零差异

`WDEva02Tagger` 现在在「CUDA + batch>1」时自动抓一张固定形状的图，
满批走图回放、尾批（不足一批）走 eager：

- **数值**：8 张图对比图回放与 eager，tag 集合完全相同、概率最大差
  **0.00000000**（记录的是同一批 kernel，只是不再逐次发射）；
- **尾批**：6 张 / batch=4 → 4 张走图 + 2 张走 eager，结果一致，不崩；
- **失败回落**：抓图抛异常（驱动/显存/自定义算子）只打印一行，继续用 eager；
- **开关**：`use_graph=False` 或 `batch_size=1` 时不建图（b1 上图没有收益，
  §2 的 36.08 vs 36.49 就是证据）。

⚠️ 速度数字这一轮**不敢写**：量的时候机器上有别的任务在抢资源，端到端从
106 ms/张 涨到 400 ms/张，同配置两次差 1.4×。已记进 §10.5 待复测。

### 11.3 顺手修掉：每张图被 `convert("RGB")` 了两次

客户端先 `Image.open(...).convert("RGB")` 一次，后端 `_open_rgb` 又
`convert("RGB")` 一次——而 PIL 即使在 RGB 图上 convert 也会整幅复制。
现在客户端把**原始字节**直接交给后端，解码只发生一次；`_open_rgb` 也
只在 mode 不是 RGB 时才 convert。像素结果与改动前逐位一致。

### 11.4 JPEG 素材下的解码占比

§10.3 的 39.4 ms（解码+预处理）是 **PNG** 的数字。同一批图转成 q95 JPEG
（211 KB）后：

| 格式 | 解码+预处理 | fp32 b1 端到端 | bf16 b4 端到端 | bf16 收益 |
|---|---:|---:|---:|---:|
| PNG（1129 KB） | 32.8 ms | ~95 ms | ~59 ms | 1.61× |
| JPEG（211 KB） | 17.6 ms | ~80 ms | ~44 ms | **1.82×** |

结论：**素材以 JPEG 为主时，解码占比从 ~35% 降到 ~22%，bf16 的端到端收益从
1.5× 回到 1.8×**。所以 §10.3 那句"下一个瓶颈是解码"要分格式说，不能一概而论。

⚠️ 这组 JPEG 是从 PNG 转码来的（quality 95），不是线上的原始 JPEG；真实素材
的量化表不同，解码耗时会略有出入。**要钉死这个数，得拿一批真 JPEG 素材重跑**。

### 11.5 溯源：补 `tag_precision`，并修了 `tag_backend` 一直是"(缺失)"的 bug

`merge` 的 `PROVENANCE_DIMS` 里有 `tag_backend`，但它去 `provenance` 字典里
取——而客户端从来没往 `provenance` 里写过这个字段（只写在 payload 顶层），
于是这一维**永远报"(缺失)"**，等于白设。现在：

- tag 产物的 `provenance` 里写入 `tag_backend` 与 `tag_precision`；
- `PROVENANCE_DIMS` 增加 `tag_precision`，并新增 `TAG_ONLY_DIMS`
  （这两个维度只查 tag 那份 provenance，避免把"只交了 NL"误报成"溯源缺失"）；
- `--stats-json` 里一并记 `tag_precision / tag_batch / tag_graph`。

为什么非记不可：fp32 / tf32 / bf16 出的 tag 集合不一样，混批之后想回答
"这批为什么多出几个 tag"，没有这一维只能靠猜。注意与 `provenance.precision`
区分——那个是 VLM 的精度。

---

## 12. 第三轮：正确性止损 + 可观测性（2026-11-02）

这一轮不动性能，全部是"别让错误静默发生"。四条按风险排序落地。

### 12.1 词表与分类头的一致性校验

`_filter` 是 `zip(names, probs, categories)`。以前 `_load` 读 `num_classes`
建头、`_read_wd_tags` 读 csv 建词表，两边**从不交叉校验**：

- csv 比头多 → 末尾的 tag 永远出不来，其余全部错位；
- csv 比头少 → 截断点之前的对应关系整体偏一格；
- csv 里有空名字 → 原来的实现会补空字符串，空 tag 一路传进产物；
- `num_classes` 缺失 → 0 会被 timm 当成默认 1000 类，权重必然对不上。

四种情况都不抛异常、日志正常、产物里的 tag 全都"看着像那么回事"。
30M 张的规模上，这种错位等于永久污染。

现在：先读词表（在加载 1.3 GB 权重**之前**）→ `_check_vocab_against_head()`
对账 → 长度不等直接抛 `TaggerUnavailable`，并在错误信息里给出两个数字和
"csv 与 safetensors 必须同一次导出"。空名字、非整数 `tag_id`、`tag_id`
不连续也都改成直接报错，不再补空。

### 12.2 分段计时再拆一层：`tag_decode` / `tag_fwd`

`tag` 这一段一直混着"解码+预处理"和"前向"。实测两者的比例随精度和图片
格式翻来覆去（PNG 解码 33 ms 对前向 26~63 ms），只看合数根本判断不了该
优化哪边——这也是 §11.1 那个"torchvision 解码 2.1× 其实白干"的结论
必须靠临时脚本才挖出来的原因。

现在 `WDEva02Tagger._predict_many` 自己给解码计时，经 `last_timings`
报到客户端，`--stats-json` 里就多了 `tag_decode` / `tag_fwd` 两段。
没拆分能力的后端（pixai / onnx）只报 `tag`，绝不编一个解码占比出来。
过滤循环（16473 个 tag）刻意不算进前向：它是 CPU 后处理，混进 GPU 段会
让人误以为换精度能省掉它，差额由 `total` 自己吸收。

### 12.3 rating 从"算完就扔"改成独立字段

这个仓库的词表构成：general 11601、character 4868、**rating 4**。
`DEFAULT_KEEP` 不含 rating，所以模型每张图都算出的四个分数被直接丢弃。

丢的方向是对的（描述审核等级的 tag 混进训练串会给模型喂错误先验），
但**正确的做法是单列字段**：tag 产物的 `payload["rating"]` 现在是
`{general, sensitive, questionable, explicit}` 四个原始分数。

两个实现细节值得记：

- rating **不按阈值过滤**。判定靠四个分数的相对大小（argmax），按 0.35
  过滤会把 `0.31/0.28/0.25/0.16` 这种真实分布削成空；
- 四个是**独立 sigmoid**，加起来不等于 1（实测某张图 ≈ 1.44）。想当
  概率用必须先自己归一化。

### 12.4 黄金数值测试

仓库里 150+ 个测试原来全是结构性断言，没有一条会因为"预处理换了实现"
而失败。现在有 `tests/test_tag_golden.py`：

- 两张**合成图**（固定种子画色块，不提交二进制素材）；
- 在真实权重上跑 fp32 / **CPU**（与驱动、tf32、CUDA 版本无关，换机器可复现）；
- 钉住稳定区（离阈值 > 0.02）的 tag 集合 + 概率（容差 5e-3），
  rating 钉"谁占主导"；
- 没有 `models/wd-eva02` 时自动 skip，CI 不受影响。

边界带内的 tag 不断言：那部分换台机器自己也会翻面，钉死只会把测试变成
环境探测器。判据与 `verify_dtype.py --margin` 是同一套口径。

期望值由 `devtools/dump_golden.py` 生成。**刷新基线必须写清为什么口径变了**，
否则这次测试就退化成"改完之后顺手重刷"的橡皮图章。

### 12.5 这轮的校验

`pytest` 159 passed（新增 10 个：词表校验 4、rating 2、耗时拆分 2、
黄金 2），lint 0。黄金测试在本机跑通（22 + 13 个 tag）。

### 12.6 下一步（按收益排序，都还没做）

1. **阈值校准**。现在 11601 个 general 与 4868 个 character 共用 0.35。
   黄金测试里那张纯色块图都吐出了 `komeiji_koishi`（0.7896）——说明
   character 类在 0.35 下非常松，而同人图上错误的角色名是毒标签。
   `thresholds` 已支持按类别给，缺的是一份人工标注小样本把每一类校准到
   相近的精度。这是对数据质量影响最大的一条。
2. ~~**解码与预取**。`_predict_many` 现在先解完整批再前向，批量下 33 ms 的
   解码是纯等待。加一个预取线程（解下一批的同时跑当前批）能省
   `min(解码, 前向)`，且**像素逐位不变**——仍是同一条 PIL 路径。~~
   **已做，见 §13**：实测 1.75×，逐位一致。
3. **bf16 / CUDA Graph 的耗时定稿**（见 §10.5），必须在机器空闲时跑。

---

## 13. 第四轮：解码预取（2026-11-02）

§12.6 第 2 条落地。**一句话：CUDA 上批量打标 1.75×，tag 逐位一致。**
数字由 `devtools/bench_prefetch.py` 出（A/B 交替、每轮单独出比值）。

### 13.1 做法

`_predict_many` 原来是一轮一轮地 `解码 → 前向`。现在在 CUDA 上把解码挪到
一个**单 worker 后台池**，前向跑第 N 批时第 N+1 批正在解码：

- 只在 `prefetch=True`（默认）且 `len(parts) > 1` 且设备是 cuda 时启用。
  **CPU 上不预取**：两边抢同一批核，重叠只会互相拖；
- 最多两批在途（在算的解完的）。一次把整批提交出去会把 79 张解好的张量
  全压在内存里（约 190 MB），收益为零、内存翻倍；
- 像素逐位不变：还是同一条 PIL 路径，只是换了执行的时间位置。

工厂侧 `build_backend(..., prefetch=True)` 与 `use_graph` 同样透传（写进
`kw` 而不是按后端名分支），别的后端收了不用也不报错。

### 13.2 实测（真权重，bf16，batch=4，24 张 PNG，3 轮交替）

| 配置 | ms/张 | 相对 |
|---|---:|---:|
| `prefetch=False`（等价于旧行为） | 117.9 | 1.00× |
| `prefetch=True` | **67.5** | **1.75×** |

逐轮比值 1.69 / 1.73 / 1.82×，离散度小，**说明收益不是某一次撞上的**。
（同一台机器前景跑过别的任务时绝对值会整体上浮 2×，但比值不动——所以
只信逐轮比值，别拿两次不同时间的绝对 ms 互相比。）

`last_timings`（整次调用累计，毫秒）正好把重叠照出来：

| 配置 | total | decode | forward | decode+forward |
|---|---:|---:|---:|---:|
| `prefetch=False` | 2883 | 741 | 2089 | 2830 ≈ total → **串行** |
| `prefetch=True` | 1701 | 826 | 1507 | 2333 ≫ total → **重叠** |

省下来的量级 ≈ `min(解码, 前向)`：这里解码 34 ms/张、前向 62 ms/张，串行
118 ≈ 31+87，预取后 67.5 ≈ max(34,62)——公式对得上，没有额外收获可挖。

⚠️ 判据留档：**预取生效时 `decode + forward` 必然大于 `total`**，这不是
统计 bug。以后看到这个不等式别去"修"计时器；反过来，如果两者基本相等，
说明预取没生效（大概又退回串行分支了）。

### 13.3 等价性

同一批 24 张，`prefetch=False` 与 `True` 的 tag 列表 `==` 比较为 True
（tag 名与概率全等，不是 IOU，是逐项相等）。

### 13.4 复跑

    env\\python\\python.exe devtools\\bench_prefetch.py --images 24 --batch 4 --rounds 3

脚本自带 warmup（跑满一批，避免 CUDA Graph 抓图那次落进计时），并逐轮
打印比值。**默认参数别改成 batch=1**：单批没有下一批可解，此时脚本量的是
"同一件事做两遍"的噪声。

### 13.5 校验

`pytest` 161 passed（新增 2 个：开关透传、CPU 上必须退回串行），lint 0。



## 14. 第五轮：预处理上卡（2026-11-04）

### 14.1 结论先说

解码**不搬**，只把 timm transform 的缩放+归一化搬到卡上（`preprocess=tensor`）。

理由是量出来的，不是猜的（4070 Ti SUPER / torch 2.14.1+cu126 / 1024² q90）：

| 场景 | PIL（现状） | GPU nvJPEG | 结果 |
|---|---:|---:|---|
| 单张解码（生产是逐张喂） | 11.3 ms | 15.2 ms | GPU 更慢 |
| 批 8 仅解码 | 5.4 ms/张 | 4.6 ms/张 | 只省 0.8 |
| 批 8 解码 + 回 PIL 做 transform | 15.6 ms/张 | 23.3 ms/张 | 0.67×，净亏 |
| 批 8 解码+缩放+归一化全留张量 | 15.5 ms/张 | 4.5 ms/张 | 3.4× |

即：nvJPEG 的优势全在批量上，而 `_decode_batch` 是逐张；而 15.5 ms 里有
约 10 ms 是 PIL 的 bicubic 缩放 + 归一化，不是解码。所以只搬那 10 ms。

### 14.2 端到端实测（bf16 + batch=4 + CUDA Graph，chunk=batch*8）

| 素材（各 32 张） | pil | tensor | 加速 |
|---|---:|---:|---:|
| 1024² PNG 3 MB | 29.3 ms/张 | 20.0 ms/张 | **1.47×** |
| 1024² JPEG q90 | 27.0 ms/张 | 25.0 ms/张 | 1.08× |
| 768×512 PNG（平滑） | 19.1 ms/张 | 18.1 ms/张 | 1.06× |

分阶段（768×512 PNG，每批 4 张）：

- pil：decode 6.0 ms/张，forward 16.0 ms/张
- tensor：decode 1.3 ms/张（只解码 + 数组化），forward 15.8 ms/张（已含 H2D + 缩放 + 归一化）

**什么时候开**：预取在的时候墙钟 ≈ `max(CPU 预处理, GPU 前向)`。tensor 把 CPU 侧
砍掉 transform、给 GPU 侧加 1~2 ms/张，所以只有"CPU 预处理本来就比前向重"才赚。
1024² PNG 这种解码就 20 ms 的赚到 1.5×；JPEG / 小图基本打平，别开。

### 14.3 代价：tag 会变（重采样换了实现）

PIL 的 bicubic 与 `F.interpolate(bicubic, antialias=True)` 是两套核。归一化后的
像素差 p99 ≤ 0.013（约 1 个灰阶），但边缘像素能到 0.25，阈值 0.35 附近的 tag 会翻。

真权重实测（默认阈值 0.35）：

| 素材 | 改判图 | 平均 tag IOU |
|---|---:|---:|
| 40 张平滑 PNG（work/_lt_smoke） | 0/40 | 1.000 |
| 25 张 skimage 合成/线稿/超小图 | 15/25 | 0.921（最低 0.696） |

所以它是 opt-in、默认 `pil`；并且**线稿/高对比素材别开**——那些图的重采样差最大。
（对照组：同一后端连跑两遍 tag 逐条一致，说明上面不是噪声。）

### 14.4 实现要点

- `normalize_preprocess`：`pil`（默认）/`tensor`，认 `gpu`/`cuda`/`pillow` 等别名。
- `_tensor_prep_plan(transform)`：**从 timm 建好的 transform 里读** Resize /
  CenterCrop / Normalize，认不出来（RandomResizedCrop、LANCZOS、没有 Normalize）
  就返回 None → 回落 PIL 并打一行日志。不写死 448/bicubic：换个分类头就是另一套参数，
  写死等于埋"换模型悄悄换口径"。
- `_pixels_uint8`：解码仍走 PIL，只到 uint8 CHW（`np.array` 而不是 `asarray`——
  PIL 的数组视图只读，`torch.from_numpy` 会告警）。
- `_prep_on_device`：H2D → `F.interpolate`（bicubic + antialias；nearest 关 antialias）
  → 中心裁剪（`round` 对齐 torchvision）→ `/255` → `(x-mean)/std`。
- 计时口径：tensor 的 H2D + 缩放 + 归一化**算进 forward**（它占的就是前向那条流），
  decode 只算 CPU 解码。否则 `tag_decode` 会凭空变小，看着像解码变快了。
- 预取保留：解码仍在 CPU，照样能和 GPU 前向重叠。

### 14.5 接线与校验

- CLI：`tagger tag --preprocess`、`tagger pack --preprocess`、`tagger client --tag-preprocess`
- Web：`JobSpec.preprocess` → `build_backend(preprocess=…)`；UI「模型与性能 → 预处理」
  两档 `CPU（默认）/ GPU`；运行页/历史页在开了时显示 `· GPU 预处理`
- 溯源：`tag_preprocess` 进 `Provenance`、`TAG_ONLY_DIMS`、`PROVENANCE_DIMS`
  （事后比两批 tag 数量时，得能分辨是口径变了还是模型变了）
- 测试：`tests/test_cpu_tagger.py` 新增 7 条（别名/默认/pil 与 tensor 两条路的
  transform 解析、不支持的算子必须回落、CPU 上两条路的像素差 < 0.05）
- 全量 `pytest tests` 通过；`cd tagger/web/ui && npm run build`（tsc + vite）通过
