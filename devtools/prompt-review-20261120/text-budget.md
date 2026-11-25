# 训练文本长度：384 软目标 / 512 硬上限

2026-11-20。按用户确认的语义实现：尽量不超过 384 token；385–512 保留必要内容，不因超过软目标扣完整度分；超过 512 才裁短。

## 实现

- 普通、密集重试的中英文提示词及 Torii 描述提示词均包含软目标与硬上限，默认预设已同步。自定义整段提示词沿用原有替换约定。
- `TextEncoding.max_length` 默认 512、最多允许 512；降低硬上限时软目标取 `min(384, max_length)`。当前没有保存的覆盖配置，使用默认值。
- `TaggerClient._nl_payload` 在画师注入和标签确定后，用本地 Qwen3.5-0.8B tokenizer 按所选训练模板计数，包含启用的特殊 token、chat template 与 system 文本。超过硬上限优先按句裁短，必要时缩短并复验；标签或模板已占满预算时明确报错。
- 修复新版 Transformers 的 chat template 返回 `BatchEncoding` 时误把字段数量当 token 数量的问题；现在读取 `input_ids`，兼容旧版返回列表。
- 训练文本元数据记录 `target` 和 `over_target`；界面分别显示目标内、超过软目标但可接受、超过硬上限。
- 生成模型回复的 `nl_max_tokens` 和字符上限只记录提示，不扣分。上限提示不再遮蔽实际描述过短的检查；其他质量检查保留。缓存版本更新，避免复用旧的长度评分。
- API 生成预算保持独立，例如 Torii JSON 可以使用 2048 输出 token；512 限制的是最终训练文本。

## 验证

- 项目嵌入式 Python：既有 230 项回归和本轮 20 项边界检查合计 **250 passed，38.51 秒**。
- 训练器 `backend/env/python-flashattention`：相同 20 项边界检查 **20 passed，10.32 秒**；有一个 Starlette/httpx 依赖弃用提示。
- 使用实际本地 tokenizer 验证精确 384、385、511、512、513、700 token：不超过硬上限时原样保留，超限后不超过 512。
- 验证真实 `_nl_payload` 的画师、标签、文本模板与聊天模板合并计数；验证 API 软硬标志、配置拒绝 513、普通和 Torii 提示词、预设一致性、超出软目标不扣分及长回复不能掩盖短描述。
- 前端 `npm run build` 通过，包含 TypeScript 检查和 Vite 构建；仅有已有的大 chunk 提示。构建后仅修改 Python。
- 修改文件 `git diff --check` 通过；没有提交或推送。

临时检查脚本归档至 `H:/lulynx-trainer/.delete/tagger-text-budget-20261120/backend/smoke/tagger_text_budget_20261120`，不留在活动测试目录。

## 适用边界

本轮验证的是当前描述生成产物的确定性长度处理，没有重新运行 GPU 图片生成，不能据此声称模型每次都能主动写到 384 以内，也不能据此声称小字转写或身份幻觉已解决。此前图片实测仍见同目录 README 和 results.json。历史产物、外部导入内容或训练端后续另行追加文本不在本轮自动重写范围；训练模板须与实际训练输入一致。
