# 图像生成训练数据：抽样与重标注规范 V0.3

> 日期：2026-11-10。本文只规定 **数据集抽样配额、原标注处理、NL（自然语言描述）及 Tag 生成规则**。不涉及架构、训练超参、存储压缩或完整下载程序。文档不使用任何项目代号。
>
> 画师注入是硬规则：**位置固定、中英各一套模板、画师名绝对原样**（见 §5、§7.3、§8）。
>
> 所有配额是**筛选/去重前的计划上限**；实际数量取决于可用图像、许可证审核、损坏、重复与质量过滤。数据集版本和来源路径必须固定至 commit/revision 并保存在 manifest。不同来源单独建目录与索引。

## 1. 已选数据源与抽样清单

| 类别 | 数据集 | 计划记录数 | 抽样规则 | 链接 |
|---|---|---:|---|---|
| 通用 T2I | MONET | **30,000,000** | **分原始来源定额抽样**，不含其内部 synthetic | https://huggingface.co/datasets/jasperai/monet |
| 通用 T2I | PD3M | ~3,301,000 | 全量候选；下载与质量/去重过滤后保留 | https://huggingface.co/datasets/Spawning/PD3M |
| 通用 T2I | Fine-T2I | **1,000,000** | **按官方五个目录定额抽样** | https://huggingface.co/datasets/ma-xu/fine-t2i |
| Anime | Danbooru | ~10,000,000 | 既定来源全量候选，具体版本待锁定；跨库去重 | https://huggingface.co/datasets?search=danbooru |
| Anime | Pixiv-2.6M | 2,639,665 | 全量候选，保留原 Tag 与艺术家元数据；与 Danbooru 去重 | https://huggingface.co/datasets/Amber-River/Pixiv-2.6M |
| Anime | 内部精选 | 300,000 | 全量候选，保留已有 Tag / 来源 / 作者标注 | 内部 |
| 编辑 | UnicEdit-10M | **2,025,000** | 按**当前实际公开**记录全量，不按名称中的 10M | https://huggingface.co/datasets/xiaotanhua/UnicEdit-10M |
| 编辑 | Pico-Banana-400K | ~385,000（名义约 400,000） | 先拆 SFT / Preference / 多轮；仅成功编辑直接用于 SFT | https://github.com/apple-aiml-research/pico-banana-400k |
| 多图参考 | MICo-150K | 148,637 | 全量；保留多图输入顺序和分解/重组任务结构 | https://huggingface.co/datasets/kr-cen/MICo-150K |
| 编辑 | NHR-Edit Part 1 | 358,463 | 全量；按 original_seed / 源图分组去重和划分验证集 | https://huggingface.co/datasets/iitolstykh/NHR-Edit |
| 普通合成 T2I（可选） | GPT Image 2 ZQ10K 2048 | 10,000 | 访问批准后全量；**不是编辑配对** | https://huggingface.co/datasets/Kontext-Style/gpt-image-2-zq10k-2048 |
| 风格参考 | MegaStyle-1.4M **v1.0** | 1,389,424 | 全量；**固定 v1.0**，勿误取 v2.0/8M；保留风格分组 | https://huggingface.co/datasets/tencent/MegaStyle-1.4M/tree/v1.0 |
| 风格参考 | OmniStyle-150K | ~150,000 组三元组 | 全量；按内容图、风格图和结果图三者关系保留 | https://huggingface.co/datasets/StyleXX/OmniStyle-150k |
| 内部参考 | GPT Image 2+ Anime 角色 / 画风 / 编辑 | ~30,000 组 | 全量；独立记录任务类型及参考图与目标图关系 | 内部 |

备注：一条编辑/参考记录可能含 2–5 张甚至更多图像；表中为**任务记录数**，不得误认为独立图像数。Pico 的 preference 失败样本不直接计为成功 SFT 监督；多轮任务应按轨迹保存、可在训练时展开为多个转移步骤。

## 2. MONET — 精确抽取 30,000,000 条

