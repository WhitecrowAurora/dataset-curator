lulynx 打标器 —— 安装与调度中心
===============================

首次使用
--------
1. 解压到可写目录，例如 D:\lulynx-tagger；不要放进 Program Files。
2. 运行 install.bat。安装包自带 Python 初始化文件，依赖与 llama.cpp 联网安装。
   只做 TAG、不需要 llama.cpp：install.bat -NoLlama
   只做封包或使用已有 GGUF 做 NL：install.bat -SkipTag
   -SkipTag 会跳过 torch/transformers，不能用于 TAG 或从原始模型转换 GGUF。
   离线依赖包：install.bat -WheelDir env\initialization\wheels
3. 双击 Tagger.Shell.exe；也可运行 run-web.cmd，在浏览器打开本地界面。
   Windows 10/11 64 位；壳子需要 Edge WebView2 运行时。
   自包含版无需单独安装 .NET；依赖框架版需要 .NET 8+ Desktop Runtime。
   Linux 使用 install.sh 和 run-web.sh；桌面 exe 只适用于 Windows。

调度中心自动化
--------------
进入「调度中心」，填写中心地址（http://119.28.16.93）、用户身份、
打标模式（TAG/NL/两者）、TAG/NL 模型、设备、精度/量化和并发，保存配置。
原始 NL 模型可自动下载、转换和量化；已有 GGUF 会复用本机文件。
设备和精度应与本机硬件、所选引擎相符。

主动模式：启动后向中心请求分包，自动准备模型、下载素材、打标、回传和申报。
被动托管：保存配置后另点开始托管，中心自动安排分片并控制开始／停止／检查。
托管期间本机模型、设备、精度与并发冻结；本机可随时停止或退出托管。
HF Token、本机路径和自定义命令由本机管理，不能由中心任意改写。

HF 授权
-------
访问受限仓库时填入有权限的 HF Token；下载和上传分别需要相应权限。
仓库列表能浏览不代表数据文件可匿名下载。manual gated 仓库还需账号获准访问。
Token 默认只在当前进程使用，勾选记住才保存到本机；不随发布包分发。
未勾选记住时，重新打开客户端需要再次填 Token。

停止与恢复
----------
崩溃/强制退出后重新打开客户端，可按保存的运行意图续接任务，复用已校验分片。
自动回传成功后才申报完成；回传失败保留任务等待恢复。
用户主动停止会归还未完成租约；再次打开保持停止。
Windows 宿主退出时回收它启动的模型、worker 和转换子进程。
Linux 宿主崩溃后的进程自动回收尚未验收。

升级
----
关闭旧客户端，将新版程序文件覆盖到原目录。发布包不带 configs/models.json、
configs/ui-form.json、configs/nl_api.json、configs/setup.json、work、models 或运行凭据，
保留本机原有目录与设置。内置 tiers.yaml、提示词预设会随版本更新，自行修改过的
预设请先备份。不要把旧目录的模型和工作目录覆盖成空目录。

独立 worker（无需安装完整 WebUI）
--------------------------------
包内 worker.py 与客户端同源，调度中心当前入口为 http://119.28.16.93。
Python 3.10+，无需 pip；worker-sources.json 记录内嵌模块的 SHA256。
分片下载需要 curl，自定义打标命令所需的模型环境另行安装。
python worker.py login --server http://119.28.16.93 --user-id 你的名字
python worker.py run --help
run --cmd 指定外部执行命令；--auto-upload 开启产物回传，成功后才申报完成。
--wait 常驻等待；--download-conns 4 多连接下载；--parts 选择派工倾向。
被动托管请使用完整客户端。先保存本机环境，再点击开始托管；托管期间模型、设备
和精度锁定，中心安排仓库、分片及开始／停止／检查。退出托管后可修改本机环境。
勾选自动回传后，中心按分片分别统计 TAG／NL 成功、排除、失败、漏项及完成状态。
失败或漏项不会登记完成；已有结果按来源归并，可由下一台设备增量续作。
历史手动产物先关联原分片和产物目录，再由面板下发检查。未回传验收的旧结果
显示未验收，中心不会推测成功数量。分项分配和检查之后可点击刷新分片查看结果。

排错
----
安装失败先看 install.bat 输出，依赖没装齐请重跑安装。
模型下载、转换、启动进度在界面显示，核对错误里的路径和剩余磁盘空间。
检查命令：env\python\python.exe -m tagger.cli --help
llama.cpp 状态：env\python\python.exe -m tagger.cli llama status
桌面日志：%LOCALAPPDATA%\Tagger\shell.log
MCP 操作支持见包内 agent.md（需先打开本地服务）。
