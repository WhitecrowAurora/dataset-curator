# V0.3 画师注入改造：队长审计台账（2026-11-11）

口径来源：规范 `annotation_spec_v0_3.md`（v0.2 为只读基线，md5 `9204c0ab9e2c0921b6735b933aa2eb5f`）。
执行方式：AgentTeams `tagger-artist-v03b`，4 成员 / 7 任务；成员交付后由队长独立复核（不采信汇报数字）。

## 一、任务级复核结果

| 任务 | 产物 | 队长复核手段 | 结论 |
|---|---|---|---|
| t1 规范 v0.3 | `devtools/annotation_spec_v0_3.md` | md5 基线比对、禁用词 grep、模板命中计数、§8 项数 | 通过 |
| t2 占位符 | `tagger/artist_phrase.py` | 自造 5 类文本、长名优先实证、打坏测试 | 通过 |
| t3 元数据 | `tagger/metadata_strip.py` | 自造 EXIF/XMP/COM/PNG-tEXt 样本、像素差、CLI 端到端哈希 | 通过 |
| t4 模板注入 | `tagger/artist_templates.py`（+ artist/client 接线） | 自喂 7 组参数、>3 截断、同名去重、含空格名原样 | 通过 |
| t5 校验器 | `tagger/validate_artist.py` | 自造 7 类样本、三种退出码、`--manifest-out` 内容核对 | 有条件通过（见 R1） |

## 二、审计发现

### R1（已定并已修，2026-11-11 复核通过）告警级别过低
`--manifest-out` 把**裸名前置**行（`@wlop, a girl sits.`）放进通过清单——它只触发 `W2` 告警。
- 裸名前置是规范 §5 硬禁止，必须是 error；
- `W1` 占位符残留 = 名字没解析出来，坏行；
- `W4` `@@` 会让下游按 `@@name` 解析，坏行。
裁定：`W1/W2/W4 → error`，`W3`（无画师声明的风格词）保持 warn，`--strict` 保留。

复核（队长自造 5 行）：裸名前置 → `[error] W2`、占位符残留 → `[error] A4 + W1`、`@@wlop` → `[error] W4`、无画师风格词 → 仅 `[warn] W3`。`--manifest-out` 只保留 `w3` 与 `clean` 两行，其余全部排除；`--strict` 退出码 1。
另验字段优先级：`artists=['real name']` + 陈旧 `artist='stale name'` 时，正文写 `@stale name` 判 `A2`（声明：real name），即注入形态洗完也不放过。selftest 由 30 项增至 45 项。

### R2（队长已修）封包侧重复注入
`pipeline.py` 在封包时仍 `prepend_tag(caption, "@名")`，与打标侧模板句叠加 → 同一句出现两次画师名。
修复：
1. `client._nl_payload` 写入 `artist_injected` ∈ {`template`,`bare`,``}；
2. `pipeline.py` 新增模块级 `_caption_already_has_artist()`：有标记按标记判，老产物看开头是否为
   `使用 @` / `In the style of @`；命中则不再补裸名（`tag_text` 的裸 `@` 前缀保留，tag 串本就该是列表形态）。
证据：单测 6 例全过；实跑 1 张得 `artist_injected='template'`、caption 开头
`In the style of @test_artist_a, A young woman with long, glo…`；`pytest -k "pipeline or pack or client or artist"` 全绿。

### R3（队长已修）merge 缺权威画师声明
`merge.build_rows` 只写 `artist` / `artist_tag`，校验器的 A2/A3 基准要靠推导。
修复：输出 `artists`（逐字原名列表，老产物用 `artist` 兜底）与 `artist_tags`（`@` 形态列表）。
证据：重合并 4 行得 `['test_artist_a']` / `['@test_artist_a']`，无画师行为 `[]`；校验器复跑 A2/A3 基准生效。

### R4（评审 t10 发现，已复现，待修 t12）JFXX 缩略图绕过元数据清除
APP0 被无条件保留，而 APP0 的 JFXX 载荷可内嵌一张带 EXIF 的完整缩略 JPEG。
队长独立复现：主图 + `APP0/JFXX(内含 EXIF 0x010F='PROBE-JFXX-EXIF-T10' 的 8x8 缩略图)`
→ 清除后产物**仍含该标记**，且 `stripped_keys` 报空。
根因是「残留复检复用清除解析器」——解析器盲区同时骗过清除与自检。
改法：APP0 仅在 `JFIF\x00` 开头时保留；另加与解析器无关的字节级 fail-closed 断言。
回归夹具：`devtools/probe_jfxx.py`（队长落盘，t12 的 verify 直接使用）。

