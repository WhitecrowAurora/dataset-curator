# 首次安装 Hub / Transformers 依赖冲突修复

2026-11-23。

## 原因与修改

用户日志中的 `ResolutionImpossible` 是发布清单的确定性依赖冲突，与 RTX 6000 Pro 的驱动或显存无关。`requirements-embedded.txt` 同时固定 `transformers==5.14.1` 与 `huggingface_hub==2.4.0`，但 Transformers 的发行元数据要求 `huggingface-hub>=1.5.0,<2.0`。tokenizers 0.22.2 同样要求 Hub `<2.0`；datasets 5.1.0 要求 Hub `>=1.31.0,<3.0`。

仅将 Hub 固定为开发环境已经运行的共同兼容版本 `1.33.0`，补充兼容原因注释。保持 Transformers 和其他依赖版本。Windows / Linux 安装脚本和客户端依赖页均读取这份清单，不新增安装入口或运行检查代码。

## 用户操作

已拿旧包的用户：关闭客户端，把 `dist/tagger-20261123-install-deps-patch.zip` 解压到程序根目录，覆盖 `requirements-embedded.txt`，再重新运行 `install.bat`。可以直接把旧清单中的 `huggingface_hub==2.4.0` 改为 `huggingface_hub==1.33.0` 后重跑安装。原安装过程中已下载／安装的依赖可以复用，不需要删除环境、模型、任务或个人配置。

新用户使用完整包 `dist/tagger-20261123-install-deps-fix.zip`，按原安装流程运行。

## 验证

- PyPI 发行版本核验与本机发行元数据确认：1.33.0 存在，并满足 Transformers / tokenizers / datasets 的共同范围。
- 在 Windows Python 3.12.9 上使用 `pip install --dry-run --ignore-installed --only-binary=:all:` 解析完整基础清单成功，选择 Transformers 5.14.1、Hub 1.33.0、tokenizers 0.22.2。报告：`resolver-core.json`、`resolver-core.log`。
- 基础清单、TAG 清单、native-nl extra 合并解析通过；此项复用了本机已安装的 torch / torchvision / datasets 等 GPU 与打标依赖，没有重新下载大包。报告：`resolver-combined.json`、`resolver-combined.log`。
- 从修正发布 ZIP 解压到隔离目录，初始没有 `env/python`，运行实际 `install.bat -SkipTag -NoLlama -IndexUrl https://pypi.org/simple` 成功，官方便携 Python 引导、基础依赖安装、开发文件补齐与原自检完成；再次运行安装成功。报告：`fresh-install.log`、`repeat-install.log`。
- 新环境 `pip check` 输出 `No broken requirements found.`；FastAPI TestClient HTTP、Hub API 导入及原项目 TrainingText 加载本机 Qwen3.5-0.8B tokenizer 计数通过。报告：`fresh-install.json`、`fresh-pip-check.log`、`fresh-imports.log`。
- 新完整包 ZIP CRC、源码与前端资源、安装清单、内嵌 worker 来源哈希、个人状态排除与凭据扫描通过。补丁 CRC 与清单字节匹配通过。

未在 RTX 6000 Pro 用户机器上进行完整 GPU 依赖安装或模型推理验收；本轮修复和新环境验证针对首次安装日志中的基础依赖解析失败。未更改服务器，也未修改现有开发环境的包。

## 发行

完整包：76,749,834 bytes；SHA256 `2d27ac13d50799160705d48a107828d9931cba9f7e9617512d84cf18d5964feb`，见 `package.json`。

补丁：3,450 bytes；SHA256 `5c8ffe050b28ce432873b335689131b76a0115b56f833ebe61ca648e7bfcfec5`，见 `patch.json`。仅含修正后的清单和使用说明。

测试脚手架与隔离安装环境在测试后按原结构归档到 `H:/lulynx-trainer/.delete/tagger-install-deps-20261123/backend/smoke/tagger-install-deps-20261123`；发行暂存由既有打包器归档到 tagger `.delete/pack-staging`。