官方 v1.2.0 提供来源分目录，元数据字段包括 `source`、`id`、`aesthetic_jasperai`、`aesthetic_laion-improved`、`width`、`height`、`hash_sha256`、`hash_perceptual`、`sscd_cluster_id`、原始 Caption 和多个 VLM Caption。Parquet 主要用于筛选；真正的高分辨率 JPEG 需从 WebDataset TAR 取出。不要将 Parquet 的小缩略图当训练图，也不要保存无关 SANA VAE latent。

| 来源目录 / `source` | 官方最终约有 | 本计划配额 | 说明 |
|---|---:|---:|---|
| `laion` | 46,600,000 | **10,300,000** | 大规模多样性，严格审核噪声和相似图 |
| `coyo` | 19,100,000 | **6,600,000** | 独立来源配额，避免与 LAION 相同图像 |
| `commoncatalog-cc-by` | 11,200,000 | **5,000,000** | 真实摄影；必须考虑与其他来源重复 |
| `megalith10m` | 8,000,000 | **4,300,000** | Flickr 来源，兼顾场景/人物长尾 |
| `cc12m` | 6,400,000 | **3,787,200** | 场景/商品/物体补充；注意含文字图与噪声 |
| `diffusion-aesthetic-4k` | 12,800 | **12,800** | 全量候选，仍需质量与重复筛选 |
| `synthetic/*` | ~13,800,000 | **0** | 已有 Fine-T2I 合成数据，不从本来源抽样 |
| **合计** | — | **30,000,000** | **配额准确合计** |

- 先对每个来源建立独立候选 manifest，再进行损坏、安全、重复与分辨率检查；依据 **来源 × 清晰度/有效分辨率 × 美学区间 × 宽高比 × 内容类别** 分层，而不是在整个 MONET 按文件顺序或单一美学分数取样。
- **初始可测试策略（需抽样审计后定稿）**：合格候选中 60% 按综合质量加权，25% 为稀有题材/长尾保底，15% 在合格集合中均匀随机；所有分支都必须通过最低质量过滤。极高美学并不等于一定有高价值视觉知识。
- 同一 `sscd_cluster_id` / 强近重复簇原则上不重复入选；跨来源发现重复时只保留质量更高且来源可追踪的版本。超额/缺额按来源预先列好候补，缺额可跨来源补齐，但要记录 `quota_original` 与 `quota_final`。
- 单一来源分文件夹，例如 `sampled/monet/laion/`、`sampled/monet/coyo/` 等；manifest 保留原始 `id`、`source`、revision、TAR 分片、文件哈希、质量分数和处理状态。

## 3. Fine-T2I — 精确抽取 1,000,000 条

官方数据为 WebDataset：每个样本含 `jpg`、`txt`、`json`，五个目录必须分别扫描、分别计数。`txt` 对合成图主要是**生成 Prompt**，不是普通自动 Caption；不得当作质量差的旧 Caption 直接丢弃。

| 官方目录 | 官方条数 | 抽样配额 | 抽样策略 |
|---|---:|---:|---|
| `curated` | 168,424 | **168,424** | 高质量真实图全量候选；抽查来源与实际清晰度 |
| `synthetic_enhanced_prompt_random_resolution` | 1,615,592 | **230,000** | 覆盖详细 Prompt、多种宽高比 |
| `synthetic_enhanced_prompt_square_resolution` | 1,538,253 | **190,000** | 详细 Prompt、正方形内容 |
| `synthetic_original_prompt_random_resolution` | 1,686,498 | **230,000** | 原始短 Prompt、多宽高比 |
| `synthetic_original_prompt_square_resolution` | 1,305,350 | **181,576** | 原始 Prompt、正方形 |
| **合计** | **6,314,117** | **1,000,000** | **配额准确合计** |

