# ToriiGate-0.5 发色复核实测

结论：可以作为动漫分角色描述与辅助复核候选，本轮没有证明相近发色优于现有 Qwen。金发、棕发、蓝发、绿发重点例可用；银灰白细分类与挑染/高光仍不稳定，不直接替换自动裁决。

[逐图对照报告](index.html)展示图片、两模型完整输出、此前专用分类器前三原生概率，以及 41 个重点例的人工看图注释。人工注释不是模型输出，也不是全量发色真值。

## 测试范围与方法

复用此前 handoff 指定 `jasperai/monet` 三个包的全部 120 张原图，各来源 40 张；另外复用两张手工头部裁图及四个动漫原型案例，共 126 个独立输入。没有根据 ToriiGate 新结果挑选主测试图，也没有重新抽包或全包处理。

所有图片先处理 EXIF 方向，转换 RGB，长宽比不变地以 LANCZOS 限制在 100 万像素内，存成无损 PNG。两模型使用完全相同的 PNG 字节；源 SHA256 与此前报告核对一致。历史 PixAI、MobileNet、CAFormer 结果使用此前原图输入，只作背景信息，没有冒充本轮重跑结果。

两模型均独立看图，未提供已有标签、角色名、颜色候选或已知角色属性：

- ToriiGate：固定官方 `scripts/prompts.py` 的 `min_structured_json`，`USE_NAMES=False`，所有 grounding 关闭；官方 system prompt 和 chat template；temperature=0.5，max_tokens=2048，seed=20261119。
- Qwen：现有 Qwen3VL-8B-abliterated Q6_K + F16 mmproj，使用现有发色 JSON 提示；temperature=0，max_tokens=180，seed=20261119。
- 主测试完成后，用另一种官方 `json` 格式重复 8 个重点例；所有其他条件相同。格式对照不是重复采样稳定性或 BF16 量化精度验收。

两模型都是请求串行，服务先后启动；Qwen 完成并退出后才启动 ToriiGate。使用本机 RTX 4070 Ti SUPER 16 GB 和现有 llama.cpp 0.9.0-dev build 12188、commit `095668452`，无需替换生产推理运行时。ToriiGate context=8192，Qwen context=4096，image_min_tokens=1024、image_max_tokens=4096，单 slot，全 GPU offload。推理后 GPU 显存回落约 1577 MiB。

后续 GPU 测试也按用户要求串行，启动前检查其他窗口任务；本轮未实现跨窗口自动互斥机制或增加生产 gate。

## 运行结果

| 项目 | Torii 精简 JSON | 现有 Qwen 发色 JSON | Torii 普通 JSON 对照 |
| --- | ---: | ---: | ---: |
| 请求数 | 126 | 126 | 8 |
| 请求失败 / JSON 错误 / 输出截断 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| 单图耗时中位 | 2.58 s | 1.30 s | 3.56 s |
| 单图耗时 P95 | 4.78 s | 1.59 s | 4.33 s |
| 平均输出 token | 259.2 | 45.9 | 345.6 |
| 生成 token/s 中位 | 111.5 | 72.5 | 111.8 |
| 请求后整卡显存占用最大采样 | 6592 MiB | 10329 MiB | 6421 MiB |

Torii 生成完整描述，Qwen 只生成短发色 JSON；不能把单图耗时直接解释成相同输出量下模型速度。显存是请求完成后采到的整卡总占用，包含桌面和其他进程，不能当成模型真实峰值或精确净占用。没有连续显存峰值监控。本轮无 prompt cache 命中，未出现输出落入 reasoning 通道的解析问题。

## 看图核实的重点

