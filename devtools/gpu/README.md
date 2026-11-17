# 开发会话 GPU 串行等待

所有开发会话启动 GPU 测试前使用 `serial.py`，按 **nvidia-smi 物理卡号或 UUID** 共用操作系统互斥锁。等待阶段不导入 Torch、不初始化 CUDA。Windows 锁名为 `Global\lulynx.gpu.<UUID的SHA256前24位>`，跨仓库和工作目录共用；持有进程异常退出后由系统释放。

运行 Python 测试脚本：

```powershell
& 'H:/lulynx-trainer/backend/env/python-flashattention/python.exe' -B `
  'H:/tagger/devtools/gpu/serial.py' --device 0 'path/to/test.py' test_argument
```

浏览器或 HTTP 编排脚本应在提交任务前获取锁，确认训练／推理 worker 完全退出后才离开上下文：

```python
import sys
sys.path.insert(0, 'H:/tagger')
from devtools.gpu.serial import gpu_lock

with gpu_lock(0, label='WebUI training case'):
    # 提交一个任务，等待它结束，并在 finally 中确认／清理自己的 worker。
    run_one_complete_case()
```

每个 case 结束就释放锁，再排下一项；不要拿着锁等另一会话完成，也不要父脚本持锁后让子脚本重复申请同一锁。已初始化 CUDA 的脚本在释放前同步并清理未使用的缓存；模型必须已离开作用域，子 worker 必须已退出，仍被引用的模型权重不会被这一步释放。首次切换前，已启动但未持锁的任务需要先结束。锁只约束使用该工具的开发进程，普通 WebUI 或外部 GPU 任务不会自动加入。
