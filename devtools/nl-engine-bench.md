# NL 引擎实测：Windows 上到底能不能跑，跑多快

> 这份文档记的是**测出来的数**，不是估的。测量脚本是
> `devtools/bench_nl_engine.py`，同一台机器可重跑。
> 换模型 / 换量化档位 / 换 prompt 长度之前，先重跑一遍再比较。

---

## 0. 为什么要测

需求量级：**3000 万张，每人约 300 万张，0.1% 人眼抽检**。这个量级下
"引擎差 2 倍"就是"11 天 vs 22 天"，必须用实测数字定，不能凭印象。
同时有个硬约束：成员机是 **Windows 原生**。

## 1. 环境

| 项 | 值 |
|---|---|
| GPU | RTX 4070 Ti SUPER 16 GB |
| CPU / RAM | Ryzen 9 5900X / 96 GB |
| 驱动 | 595.79 |
| 引擎 A | llama.cpp `b12088`（官方 Windows CUDA 13.4 预编译包） |
| 引擎 B | mistral.rs `0.9.5`（PyPI Windows wheel） |
| 模型 | `Qwen3-VL-8B-Instruct-Q4_K_M` + `mmproj-…-F16`（官方 GGUF） |
| 输入 | 24 张真实照片，源图 4K；准备阶段按 `max_pixels=589824` 缩放 |
| prompt | 直接 import `tagger.client._prompt_for`（与生产同源，630 字符） |
| 采样 | temperature 0.2 / top_p 0.9 / max_tokens 800 |

## 2. 结论先说

| 引擎 | Windows GPU | 解码速度 | 判定 |
|---|---|---:|---|
| **llama.cpp** | ✅ CUDA 官方预编译 | **68 tok/s（单流）· 206 tok/s（batch 4）** | **可用** |
| mistral.rs | ❌ 只有 CPU zip | 4.86 tok/s | 不可用（慢 40 倍） |
| vLLM 官方 | ❌ Linux only | — | 只能 WSL2 / 远程 |
| vllm-windows-build | ⚠️ 有原生 wheel | — | 无多模态证据，且不读 GGUF |

**mistral.rs 的 CUDA wheel 只发布 manylinux 版本，Windows 侧只有
`mistralrs-cpu-x86_64-pc-windows-msvc.zip`。** 这是它"架构最好但用不上"
的唯一原因，不是配置问题。

## 3. llama.cpp 并发梯度（8B-Q4_K_M，16 张图）

| 并发 | 张/秒 | p50 | p95 | tok/秒 | 可用率 | 均字符 |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 0.41 | 2.34s | 2.68s | 68 | 100% | 791 |
| 2 | 0.74 | 2.51s | 3.20s | 123 | 100% | 805 |
| **4** | **1.27** | 2.81s | 3.39s | 206 | 100% | 780 |
| 8 | 1.34 | 5.22s | 6.33s | 215 | 100% | 771 |

- **4 个 slot 就饱和**。从 4 加到 8：吞吐 +5%，p95 从 3.39s 涨到 6.33s。
  故意多开只会让抽检看到更长的等待，不会多产出。
- 4 档之前近似线性（0.41 → 0.74 → 1.27），说明 decode 确实被批起来了；
  这也是 llama.cpp 比"自己写循环"强的那部分。

## 4. 客户端准备成本（缩放 + base64，纯 CPU）

| 线程 | ms/张 | 张/秒 | 加速比 |
|---:|---:|---:|---:|
| 1 | 65.99 | 15.2 | 1.00× |
| 4 | 20.07 | 49.8 | 3.29× |
| 8 | 15.94 | 62.7 | 4.14× |

**单线程 66ms/张 = 15 张/秒的硬天花板。** 推理侧 1.27 张/秒远低于它，
所以现在瓶颈确实在 GPU；但一旦换更小模型或加机器，这里会先撞墙，
届时必须把准备放进线程池（4 线程就能到 50 张/秒）。

## 5. 机时推算（按最佳档位 1.27 张/秒）