**t12 已修并复核通过**：
- APP0 收窄到 `JFIF\x00` 才保留，`JFXX` 整段丢弃；未知 APP0 载荷 fail-closed 丢弃并记 key。
- 新增与解析器无关的字节级兜底 `find_dirty_markers()`（`Exif\0\0` / `xap` / `Photoshop 3.0` / `JFXX`），
  写盘前命中即 raise —— 解析器盲区不再同时骗过清除与自检。
- `stripped_keys` 同类段去重（3 条 COM → 1 条）。
- 队长复核：三条 verify 全绿；我自己另造一份 JFXX 样本（标记 `MY-OWN-MARK-991`）→ 标记与 `JFXX` 均消失、
  像素一致；`probe_jfxx.py` 7/7 PASS。
- **误报率实测**（补上作者未测的那一项）：27 张真实分片图 → 字节级误报 0、exit 0；
  另对 **6253 张真实图**跑全量批处理：**成功 6253/6253，字节级失败 0，失败 0**，
  耗时约 10 分钟（容器级重写，不解码）；总体积 1 017 150 108 → 1 017 134 332 字节（只掉元数据段）。
  其中 **464 张真实图确实带 COM 段**（`jpeg:COM` × 464）——说明清除工作不是空转，
  真实数据里就在携带元数据；同时证明字节级 fail-closed 在真实压缩数据上**零误报**，
  作者提出的"可能误报"取舍在真实分布下不成立。

### R5（评审 t8 发现，项目级，待修 t13+t14）占位符层整层没接线
全仓库无 `protect/restore/leaked_names` 调用点，`client._ref_block` 渲染进 prompt 的仍是
**画师真名原文**。即规范 §7.3「送模型前替换」目前只存在于模块里，运行链路并未生效。
- t14（接线）：prompt 侧 protect、输出侧 restore + `leaked_names` 断言、重试复用同一 mapping。
- t13（同文件缺陷）：f2 无词边界（`a ring of light` + `rin` → 假名 `@ring`）、f3 `@名` 入参不归一、
  f4 `@@__ARTIST_0__` 回填仍双 @、f5 `PLACEHOLDER_RE` 死代码。
注：t6 pilot 在接线前启动，其样本的 prompt 仍含真名；这不影响「三套句式对比」的结论，
但**结论不能用来证明占位符已生效**。

### R6（评审 t9 发现，待修 t16）英文模板配 CJK 画师名产出全角逗号
`insert()` 用「句式里有没有 CJK 字符」选分隔符，而不是按模板语言。真实数据里有 `竹うら` 这类
CJK 画师名，英文模板会长出 `In the style of @竹うら，a girl stands`。t5 不检查标点，所以不会被拦。
另有 f2 `render("wlop", …)` 按字符迭代、f3 不归并 `@@`。

### R7（评审 t11 发现，待修 t15）校验器诊断码与覆盖缺口
F1 含空格名改大小写报 A2（应 A3），排障方向被带偏；F2 声明了画师但 tags_final 无对应条目时整行通过
→ 新增 A5（默认 warn，`--strict` 升 error），先量误报率再决定是否提硬。

### R8（已修，t13 复核通过）占位符层四项缺陷
| 发现 | 修法 | 队长实测 |
|---|---|---|
| f2 无词边界 | 交替分支两侧加 `(?<![A-Za-z0-9_]) … (?![A-Za-z0-9_])` | `a ring of light`+`rin` 不动；`ARTISTS`/`ARTIST`、`img0012`/`0012`、`wlop_fan`/`wlop` 均不命中；`@`、`( )`、行首行尾照常命中 |
| f3 `@名` 入参 | 入参统一 `lstrip("@")` | `protect("by wlop",["@wlop"])` 正确替换并返回 mapping |
| f4 `@@` 不收敛 | 回填正则 `(?:@+[ \t]?)?__ARTIST_(\d)__` | `@@`/`@@@`/`@@ ` 全收敛为单个 `@`，未知下标原样 |
| f5 死代码 | 删除 `PLACEHOLDER_RE` + `hasattr` 断言防回潮 | 已无该符号 |

边界口径（队长裁定）：**词边界刻意不含 CJK/假名**——中日正文无词间空格，把 CJK 当词字符会让
`画师竹うら的作品` 整段漏名，漏名比多替换危险。实测该句仍正确命中。
`leaked_names` 采用与 `protect` 同一套边界（词内不算泄漏），不回退纯子串：断言目的是抓
「模型把真名写进正文」，自然文本里边界匹配才是正确语义。selftest 30 → 53 项，原 30 项未动。

