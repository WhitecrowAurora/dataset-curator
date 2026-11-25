# 提示词修正与实测（2026-11-20）

代码回归通过；画师注入功能已具备。密集文字的规则冲突已修正，但当前 Qwen3-VL / Torii 的指令遵循不足，不能宣称能够稳定忽略对白或消除身份幻觉。

## 本轮修正

- `prompts/图中文字规则.md`：去掉全量逐字转写与依赖 `CAPTION_DENSE` 节名的条件说明。解析器实际把该节名转换成 `<CAPTION> rules:`，模型看不到原节名。改为直接要求精简密集对白、重复标签和小字，仅保留显著、清晰、有用的短引文，不猜缺字。
- `prompts/打标规则.md`、`prompts/密集文字重打规则.md`、`tagger/client.py` 内置兜底：统一文字筛选和签名处理；保留此前对可见颜色、材质、机位等约束的修正。
- `prompts/画师规则.md`：图中文字引用服从正文筛选，不以签名或水印判断作者。可信来源的画师信息仍由既有产物注入逻辑处理。
- `tagger/hair_color/vlm.py`：Torii 使用独立 JSON 提示词，已同步密集文字与不猜身份规则，保留原字段、JSON格式和人物/头发边界。
- `prompt/默认提示词.json`：重新生成中英文快照，与内置默认产出规格一致。自定义提示词仍按既有整段替换约定处理。
- `tests/test_client.py`：更新原有引文与身份隔离断言，适应新的选择性引用措辞。

## 验证

命令：`env/python/python.exe -m pytest tests/test_prompt_rules.py tests/test_artist_table.py tests/test_prompts.py tests/test_client.py tests/test_torii_caption.py tests/test_caption_text.py -o addopts= -q`。

最终 **152 passed**。另检查了中英 × nl/both × 普通/密集重试的 8 组提示词、静态预设与内置提示词一致性、Python语法及 diff 空白。既有测试采用项目嵌入式 Python；图片实测使用 `H:/lulynx-trainer/backend/env/python-flashattention/python.exe` 调用本地 llama.cpp HTTP 服务。

画师检查使用实际 `TaggerClient` 的字段解析和注入方法：Qwen英文、Qwen中文、Torii英文 3 组，均确认开关关闭时不注入、来源缺失时不注入、开启时原名保留且只出现一次、重复调用不重复添加。该检查不代表模型不会另行幻觉一个签名或作者名。

## 图片实测与限制

4070 Ti SUPER，Qwen3-VL-8B-abliterated Q6_K 与 ToriiGate-0.5 Q6_K + FP16 mmproj，llama.cpp；模型依次运行，temperature=0、seed=42。使用两张程序绘制的受控插图（同一人物，有/无八处对白气泡）和仓库既有英文招牌图，共 3 个独立输入。不是 Anime-Fox 全库或人工真值集，不能据此报误判率。

最终版本共 **8 次真实生成**：Qwen英文对白、中文对白、密集重试、短招牌、无字图；Torii对白、短招牌、无字图。全部输出可解析、非空，无输出长度截断异常；原文见 `results.json`。

- **Qwen英文**：普通及密集重试仍抄对白，尚未实现可靠忽略。中文虽未逐字引用，但仍概括对话含义。
- **Torii**：对照图由长篇逐条对白缩到重复台词，但仍违反忽略要求；还把该受控插图解释为某影视作品的梗。招牌图中编造了实际不存在的画师签名。
- **短招牌**：两模型都保留了 KEEP OUT / DO NOT ENTER。Torii上述签名幻觉意味着该输出仍需审核。
- **无字图**：最终版本两模型均未编造图中文字。

额外测试了更强的系统禁令、文末要求和简短提示词，效果不稳定；一个简短Torii版本还输出了非严格JSON。未将这些实验变体投入生产。`variants.json`、`results-round*.json` 保留实验原文，不能混作最终版本验收结果。不同请求顺序下 Qwen greedy 输出仍有细节差异，不作逐 token 确定性声明。

需要修正此前结论：提示词文本存在某条规则，不等于模型实际遵循。当前可确认的是规则和代码链路已修正；“稳定无视密集文字”和“不会猜画师”尚未通过效果验收。下一步若要强制忽略，需另行设计文字区域处理或输出结构筛选，不能只继续堆提示词。

测试服务已退出；临时脚本、运行日志归档到 `H:/lulynx-trainer/.delete/tagger-prompt-tests-20261120/backend/smoke/tagger_prompt_20261120`，保留结果和受控输入于本目录。没有提交或推送。
