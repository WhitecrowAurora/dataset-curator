# 头发分割尝试 · 2026-11-19

实测了三个分割模型，另对动漫旧模型的官方脚本设置做了对照。能生成头发蒙版，但当前没有一个适合直接接入批量自动采用：动漫模型混入背景，真人模型对动漫漏检明显，分割后的发色判断有改善也有恶化。

[打开图像对照报告](index.html)：输入 → 蒙版 → 分割叠加 → 白底头发图，以及 PixAI / MobileNet / CAFormer 的前后颜色与分数。粉色叠加只用于展示；实际分类保留原头发像素，不将叠加色喂给模型。

## 范围与方法

- 15 张原图 + 2 次手工头部裁图，来自先前 MONET 真实包样本与 deepghs/imgutils 公开测试图。覆盖动漫浅发、棕发、绿发、蓝发、多角色、真人金/银灰发、鱼、卧室和玩偶负例。
- 三个模型均使用真实预训练权重，无随机权重测试。Chainer load_npz、SegFormer from_pretrained、BiSeNet load_state_dict 均完整加载；hash 和固定源码/模型 revision 见 `provenance.json`。
- 为比较相同输入，统一按动漫项目的预处理生成 256×256 中央裁图。每种模型自己的后续缩放、标准化按其官方方案执行。蒙版坐标都对应此输入，不能视为原图全幅自动分割。
- 头发类 argmax 蒙版之外置白，按蒙版包围框裁紧。没有人工修蒙版、平滑、阈值调整或按颜色挑像素。严格空蒙版跳过分类；极小非空蒙版仍试跑，以暴露问题。
- 68 次分割（17 输入 × 4 设置），85 个输入/蒙版变体，其中 13 个空蒙版，72 个变体实际运行 PixAI 和两个专用发色分类器，共 216 个模型结果。没有重复跑 Qwen，本轮验证分割与发色输入变化。
- 没有人工像素真值或系统标注的发色真值，不报告 mask IoU、precision、recall 或发色准确率。

## 三个候选

| 模型 | 独立头发类别 | 本次耗时中位 | 主要观察 |
| --- | --- | ---: | --- |
| [pit-ray/Anime-Semantic-Segmentation-GAN](https://github.com/pit-ray/Anime-Semantic-Segmentation-GAN) | hair=2，五类 | 623 ms（CPU eval） | 真实头发可部分分出，但背景、皮肤等混入严重；鱼/卧室也有大片蒙版 |
| [jonathandinu/face-parsing](https://huggingface.co/jonathandinu/face-parsing) · SegFormer B5 | hair=13，十九类 | 74 ms（GPU FP32） | 真人银灰发较完整；多人动漫漏检，蓝发头部仅输出 2 像素 |
| [xinntao/facexlib BiSeNet](https://github.com/xinntao/facexlib) | hair=17，十九类 | 15 ms（GPU FP32） | 比较轻，部分真人/动漫能分；蓝发头部和浅发刘海覆盖不全，特效/衣服也可能混入 |

这些耗时来自控制实验，不含加载、下载，也不能直接外推批量全尺寸吞吐。GPU 使用现有 4070 Ti SUPER；PixAI 为 BF16、batch=2，发色 ONNX 按现有 CPU provider。

动漫项目 README 明确假设 256×256 白背景并记录复杂背景失败。本次大多数素材不符合这个条件，结果说明它不适合当前混合包，不能据此断言其在适配训练分布上没有作用。我们同时运行正常 eval 和作者 `predict.py` 默认 train=True 的设置（约 651 ms/张）；后者也误分背景，没有借修实现掩盖问题。作者脚本中的白背景/眼睛/脸筛选未用于排除失败样本，报告保留原始蒙版输出。

旧 Chainer 在 Python 3.12 / 新 NumPy 上有兼容问题。仅在 `backend/smoke` 内隔离放置 Chainer 7.8.1、NumPy 1.26.4、SciPy 1.12、setuptools 70.3，不改主环境依赖。临时将仅 macOS 使用的 numpy.distutils 导入移入对应分支；源码构造 ResNet101 时关闭额外 Caffe 下载，随后完整加载官方 gen.npz（含 ResNet），没有跳过权重。

## 发色对照中的关键例子

| 样本 | 未分割输入：MobileNet / CAFormer | 蒙版后结果 | 意义 |
| --- | --- | --- | --- |
| 浅发动漫 `portrait` | brown 99.5% / brown 61.9% | BiSeNet：light 83.2% / light 93.7% | 有所改善，但只到“浅色组”，仍无法辨金/白/银/灰 |
| 同一 `portrait` | brown / brown | SegFormer：brown 79.2% / red 96.1% | 更完整蒙版也不保证分类更准 |
| 蓝发头部 `101-head` | blue 99.1% / blue 99.8% | BiSeNet：blue 98.7% / aqua 56.5% | 漏分后 CAFormer 从蓝变青，恶化相近色判断 |
| 银灰发头部 `107-head` | light 99.8% / light 99.9% | SegFormer：green 99.3% / light 99.7% | 只留头发使 MobileNet 判绿，不能假定裁净即可靠 |
| 棕发 `hutao` | brown / red | BiSeNet：brown 95.1% / red 90.2% | CAFormer 的红发误判仍在 |

鱼 `#4`、卧室 `#45`、玩偶 `#42`：两个真人分割器在这三例返回空蒙版，旧动漫模型则有大片“头发”。但真人模型也会对确实有头发的多人动漫图返回空蒙版，不能简单把空蒙版当作无人/无头发判据。蓝发 SegFormer 仅 2 个像素的输出也被实验送去分类，结果毫无参考意义；此例直接体现分割失败不能继续当有效发色输入。

## 建议

继续以“按角色头部裁图”为输入基线；头发分割先做可查看、可修正的辅助，不直接作为自动决定的依据。若要用于动漫批量流程，需要适配动漫数据的专用分割器或针对当前材料训练/微调，并建立人工蒙版与相近色验证集。分割也补不齐两个发色分类器只有 light 浅色大类的限制。

本轮仅完成研究实测，没有改现有页面/生产接受策略，没有写入已有人工决定。

## 许可与证据

SegFormer 模型卡明确为 **non-commercial research and educational purposes**，这里只做研究对照，没有纳入发布或生产。BiSeNet 与旧动漫项目源码是 MIT；真人模型使用 CelebAMask-HQ 数据，旧动漫权重的独立授权说明未找到，生产采用前仍需核实适用授权。

`samples.json` 保存样本来源和手工裁框；`*-results.json` 保存蒙版面积、概率与耗时；`color-results.json` 保存全部分类分数；`provenance.json` 保存权重 hash/来源与固定 revision；`images/` 保存控制输入、蒙版、叠加图、白底头发和紧裁分类输入。源包完整原图仍在上一轮的归档目录，本次没有挪动它们。

浏览器已验证 68 张报告卡片、272 张展示图片均可加载，模型/样本筛选可用，0 个页面脚本错误。见 `browser-results.json`、`blue-hair-comparison.png`、`silver-hair-example.png`。分类结果另核验 85 个变体、72 个真实执行结果、RGB 输入以及专用模型 softmax 概率和。

测试完成后临时脚本、源码、隔离依赖与下载权重移入 `.delete/hair-segmentation-tests-20261119/backend/smoke/hair-segmentation/`，保留这个可视报告和结果。无 commit / push。