- 四个合成目录分别按 Prompt 分类、题材、视觉风格、有效分辨率与相似度去重后抽取；注意 `original` 和 `enhanced` 之间可能有内容或概念重叠，必须检查跨目录近重复。
- 采用稳定随机哈希 / 固定种子，**先流式读取目录与每个 TAR 的样本索引，再按选中 ID 提取 JPG/TXT/JSON**；不要一次性下载并解压 2.32 TB 后再挑选。
- `curated` 全量是抽样配额，不是质量豁免；发现无法下载、重复或严重失真的图片后可剔除并记录实际数量。

## 4. 重打标注：哪些旧字段退出最终训练？

**定义：**“删除旧标注”指从**最终训练输入字段**中剔除旧 Caption/Tag，并使用本地 Qwen-VL-8B 新结果替代；原始发布文件、来源许可证、文件哈希、任务关系等保留在只读原始区和内部审计 manifest。**不要直接破坏唯一的原始拷贝。**

| 数据来源 | 旧 Caption / Tag 处理 | 必做的新标注 | 不得覆盖的字段 |
|---|---|---|---|
| **MONET** | `caption_original`/Florence/ShareGPT4V/InternVL/Gemini **不直接进入默认训练 Caption**；先作为审核对照。有抽样证据表明优于新标注的部分可例外采纳。 | **新 Tag + EN/CN NL** | 来源、哈希、许可、原评分、多版本 Caption 的审计副本 |
| **PD3M** | 旧 `caption` 从默认训练 NL 中退出 | **新 Tag + EN/CN NL** | `id`、`url`、`source`、`license`、MD5 |
| **Fine-T2I curated** | 原有 Qwen2.5-VL 重描述可保留作优质候选；经抽审决定与新 NL 谁进入默认训练 | **新 Tag + EN/CN NL（至少中文部分必须生成）** | 作者提供 Caption、样本 JSON、来源 |
| **Fine-T2I synthetic** | **保留原始生成 Prompt**（最有价值的条件语义）；新 NL 不得覆盖原 Prompt | **新 Tag + EN/CN NL**，可作为辅助/交替训练描述 | `txt` 生成 Prompt、`json`、任务/比例 |
| **GPT Image 2 ZQ10K（可选）** | 保留生成 Prompt 与亮度校正元数据；不用原 Prompt 充当图像所见事实的绝对真值 | **新 Tag + EN/CN NL** | 原 Prompt、生成器与光度修正记录 |
| **Danbooru** | **原始 Tag 全部保留**，包括 general、artist、character、copyright、meta 等（若有分类） | **新增 EN/CN NL**；必要时新增辅助 Tag，但不替换原 Tag | 原 Tag、post ID、artist 等来源元数据 |
| **Pixiv-2.6M** | **保留 WD Tagger 原 Tag**，不应视为人工核实的作者信息 | **新增 EN/CN NL**，可以新增校验后的 Tag | Pixiv 来源/作者 ID、`artist-danbooru.json`、美学分数 |
| **内部精选 Anime** | **原 Tag / 人工标注保留** | **新增 EN/CN NL** | 内部来源、作者、角色、版权和已有标签 |
| **UnicEdit** | 原有 `prompt_en`、`prompt_cn`、`edit_task`、`edit_subtask` **保留**，不以单张结果图 Caption 替换编辑指令 | 可选新增输入图/结果图的 Tag + NL（仅辅助） | 源图—指令—结果图关系及双语原指令 |
| **Pico-Banana** | 原始编辑指令保留；成功/失败偏好不得混淆 | 可选补充 EN/CN 描述与图片 Tag | 编辑结果、原图、评分、偏好、多轮顺序 |
| **NHR-Edit Part 1** | 原始 `edit_instruction`、`augmented_instructions` 保留 | 可选补充 Tag/NL（仅辅助） | source/edited、kind、category、original_seed |
| **MICo-150K** | 原始 `instruction`、`separate_prompt`、`editing_type` 保留 | 可选给输入/结果分别补 Tag/NL | 输入图列表**顺序**、多图映射、De&Re 参考关系 |
| **MegaStyle-1.4M v1.0** | `content` 和 `style` 原始文本 **保留** | 目标图新增 EN/CN NL + Tag；可做风格描述准确性校验 | 原始 `id`、style group 和 source content/style |
| **OmniStyle-150K** | 无统一可直接代替的目标 Caption；保留原三元组 | 给**目标图**新增 EN/CN NL + Tag，并按条件图记录内容/风格 | content、style、output 三元组的精确文件关系 |
| **内部 30K 参考样本** | 原始指令、角色、画风、作者与任务类型保留 | 目标图新增 EN/CN NL；角色 / 画风引用必要时额外做 Tag | 每张参考图的身份、角色名、原始 Prompt、任务类型 |

