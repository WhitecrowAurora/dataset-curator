# 正文 token 计数

使用 Qwen/Qwen3.5-0.8B，固定版本 `2fc06364715b967f1860aea9cf38778875588b17`。
`assets/tokenizer.json` 与客户端训练 tokenizer 文件一致；SHA256 固定在 `tokenizer.py`，来源记录见 `assets/source.json`。

安装：`python -m pip install -r text_encoding/requirements.txt`。
只编码去除首尾空白后的 TAG 或 NL 正文，不计算图片、JSON 元数据、特殊 token 或聊天模板；不截断。
计数缓存有数量上限，不加载模型，不隐式联网。文件或依赖缺失时仍可使用时间和字符规则。

批量策略 `most_tokens` / `fewest_tokens` 选择唯一最大值 / 最小值；并列保持待处理，原始候选保留。
token 数衡量长度，不代表描述准确性。每次批量决议保留 tokenizer 版本和文件哈希。