| 规模 | 单机 | 4 台 | 8 台 | 16 台 | 32 台 |
|---|---:|---:|---:|---:|---:|
| 每人 300 万张 | 26.0 天 | 6.5 天 | 3.2 天 | 1.6 天 | 0.8 天 |
| 全量 3000 万张 | 260 天 | 65 天 | 32 天 | 16 天 | 8 天 |

读法：
- **单机不可能跑完 3000 万张**，这不是"再优化一下"能解决的量级差。
- 要 30 天全量收工 → 至少 9 台同规格机器。
- 要每人 3 天内交 → 每人配 9 台，或降规格（4B 约 2×，见下）。

## 6. 还没测的（下一步）

- **Qwen3-VL-4B**：同口径应当约 2×（权重 2.33GB vs 4.68GB），
  但 caption 质量是否够用必须 A/B，不能只看速度。
- **`--image-max-tokens` 下调**：589824 像素 = 576 视觉 token 是目前最贵的
  一项。降到 360 附近通常不掉质量，prefill 能省三分之一。
- **Q8_0 / bf16**：质量上界，代价是权重翻倍、吞吐下降，只适合抽检对照。

## 7. 槽位与上下文预算（把 GPU 从 58% 喂到 82%）

`-np`（slot 数）与 `-c`（总上下文**池**）的配比是 llama.cpp 上最容易被抄错的地方：
照搬 vLLM 的 `max_num_seqs × max_model_len`（4 × 8192）只用到 58% 的 GPU。

实测（8B-Q4_K_M，真实素材，输出约 200 token）：

| slot | 每 slot 上下文 | KV | 张/秒 | p50 | GPU util | 显存 |
|---:|---:|---|---:|---:|---:|---:|
| 4 | 8192 | f16 | 0.91 | 3.80s | 58% | 12.1 GB |
| 8 | 4096 | f16 | 1.00 | 7.21s | 79% | 11.6 GB |
| **12** | **2816** | **q8_0** | **1.22** | 8.37s | **82%** | 9.7 GB |
| 16 | 2560 | q8_0 | 1.18 | 11.88s | 79% | 10.1 GB |

**12 是拐点**，16 反而更慢（compute 顶到了）。延迟随 slot 上升，但打标是批处理，
看张/秒不看 p50。

两个反直觉的点：

1. **8 slot × 64K 总上下文会让 decode 崩到 1 tok/s** —— 显存吃满 15.8/16.4 GB。
   同样的卡配成 "12 slot × 32K 总上下文" 就很健康（9.7 GB）。
2. **prefill 不是瓶颈**：并发 1→4 时短输出（max_tokens=16）从 1.37 → 5.70 张/秒，
   即 **4.16×**，说明视觉编码是**按批并行**的，没有串行化。
   所以"加 IO 队列 / 预取"对吞吐的贡献只有 2~3%（准备 82ms vs 请求 2.5s）。

已写进代码：`nl_engine.slot_plan()` 按显存分档（16G→12、10G→8、7G→4、未知→2），
`-c = slot 数 × 3072`、`-ctk/-ctv q8_0`、`--image-max-tokens` 由 `max_pixels` 换算。

## 8. 复现

```powershell
# 1) llama.cpp（Windows CUDA 包，含 cudart）解压后指定路径
setx TAGGER_LLAMA_SERVER D:\llama\llama-server.exe

# 2) 权重放模型目录（官方 GGUF：Qwen/Qwen3-VL-8B-Instruct-GGUF）
#    models/nl/qwen3vl-8b/Qwen3VL-8B-Instruct-Q4_K_M.gguf
#    models/nl/qwen3vl-8b/mmproj-Qwen3VL-8B-Instruct-F16.gguf

# 3) 起服务（页面里也能选，这里用命令行便于复现）
env\python\python.exe -m tagger.cli serve --engine llamacpp --model qwen3vl-8b --port 8080

# 4) 跑基准
env\python\python.exe devtools\bench_nl_engine.py --images <图片目录> ^
    --engine llamacpp-q4km-8b --url http://127.0.0.1:8080 --limit 16 ^
    --concurrency 1 2 4 8
```