原则：**所有普通 T2I 最终至少拥有一套可用 `tags_final` + `nl_final`；所有编辑/多图/风格任务必须保留其条件结构和原指令**。新生成的 NL 是附加监督，不得让编辑数据退化为独立的 T2I 图文对。

## 5. 统一 EN/CN 标注分配

- 默认建议 **英文 NL 75%、中文 NL 25%**（初始配比，可在审计后调整）。这是**样本级单语言主描述**，不是对每张图都生成两套再重复训练。中文必须覆盖通用摄影、Anime、真实艺术、合成图、风格参考等多种来源，不得只集中于一个小来源。
- **按数据集 × 来源 × 内容类别 × 任务类别分别分层选 25% 中文**，并使用 `hash(source + stable_image_id + seed)` 稳定决定语言；避免某个目录只有英文或只有中文。
- 对本来具有双语原始编辑指令的 **UnicEdit**，保留双语字段，可按相同比例选 `prompt_cn` / `prompt_en` 作为训练指令。其他编辑数据若需要中文指令，只能**另增经过审核的翻译/改写字段**，不得覆盖原始命令或改变操作语义。
- 可以记录多语言文本片段（如图中日语）；但**Caption 的叙述语言**只有任务指定的 EN/CN，不允许把图中文字强行翻译为该语言。
- 无论什么样本类型，NL 都必须描述其图像的大体风格。
- 动漫数据必须在 NL 正文（无论 EN 还是 CN）的总体风格描述中包含“动漫风格” / `anime style`，用于把动漫数据与通用数据区分开。
- **画师名绝对原样（硬规则）。** `@` 后紧跟的画师名必须与随图元数据里的原始 artist tag **逐字相等**：不翻译、不音译、不换语言、不改大小写、不改空格与连字符、不转写假名或罗马字、不补全也不截断。名字是画师绑定键而不是描述文本，改一个字就等于废掉绑定。
- **画师句固定放在 NL 开头，但必须是自然描述句式（硬规则）。** 画师句由 §7.3 的模板分句生成，从正文第一个字符起；**禁止裸名前置**——`sample_artist, ...`、`sample_artist 作品，…`、`sample_artist:`、`@sample_artist, ...` 都不是合法开头。也不得把画师句挪到中段或句尾。
- **图内日期只进 `visible_text`（硬规则）。** 画面中出现的年份、日期或时间戳属于 OCR 事实，只记录到 `visible_text`；**不参与画风年代判断**，不得据此推断作品创作年代或画风年代，也不得据此写“90 年代赛璐璐风格”一类年代风格描述。
- `tags_final` 中由原始 artist tag 来的条目与 NL 中写到的画师名都采用 `@原名` 形态；`@` 是标注形态，不改变名字本身。
- 如果任何处理的图像内包含元数据必须清除。

## 6. 图中文字：最高优先级的语言忠实规则

**无论整条 Caption 选择 English 或中文，图片中真实存在的文字必须以其原始语言、原始书写系统与可辨认的具体字词记录；不得因 NL 语言是 EN/CN 而翻译、音译、意译或改写图片中出现的文字。**