### R9（队长修）参考 tag 段不再带画师条目
占位符接线后（t14），`_ref_block` 里的画师 tag 会以 `__ARTIST_n__` 令牌进 prompt：
模型既拿不到「这是某画师」的语义（正是设计意图），又可能把令牌当普通 tag 抄进正文，
回填后正文里多出一次画师名，与开头模板句撞车。
修复：新增 `TaggerClient._artist_ref_filter()`，在 `_prompt_for_task` 里把画师条目从参考段剔除。
比较用归一形（去 `@`、空格↔下划线互换、忽略大小写），`sakimi_chan` 与 `sakimi chan` 视为同一条；
无画师时原样返回。实测 5 例剔除判定正确；真实运行 2 张：`artist_injected=template`、`flags=[]`、无占位符残留；
夹具 41 项 + 相关单测 155 项全过。
（注：第一次补丁脚本把中文塞进 bytes 字面量导致 SyntaxError，文件未被改动但 `py_compile` 仍通过——
再次印证「改完必须验证内容真的变了」。）

### R10（已修，t17 复核通过）A1 与 A5 同源重复报
裸名进 tags_final 时 `A1`（缺 @）与 `A5`（无 @ 名条目）同时报，同一根因计两次。
修法：A5 改为归一化存在性判断，名字以任何写法出现过就不报 A5；裸名归 A1、写错形归 A3。
队长实测：裸名 → 仅 A1；完全缺失 → 仅 A5（默认 warn / `--strict` error）；@名存在 → 无告警；
`WLOP` → A1 且提示"与原始拼写不逐字相等"。selftest 69 → 85 项；`manifest` 34.4 万行仍零误报。

### R11（已修，t16 复核通过）分隔符按字符集判断 → 英文模板配 CJK 名出全角逗号
`insert()` 原来用 `_has_cjk(句式)` 选分隔符，还只看 `\u4e00-\u9fff`（**不含假名**）。后果：
- 含汉字名 `瀬世ナギ` → `In the style of @瀬世ナギ，…`（全角，明确错字）
- 纯假名名 `しょくぱん` → 反而半角（巧合，不是按语言判断）

**生产侧证据**（pilot 在 pre-t16 那轮实测，不是合成构造）：生产配置 68 条英文样本里 **28 条**命中全角逗号（41%），
英文模板样本 68 条里 **56 条**命中。说明这不是测试脚本问题，是管线本身的缺陷。
修法：新增 `_lang_of_sentence()` 从画师句开头标记推断模板语言，`insert` 的优先级是「显式 lang → 开头标记 → 兜底半角」；
`_has_cjk` 删除。**明确不扩 CJK 区间**——扩区间只会让更多名字被误判成全角。
队长复测：`瀬世ナギ`/`しょくぱん`/`ふけまち`/`竹うら` 英文模板全为半角；`wlop`/`しょくぱん` 中文模板全角；
`render("wlop",…)` 不按字符迭代；`@@wlop`/`@@@wlop`/`@ @wlop` 均归并为 `@wlop`。selftest 39 → 54，夹具 41/41。

### R9 后续：t14 的 prompt 侧保护变为纵深防御
`_artist_ref_filter`（R9）生效后，正常路径下 prompt 里已不含画师名，因此 t14 的 protect 退化为第二道防线
（映射为空、回填不触发）；模型若自己编 `__ARTIST_0__`，按未知下标保留并记 `artist_placeholder_unknown`。
**t14 原验收口径"prompt 里应出现占位符"因此不再成立**，正确预期改为「prompt 里既无原名、也无画师 tag」。
这是前提变化，不是回归；已按新口径记入台账，不再重开 t14。

### R12（t6 pilot 结论）句式口径定案
- **推荐**：模板句 + 语言路由（中文正文配中文模板、英文正文配英文模板）；`nl_style: bare` 只作回滚开关。
- 队长独立复算（不从报告抄数）：136 条真实样本（31/31/62 有效）+ 68 条控制格（全部 `valid_sample=0`、指标空）；
  `s1` 三套均 100%；**`s2` 是唯一硬差异：模板 100% vs 裸名 0%**；`s3` ≈100%；
  seam 分布：`template_zh` 全角 ×31、`template_en` 半角 ×31、`bare` 半角 ×62 → t16 生效。
- 报告结构完整（九章 + 附录 A 缺陷证据 / B 控制格 / C 正文退化 / D CSV 说明），无来源与人名混入。