| 样本 | Torii 精简 JSON | Qwen | 判断 |
| --- | --- | --- | --- |
| 金发肖像 `portrait` | blonde | blonde | 两者正常；关闭猜名后 Torii 仍给角色名 |
| 棕发 `hutao` | brown | brown | 两者正常，没有修复所有红棕误差的证据 |
| 绿色动漫 `58` | green | green | 两者一致 |
| 三人动漫 `101` | light blue / brown / brown | blue，标记多人，理由列三人 | Torii 的分角色结构更方便提取 |
| 三人动漫 `mostima` | red / blue / pink | color=blonde，标记多人/多色，理由提到红蓝粉 | Torii 输出更适合逐角色看图复核；细节仍有幻觉 |
| 蓝发裁图 `201` | blue | blue | 两种 Torii 格式均为 blue；不能据一个例子验收蓝青边界 |
| 银灰发原图 `107` | white | silver | Torii 普通 JSON 改为 grey |
| 同图头部裁图 `202` | grey | silver | Torii 普通 JSON 又给 silver；浅色细分类不稳定 |
| 黑白铅笔画 `106` | 明确 monochrome / graphite，未猜具体色 | blonde | Torii 保留灰度属性较合理，但没有现有契约的 uncertain 标记 |
| 蓝紫发裁图 `mostima-head` | deep blue + blonde streaks | blue，标记多人/多色 | Torii 两种格式都把金色高光描述为金色挑染；普通 JSON 识别裁框残留人物，精简格式漏写 |

上轮确认的 18 张“没有可见人类头发却自动接受”的图，本轮 Qwen 全部返回 null；Torii 描述相应环境、商品或腿部，没有给人类发色。蟹壳细毛也被写进描述，因此简单搜索 `hair` 不足以判断适用对象。

上轮 8 张动物、玩偶、雕像误接受样本，本轮 Qwen 仍全给具体人类发色；Torii 的描述能明确这些对象类型。它没有直接输出结构化的“非人类/无头发”判据，不能把这点称为现有级联已修复，更不能只看 `Character 1` 键就当成人类。接入仍需可靠对象类型与头发属性适配。

多人图可以按角色分别写颜色，但人数、背景人物和遮挡属性会漏写或多写；有些描述只写 `dark hair` 或发型。关闭名字猜测也不保证不输出名字，因此不能依赖角色名称作为颜色真值。

没有全量人工发色或像素级真值；实际动漫样本很少，未建立红棕、蓝青、银灰白等相近颜色的专项均衡数据。本轮不报告准确率、IoU、提升比例或自动通过率，不改生产接受规则。

## 模型与证据

原模型：[Minthy/ToriiGate-0.5](https://huggingface.co/Minthy/ToriiGate-0.5)，revision `7d6b237af26e69f6ee9b381fe7beb3340cecabae`。GGUF：[DraconicDragon/ToriiGate-0.5-GGUF](https://huggingface.co/DraconicDragon/ToriiGate-0.5-GGUF)，revision `8bebf1fbf883b6d8f93ff3e8d157c615fe6b608c`，使用 Q6_K + FP16 视觉编码器，存于 `E:\dev_model\ToriiGate-0.5`。

- `ToriiGate-0.5-Q6_K.gguf`：3,985,524,576 B，SHA256 `2452c180427a127fe536e03c00cf5249f38c1378ae4d17a29e1a9f2371b5a852`。
- `ToriiGate-0.5-fp16.mmproj.gguf`：672,423,360 B，SHA256 `e0470d4bff4e932ecf3c7bf3b937d51301722c5d17c43f7ad9dfc73b65808b9c`。

两个文件大小及实际 SHA256 均与固定 revision 的 LFS 记录匹配，没有把 0.5 当作参数量。测试的是社区 Q6_K 量化版本，未验证原 BF16 权重精度。

`samples.json` 保留所有源路径、源 hash、输入 hash、裁图和旧结果；`torii-results.json` / `qwen-results.json` / `torii-json-results.json` 保留完整原始响应、提示、采样参数、usage 和耗时；`manual-audit.json` 为人工重点注释；`verification.json` / `browser-results.json` 为证据校验。报告可本地直接打开，不依赖服务。

证据校验通过：260 个请求均为有效 JSON，无失败、截断、prompt cache 命中或 reasoning 通道问题；126 个输入均完整解码，源 hash 与此前结果一致，两套 VLM 输入 hash 一致。Edge 实际检查通过：126 张卡片、126 张图加载；41 个重点、18 张无头发、8 张非人类对象筛选正确，搜索与另一格式展开可用，390px 无横向溢出，页面脚本错误为 0。

临时下载、推理、报告生成和校验脚本，以及精确 PNG 输入，测试完成后从 `backend/smoke` 移至 `.delete/toriigate-tests-20261119/backend/smoke/torii-evaluation/`。所有自己启动的模型进程已停止；没有修改生产逻辑、写入人工决定、commit 或 push。