- 英文 NL 遇到中文图中文字：`A storefront sign reads “欢迎光临”.` **不能**写成 `Welcome` 来取代原文。
- 中文 NL 遇到英文图中文字：`海报上写着“KEEP OUT”。` **不能**把实际图中文字改成“禁止入内”。
- 英文或中文 NL 遇到日文图中文字：`The notice reads “立入禁止”.` / `告示牌上写着“立入禁止”。` 保留日文原文。
- 图内混有日语、汉语、英语、韩语、阿拉伯语等多种语言时，逐段保留各自的原文；尽可能保留大小写、数字、标点和字形，必要时另存位置与语言标签。
- **不得臆造 OCR 结果。** 看不清或模型无法确认文字时，在 NL 中写“部分文字无法辨认”/`some text is illegible`，或另存 `text_readable=false`；不能自行补全文字，也不能把“疑似文本”硬判为具体字句。
- 推荐结构字段：`visible_text: [{text_exact, language, script, legibility, bbox_optional}]`。NL 中直接引用 `text_exact`，无论外层叙述是哪种语言。
- 图片中可见文字是图像事实，而 `artist`、角色名、作品来源等是**元数据事实**。不要把它们与 OCR 混为一谈。图内日期按 §5 只作 OCR 记录。

## 7. Anime NL：必须显式利用原始 Tag

对 Danbooru、Pixiv 和内部 Anime：**图像 + 原始 Tag/分类 + 可信艺术家/角色/作品元数据 → 生成 EN/CN NL**。

1. 提供 VLM 的输入包含实际图像及区分类型的 `general_tags` / `character_tags` / `copyright_tags` / `artist_tags` / `meta_tags`（如果来源没有分类，则注明 `unclassified_tags`，不可把它们任意冒充 artist）。
2. NL 必须描述实际可见的主体、动作、服饰、表情、场景、画风、构图和光影；利用 Tags 纠正角色和作品专有名词，但对不符图像的 Tags 不应照单全收。
3. **artist 只能来自可信的原始 `artist` 类型 Tag 或可靠的 Pixiv 作者 ID 映射**，不能凭模型觉得“像某个画师”就声称由该画师绘制。画师句按 §7.3 生成：占位符 + 模板分句，位置固定开头，中英各一套。
4. 角色名、作品名只在有可靠 Tag/来源证据时写入；Anime Tag 可保持 Danbooru 原词形，独立地生成自然语言。
5. 保留原始 Tag 的完整顺序或 ID 与类别，不要用 VLM 生成的新 Tag 覆盖它。实际需要的统一训练 Tag 可以另设 `tags_final`，其合并规则可审计。
6. 如果 Anime 图片内出现文字（对话框、招牌、拟声字、漫画标题），同样执行第 6 节的**原文保留**规则。

### 7.3 画师句：固定开头 + 中英各一套模板

画师句不由模型自由书写，而是“占位符 + 模板”两步：

- 送模型前，把可信画师名一律替换成占位符（`__ARTIST_0__`、`__ARTIST_1__`…）。模型全程看不到原名，因此不会翻译、改形或拼错名字，只负责搬运占位符。
- 生成后，标注流程回填占位符为 `@原名`，并把画师句替换成下列模板生成的分句；占位符不得残留在最终文本里。

**位置：固定开头。** 画师句必须从 NL 正文第一个字符起，前面不得有任何引导语，也不得移到中段或句尾。

**两套模板（语言跟 `caption_language` 路由，EN/CN 都按比例出现，所以两套都必须有）：**

| `caption_language` | 模板 |
|---|---|
| 中文 | `使用 @{a} 的风格，{rest}` |
| 英文 | `In the style of @{a}, {rest}` |

- `{a}` 替换为画师名**原文**；`@` 紧贴名字、逐名前置（`@a`、`@a、@b` / `@a and @b`），不得出现 `@@`，也不得只给第一个名字加 `@`。
- `{rest}` 替换为该图其余自然描述：主体、动作、服饰、表情、场景、构图、光影、图内文字（按 §6 保留原文），以及必要时的总体画风描述。
- 只能使用这两套模板，不得自创句式，也不得改成分号、括号、破折号或裸名形式。

