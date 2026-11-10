# 2026-11-10 我方改动说明

改动三个地方，都是**小改**，目的是让 worker 能当"常驻工位"用。每处都有独立说明和验证方式，可以单独取舍。

一共动了两个文件：`server.py`（1 处，修 bug）、`worker.py`（3 处，加功能）。

---

## 一、`server.py`：修 `serve --server-id` 在空数据目录下崩

### 原来的问题

```
$ python server.py --data-dir <空目录> serve --server-id fox-01
sqlite3.OperationalError: no such table: meta
```

`cmd_serve` 里写 `server_id` 的 INSERT 排在 `store.init_schema()` 之前，而 `store.conn()` 会顺手创建一个空 db，于是打到还没建的 `meta` 表上。

两个副作用让它更难查：

1. 崩之前留下一个 4096 字节的空 `db.sqlite3`。下次**不带** `--server-id` 再起，`db_path.exists()` 已经是真，于是跳过「尚未初始化」检查、继续 `init_schema()` 建表——看起来"自己好了"。同一个目录时好时坏。
2. 不带 `--server-id` 时行为是对的（干净报错「尚未初始化，请先运行 repo add」，退出码 2），所以很容易误判成参数问题。

### 改法

把顺序调成「先查库存在 → 再建表 → 最后写 server_id」：

```python
def cmd_serve(args) -> int:
    store = Store(args.data_dir)
    if not store.db_path.exists():
        print("尚未初始化，请先运行 server.py repo add", file=sys.stderr)
        return 2
    store.init_schema()
    if getattr(args, "server_id", None):
        c = store.conn()
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('server_id',?)", (args.server_id,))
        c.close()
```

注意没有把 `init_schema()` 提到最前面——那样会建出全套空表，「尚未初始化」这条引导就永远不会出现了。

### 验证

| 场景 | 结果 |
|---|---|
| 空目录 + `--server-id` | 退出码 2，提示「尚未初始化」，**无 traceback，目录无残留** |
| 已初始化目录 + `--server-id` | 正常起服务，handshake 200，`meta.server_id = fox-01` |

---

## 二、`worker.py`：`login` 不再顺手吞掉一个包

### 问题

`_handshake_impl` 第 504 行无条件调 `assign()`，所以**握手必然派工**。而 `login` 只登记身份、写完配置就退出，没人干活——那一片要闲置到 360s TTL 到期，这期间别的设备领不到它。

表现：有人在别的机器上点了一下「连接」，池子却少了一片。

### 改法

`cmd_login` 末尾，把刚派来的租约还回去：

```python
    if leases:
        release_unused(cfg, leases)
        print(f"（已归还刚派来的 {len(leases)} 片：只登记身份，不开工）")
```

用的是现成的 `release_unused`（`/api/v1/release` 幂等，409 当成功），和收工/中断走同一条路径。

### 验证（桩替掉握手响应）

握手返回 1 个 active 租约 → 实际发出 `POST /api/v1/release {"lease_id": "L1"}`，日志打印「已归还租约 active:p0.tar」。

### 另一条可选路子

如果更希望从根上治，可以给 handshake 加一个 `"claim": false` 参数，让"登记身份"和"领任务"彻底分开。这动的是核心协议，我没有替你决定；客户端这条改法不需要服务端配合，先上这条。

---

## 三、`worker.py`：新增 `--wait` 常驻等待

### 原来的行为

池子空了 → `服务端暂无派工` → **退出码 0**。当作"点一次干一批"够用，但当常驻工位就不行：页面从「运行中」直接变「已停止」。

### 改法

把「握手取租约」抽成 `_claim()`，`cmd_run` 外面套一层循环：

```python
    while True:
        leases = _claim(cfg, interval)
        if not leases:
            if not args.wait:
                return EXIT_OK                       # 原行为不变
            if args.idle_timeout and 空闲超过上限:
                return EXIT_OK
            time.sleep(nap)                          # 默认 30s
            continue
        rc = _run_loop(...)
        if not args.wait or rc != EXIT_OK:
            return rc
```

要点：

- **默认关闭**。不加 `--wait` 时行为和以前一模一样，老脚本无需改。
- 退出码为 0 才继续等；认证失效（3）、租约丢失（5）、任务失败（6）直接退出——再等也是同样的结果。
- 池空时**没有心跳要维持**（手上没租约，`Beater` 本来就退出），所以等待纯粹是睡眠，不占服务端资源。
- `Ctrl-C` 走 main 里原有的 `KeyboardInterrupt` 分支，正常结束。

### 验证

```
$ worker.py run --wait --idle-timeout 3
[16:37:27] 服务端暂无派工（池子空了或配额已满）
[16:37:27] 3s 后再看（Ctrl-C 结束）
[16:37:30] 服务端暂无派工（池子空了或配额已满）
[16:37:30] 已经 3s 没有派工，收工
退出码=0 耗时=3.2s
```

---

## 四、`worker.py`：新增 `--max-packages` 片数配额

「今天只打 5 片就停」这种场景。计数在 `_run_loop` 里累加，够了就停手，并且**把服务端刚派来的下一片还回去**（不然配额用完了还揣着一片，又变成闲置到 TTL）。

```python
        if args.max_packages and done["n"] >= args.max_packages:
            log(f"已达本次配额 {args.max_packages} 片，收工")
            release_unused(cfg, res.get("leases") or [])
            return EXIT_OK
```

`0`（默认）= 不限。

---

## 五、参数总览（`run` 子命令新增三项）

| 参数 | 默认 | 说明 |
|---|---|---|
| `--wait` | 关 | 常驻等派工：池子空了不退出，每 30s 再问一次 |
| `--idle-timeout N` | 0 | 配合 `--wait`：连续 N 秒没活就收工（0 = 一直等） |
| `--max-packages N` | 0 | 本次最多交付 N 片，够了就停并归还多派的（0 = 不限） |

`--once` 语义不变（只处理一个包就退出），优先级高于 `--max-packages`。

---

## 六、两个已知边界（没改，先说明）

1. **`--wait` 期间被强杀，租约不会立即归还**。Windows 上 `terminate()` 是硬杀，进程来不及跑 `finally`，那一片要等 TTL（360s）回池。POSIX 上如果加 `signal.SIGTERM` 处理可以优雅收尾，需要的话我补。
2. **`/api/v1/status` 不返回 `last_seen` / `last_beat_at`**。我们想在客户端做"心跳还活着吗"的指示灯，但本地没有时间戳可读（`Beater` 成功时不打日志、不落盘，且没租约时直接不发心跳）。如果 `status` 能带上 `devices.last_seen`，那边做状态展示会简单很多——这条是建议，不是 bug。

---

## 附：改动位置索引

| 文件 | 位置 | 改动 |
|---|---|---|
| `server.py` | `cmd_serve`（约 1453 行） | 调换「建表」与「写 server_id」的顺序 |
| `worker.py` | 顶部常量（约 57 行） | 新增 `WAIT_POLL = 30` |
| `worker.py` | `cmd_login` 末尾（约 178 行） | 归还握手误派的租约 |
| `worker.py` | `cmd_run` / 新增 `_claim`（约 455 行） | `--wait` 外层循环 |
| `worker.py` | `_run_loop`（约 490 行） | 加 `done` 计数与配额判断 |
| `worker.py` | `main`（约 559 行） | 三个新参数 |
