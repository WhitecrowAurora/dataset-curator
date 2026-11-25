# 发色复核单图原型 · 2026-11-19

已集成到现有「冲突与决策」页面。入口是 `/#/conflicts`。

上传图片，可填已有 PixAI 发色；留空实际运行 PixAI。可用原图像素裁框指定角色头部。
MobileNet 与 PixAI 一致即接受；否则运行 CAFormer；仍有分歧才请求现有 Qwen3-VL 服务。
Qwen 与已有模型的首选具体颜色一致时接受；多人、双色、不明确或服务不可用时转人工。
Qwen 独立看图，不接收前三候选，不输出概率。

人工卡片展示原图、可选裁图、各模型结果、CAFormer 前三颜色及模型概率、色块与头发示意。
支持点击候选、选择其他具体颜色、双色、撤销。浅色组不能直接作为具体颜色裁决。
人工选择存于 `work/conflicts/hair/reviews.sqlite3`。这是单图原型，未接入批量产物回写、
自动头部检测或头发像素分割。不会覆盖已有产物冲突扫描结果。

本机默认 Qwen 服务地址 `http://127.0.0.1:8000`、服务模型名 `tagger`。
先在现有模型管理界面启动服务；原型自身不启动另一套推理服务。
未加载新路由的桌面后台需重启 Tagger 后使用。

## 实际验证

- 官方 MobileNet、CAFormer 的 ONNX 输出确为 softmax 概率，输入按官方 RGB / 双线性缩放 / 均值与标准差 0.5 处理。
- 3 张真实动漫图、4 次接口运行：金发肖像、棕发肖像、多人图、多人图中的蓝发角色裁图。
- 金发肖像：PixAI 为金色，两个专用分类器都判棕色；Qwen 判金色，级联接受金色。
- 棕发肖像：PixAI 与 MobileNet 一致判棕色，未执行后续模型。
- 多人图使用显式已有结果 `black` 覆盖末级分支；真实 MobileNet、CAFormer、Qwen 都执行。
  这是主动触发分支，不声称该黑色结果来自 PixAI 推理。多人图进入人工复核。
- 裁出蓝发角色后，两个分类器与 Qwen 都判蓝色；Qwen 仍标有不明确/多色因素，按规则留人工。
- 11 个定向规则分支通过，包括提前结束、Qwen 匹配三个来源、分歧、服务失败、空发色、歧义及浅色。
- 人工选择、双色、撤销、非法浅色拒绝、无效图片、404、裁图尺寸、存储重新打开通过。
- 人工决定在实际 Web 后端重启后仍保留；中断记录会显示失败而不永久卡在运行中。
- 浏览器实际上传与推理、三个示意、选择/保存/刷新/撤销、390px 无横向溢出通过，无页面脚本错误。
- `npm run build` 通过；现有 `tests/test_conflicts.py` 与 `tests/test_web.py` 共 50 项通过。
  构建保留现有单 bundle 大小警告，测试保留 Starlette/httpx 弃用警告。

这批小样本验证的是功能闭环，不是相近发色准确率验收。已经观察到专用分类器高概率判错，
因此必须继续用有人工真值的相近色样本验证自动接受规则。

结果详情见 [results.json](results.json)，界面验证见 [browser-results.json](browser-results.json)。
临时测试脚本、素材与浏览器依赖按要求从 `backend/smoke` 移至 `.delete`，保留结果与截图。

## 模型来源

- 发色：[deepghs/anime_ch_hair_color](https://huggingface.co/deepghs/anime_ch_hair_color)，MIT。
- 固定 revision：`c9af6e013b1984a7c06bc4a951f53c9fed98b956`。
- MobileNet ONNX SHA256：`75958879e4c317e2a8d5b66b82e8a34069c16b5ec4f779e70013e3328acf80a1`。
- CAFormer ONNX SHA256：`203294f7ed1c0c3cfb2cdb6d165aed5e5370165ae6d34bfb622afc5f3da821d2`。
- PixAI：本机缓存 `pixai-labs/pixai-tagger-v1.0`。
- Qwen：本机 `qwen3vl-8b-abliterated-Q6_K.gguf` 与 F16 mmproj，经已有 llama.cpp 运行时执行。
- 图片：[deepghs/imgutils](https://github.com/deepghs/imgutils) 的公开测试图片。