**禁止裸名前置。** 开头必须是完整模板分句，画师名只能出现在分句内部；`sample_artist, ...`、`sample_artist 作品，…`、`sample_artist:`、`@sample_artist, ...` 均不合法。

**没有可信画师就整句省略。** 不存在可信 artist tag 或可靠作者 ID 映射时：不写画师句、不造名、不写“疑似某画师”、不写“风格类似某画师”，也不为凑句式补一个占位名；正文直接从 `{rest}` 的自然描述开始。同一画师在同一句里只写一次。

示例（仅说明输出规则，并非真实样本）：

- 随图元数据：画师名 `sample_artist`（按随图元数据的 artist id 精准匹配），角色名 `character_a`，画面含日文文字 `おやすみ`。
- 中文 NL：`使用 @sample_artist 的风格，画中 character_a 留着蓝色头发坐在室内，画面上的文字是“おやすみ”。`
- 英文 NL：`In the style of @sample_artist, character_a, a blue-haired girl, sits indoors. The visible text reads “おやすみ”.`

## 8. 验收与执行边界

- MONET 抽样分配合计必须为 **30,000,000**；Fine-T2I 抽样分配合计必须为 **1,000,000**；质量过滤后最终有效数量单独输出 `valid_count`，不能悄悄用不达标样本补额。
- 所有普通 T2I 记录必须有 `nl_final`、`tags_final`、`caption_language`、`visible_text`（可为空）、`annotation_model`、`annotation_version`、`source_revision`。
- 对 Anime，必须有 `tags_original`；缺失原标签则记为缺失，不能伪装成已有原标注。
- 对编辑、参考、风格任务，必须有任务条件关系与原始 instruction 字段；条件图到目标图的关联需经过验证。
- **画师注入校验项**（逐行检查，任一条不过该行不得入最终 manifest）：
  1. `tags_final` 中的 artist tag 必须带 `@` 前缀；
  2. NL 正文里出现的每个 `@{name}` 都必须能在该行的 `artist_tags` 中找到；
  3. `@{name}` 与原始 tag **逐字相等**：大小写、空格、下划线、假名一律不许变，不翻译、不音译、不改形；
  4. NL 正文出现“画风/风格/`style`”一类风格词却没有任何 `@` → 告警（模板被吃掉或画师句未注入）；
  5. 画师必须按随图元数据里的 artist id **精准匹配**后再注入，不得按名字做模糊、包含或相似匹配；id 对不上时按“无可信画师”处理（§7.3），不得强行注入。
- 随机抽查 OCR 的跨语言忠实度、Artist 归属正确性、角色名正确性、主要物体误报、数量关系和语义幻觉；建议人工分层抽查每个来源至少数百条，再决定是否大规模标注。
- 图像和原始元数据始终独立于输出标注。先写新标注、校验，再从**最终训练 manifest**排除旧 Caption；不可直接对唯一原始数据集做不可逆删除。

## 官方结构依据

- MONET 来源、字段、分片：https://huggingface.co/datasets/jasperai/monet
- Fine-T2I 五目录数量、WDS `jpg/txt/json`：https://huggingface.co/datasets/ma-xu/fine-t2i
- PD3M metadata schema、S3 图像：https://huggingface.co/datasets/Spawning/PD3M
- Pixiv-2.6M WD Tags、美学分数、艺术家映射：https://huggingface.co/datasets/Amber-River/Pixiv-2.6M
- UnicEdit 双语编辑指令字段：https://huggingface.co/datasets/xiaotanhua/UnicEdit-10M
- Pico-Banana SFT / preference / multi-turn：https://github.com/apple-aiml-research/pico-banana-400k
- MICo 多图结构：https://huggingface.co/datasets/kr-cen/MICo-150K
- NHR-Edit Part 1 原始分组：https://huggingface.co/datasets/iitolstykh/NHR-Edit
- MegaStyle 版本与组结构：https://huggingface.co/datasets/tencent/MegaStyle-1.4M/tree/v1.0
- OmniStyle 三元组命名：https://huggingface.co/datasets/StyleXX/OmniStyle-150k