### R13（t6 附带发现，建议单独排期，不在 V0.3 范围）
1. **中文正文光影/视角覆盖偏低**（32%–44% vs 英文 65%–71%）→ 中文 prompt 可补「写明光线方向与机位」。
2. **图内文字逐字保真最弱**（78%–87%，16 条零分，例：`釧路→剣路`、`しばらく→しぼらく`、`@chiyabo→@kijayao`）
   → 收紧「不确定写不可辨」，并考虑把引号内容纳入校验。
3. **`artist_name_leaked` 在版权页图上误报 2 次**（画师名本来就印在图内文字里）
   → 校验/告警口径应把「引号内或非开头位置」视为图内文字，不可一律当硬错误。
4. **名字失真风险在提取口径而非注入层**：旧清洗规则对 17 个真名确定性复算只有 `長瀬 徹 → 長瀬_徹`（1/17）；
   本轮全走查表口径故未触发。`verbatim_names` 默认 true 已挡住这条。

## 三、交叉评审结果

| 评审 | 被评 | 结论 | 关键产出 |
|---|---|---|---|
| t10 | t3 元数据清除 | pass（1 条 low） | 88 项自造断言；发现 JFXX 盲区（→R4） |
| t8 | t2 占位符层 | pass（5 条 findings） | 变异体证明 selftest 有牙；发现整层未接线（→R5） |
| t9 | t4 模板/接线 | pass（3 条 findings） | 63 项自写探针 + 两变异体验证 selftest 有牙；发现英文模板配 CJK 名产出全角逗号（→R6） |
| t11 | t5 校验器 | pass（2 条 low） | 34 项自写探针；发现 A2/A3 诊断码错位、A1 不查"声明画师是否还在 tag 串"（→R7） |

## 四、队长自身的复核方法错误（留痕，避免重犯）

1. **打坏测试打错位置**：改了一个不存在的字符串（等于没改），得到"退出 0、0 FAIL"的假通过，差点误判成员的 selftest 是空壳。第二次选对位置（回填的 `@`）才复现真结果（10 项 FAIL）。
   → 规则：打坏后先确认文件内容真变了（`grep`/`diff`）再看退出码。
2. **退出码测量被管道吃掉**：`cmd | tail -2` 后读 `$?` 拿到的是 `tail` 的退出码。重测后确认原结论（配置错 2 / 部分失败 1 / 正常 0）。
3. **补丁锚点插进 `try` 块**：`pipeline.py` 首版把函数定义插在 `try:` 体内导致 `SyntaxError`，已回滚重做。
   → 规则：改前备份（`tagger-patch-backup-20261111/r5/`）、插完立刻 `py_compile`。

## 五、工具/流程缺陷

- **契约 `inScope` 必须用仓库相对路径**。写绝对路径时插件 `pathMatchesScope` 对以 `/` 开头的 pattern 恒返 false，任何 `changedPath` 都落成 `undeclared`，任务无法登记完成。四份契约已改（t2/t3/t4/t5）。

## 五之二、全量单测基线核对（2026-11-11 02:5x）

`pytest tests -q` 跑到 100%，**失败 5 个，与改动前基线完全同一批**，无新增失败：

| 失败用例 | 归属 |
|---|---|
| `test_conflicts.py::test_merge_cli_reads_decisions` | 上游既有缺陷（merge CLI 读 decisions 的断言） |
| `test_deps.py::test_装不了的包不占按钮上的计数` | 本机环境（依赖探测） |
| `test_quality_l2.py::test_allowed_local_media_path_reaches_the_command` | 本机环境（显存护栏） |
| `test_web_models.py::test_defaults_say_which_weights_are_missing` | 本机环境（权重未装） |
| `test_web_vllm.py::test_plan_preview_does_not_start_service` | 本机环境（vLLM 未起） |

结论：V0.3 的 17 处改动（含 4 个新模块、client/pipeline/merge 接线）**没有引入任何测试回归**；
夹具三套 55 项、各模块 selftest（30→53 / 30→54 / 45→85 / metadata 全套）全绿。

## 六、待办

- [x] R1 提级执行后复测 `--manifest-out`（裸名前置行已被排除）
- [ ] `metadata_strip` 未注册 `tagger metadata` 子命令 → 流水线不会自动调用，需队长补接线
- [ ] t12 F1 修复后跑 `devtools/probe_jfxx.py` 复核
- [ ] t13/t14 占位符接线后复跑夹具与实跑
- [ ] t6 pilot 报告复核（三套句式并列可比、≥30 条/套）
- [ ] t7 提示词固化复核（`prompt_rules` 能读到新段落、T4 校验器对 3 张样本全过）
- [ ] 归档：`fox-suite-20261111` 同步 + v4 zip + `WAKE-UP.md`
