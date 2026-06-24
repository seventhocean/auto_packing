# Auto_Packing 代码导读与练习目标

> 项目定位：补丁包自动构建与分发平台
> 技术栈：Python 3.6 + Flask + Redis + Bash + Docker + K8s
> 代码规模：app.py ~1120 行，worker.py ~140 行，shell 脚本 ~420 行，前端 ~600 行

---

## 一、项目架构全景

```
┌─────────────────────────────────────────────────────────────┐
│                        浏览器 (前端)                         │
│                 templates/index.html (606 行)                │
│            - V6/V7 模式切换                                  │
│            - EventSource 接收 SSE 实时推送                   │
└─────────────────────┬───────────────────────────────────────┘
                      │ HTTP
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                  app.py (Flask Web 进程)                     │
│  职责：HTTP API + Redis 状态管理 + SSE 推送                  │
│  不执行构建！只负责入队和查询                                │
└─────────────────────┬───────────────────────────────────────┘
                      │ LPUSH 任务入队
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                    Redis (消息队列 + 状态存储)               │
│  - build:queue          待消费任务队列                       │
│  - task:<id>:meta       任务状态 (JSON)                     │
│  - task:<id>:logs       任务日志 (list)                     │
│  - builds:active        并发构建计数                         │
│  - build:processing:<w> worker 正在处理的任务                │
│  - worker:<w>:heartbeat worker 心跳 (TTL 15s)              │
└─────────────────────┬───────────────────────────────────────┘
                      │ BRPOPLPUSH 阻塞式消费
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                  worker.py (构建 Worker 进程)                │
│  职责：消费队列 + 执行构建 + 心跳上报                        │
│  调用 app.py 中导出的 run_build_task() / run_build_task_v7() │
│                                                              │
│  调用链：                                                    │
│  worker.process_task()                                       │
│    → run_build_task() / run_build_task_v7()                 │
│      → subprocess 调用 bin/*.sh                              │
│        → nerdctl pull / nerdctl save                         │
│        → tar -czf 打包                                       │
└─────────────────────────────────────────────────────────────┘
```

### 关键设计决策（面试必问）

**Q1: 为什么 Web 和 Worker 分离？不直接在 Flask 里构建？**

> 1. Flask 跑在 gunicorn 上，Worker 数量可以独立扩缩
> 2. 构建耗时长（15分钟），在 Web 进程里做会阻塞其他请求
> 3. Worker 崩溃不影响 Web，Web 崩溃不影响正在构建的 Worker
> 4. Redis 队列做缓冲，瞬时流量高峰不会丢任务

**Q2: 为什么用 Redis 队列不用 RabbitMQ/Kafka？**

> 1. 已经有 Redis 存状态，复用同一个组件减少运维成本
> 2. 任务量小（一天几十个），不需要 MQ 的高级特性
> 3. BRPOPLPUSH 原子操作保证任务"至少被消费一次"

**Q3: 为什么有 builds:active 计数器，不直接算 processing 队列长度？**

> 因为要控制全局并发上限（max_concurrent_tasks=3），INCR 是原子操作，能精确控制。
> processing 队列长度也能算，但需要 SCAN 所有 worker 的队列，效率低。

---

## 二、文件清单与职责

| 文件 | 行数 | 职责 | Python 特性 |
|---|---|---|---|
| **app.py** | 1120 | Flask API + Redis 状态 + 构建逻辑 + SSE | dict/set/正则/文件IO/subprocess/生成器/异常处理/上下文管理器 |
| **worker.py** | 140 | Redis 队列消费 + 心跳 + 任务回收 | threading/uuid/JSON/异常处理 |
| **config.yaml** | 12 | Redis 配置 | YAML 格式 |
| **templates/index.html** | 606 | 前端单页（HTML+CSS+JS）| - |
| **bin/get_patch_image_tag_list.sh** | 167 | 计算镜像版本差异 | Bash: trap/mktemp/sort/awk/sed |
| **bin/pull_save.sh** | 256 | 拉取并保存镜像 | Bash: 参数解析/nerdctl |
| **bin/deepflow_patch_upgrade.sh** | ~400 | 部署端升级脚本（打进包里）| Bash |
| **dockerfile** | 60 | 容器镜像构建 | Dockerfile |
| **entrypoint.sh** | 46 | 容器启动入口（克隆 nuwa + 生成 OSS 配置）| Bash |
| **requirements.txt** | 15 | Python 依赖清单 | pip |

---

## 三、app.py 代码导读（重点）

### 3.1 文件结构

```
app.py
├── 1-15    导入模块
├── 16-52   Flask 初始化 + 全局路径配置
├── 54-91   配置加载（YAML + 环境变量）+ Redis 客户端初始化
├── 95-218  Redis Key 操作函数（核心状态管理）
├── 222-279 工具函数（ANSI 过滤、子进程流式执行）
├── 282-363 日志 + 进度更新 + OSS 版本获取
├── 365-428 已有包列表 + 路径穿越校验
├── 430-644 run_build_task() V6 构建主逻辑 ★
├── 645-782 run_build_task_v7() V7 构建主逻辑 ★
├── 785-1066 Flask 路由 + SSE
└── 1115-1120 启动入口
```

### 3.2 关键代码段

#### A. 配置加载 (54-91) — 练习"文件 IO + dict 操作"

```python
def load_app_config():
    config = json.loads(json.dumps(DEFAULT_CONFIG))  # ★ dict 深拷贝技巧
    if os.path.exists(APP_CONFIG_PATH):
        with open(APP_CONFIG_PATH, 'r', encoding='utf-8') as f:  # ★ 上下文管理器
            loaded = yaml.safe_load(f) or {}
        if isinstance(loaded.get('redis'), dict):  # ★ dict.get() 安全取值
            config['redis'].update(loaded['redis'])

    redis_password = os.environ.get('REDIS_PASSWORD')  # ★ 环境变量读取
    if redis_password is not None:
        config['redis']['password'] = redis_password

    # 类型转换（YAML 加载的值可能是字符串）
    config['redis']['port'] = int(config['redis']['port'])
    # ...
    return config
```

**练习目标**：
- [ ] 能手写"读取 YAML 配置 → 合并默认值 → 环境变量覆盖"的逻辑
- [ ] 理解 `json.loads(json.dumps(DEFAULT_CONFIG))` 为什么能实现深拷贝
- [ ] 理解 `os.environ.get()` vs `os.environ[]` 的区别

#### B. Redis Key 设计 (95-218) — 练习"函数封装 + 字符串格式化"

```python
def task_meta_key(task_id):
    return f"{REDIS_CONFIG['key_prefix']}:task:{task_id}:meta"
# 输出: "auto_packing:task:xxx:meta"

def try_acquire_build_slot(task_id):
    current_builds = redis_client.incr(active_builds_key())  # ★ 原子操作
    if current_builds > REDIS_CONFIG['max_concurrent_tasks']:
        redis_client.decr(active_builds_key())  # ★ 超限回滚
        return False
    redis_client.set(task_slot_key(task_id), "1")
    return True

def release_build_slot(task_id):
    if redis_client.delete(task_slot_key(task_id)):
        current_value = redis_client.decr(active_builds_key())
        if current_value < 0:  # ★ 防御性检查：避免计数变负
            redis_client.set(active_builds_key(), 0)
```

**练习目标**：
- [ ] 能画出所有 Redis Key 的用途和生命周期
- [ ] 理解 `INCR/DECR` 原子性为什么能用于并发控制
- [ ] 理解 `reconcile_build_counter()` 修复计数泄漏的思路

#### C. ANSI 过滤 + 子进程流式执行 (222-279) — 练习"正则 + 生成器 + subprocess"

```python
_ANSI_RE = re.compile(r'\x1B\[[0-9;]*[a-zA-Z]')  # ★ 模块级正则（性能优化）
_SHELL_TS_RE = re.compile(r'^\[\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\]\s*')

def _run_and_stream(task_id, cmd, task_log_path, log_prefix=""):
    """Popen 逐行读输出，过滤 ANSI 码，关键事件实时推送 Redis"""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,  # ★ 文本模式（返回 str 不是 bytes）
        bufsize=1  # ★ 行缓冲
    )
    for line in iter(proc.stdout.readline, ''):  # ★ 迭代器 + 哨兵值
        clean = _clean_shell_line(line)
        if not clean:
            continue
        with open(task_log_path, 'a', encoding='utf-8') as f:
            f.write(clean + '\n')
        # 判断是否为关键事件（立即推送）
        is_key = any(kw in lower for kw in ['登录成功', 'error:', ...])  # ★ 生成器表达式
        if is_key and clean != last_logged:
            write_log(clean, task_id=task_id)
    proc.wait()
    return proc.returncode
```

**练习目标**：
- [ ] 能手写"用 subprocess.Popen 逐行读取命令输出"
- [ ] 理解 `subprocess.run` vs `subprocess.Popen` 区别（同步 vs 异步）
- [ ] 理解 `iter(callable, sentinel)` 的用法
- [ ] 理解 `any(...)` 里的生成器表达式为什么比列表表达式省内存

#### D. run_build_task() V6 构建主流程 (430-644) — 练习"异常处理 + finally + 文件操作"

```python
def run_build_task(task_id, current_version, target_version):
    try:
        # 1. 创建任务目录
        task_dir_name = f"{current_version}_to_{target_version}_{task_id}"
        task_image_dir = os.path.join(IMAGE_TAR_ROOT, task_dir_name)
        if os.path.exists(task_image_dir):
            shutil.rmtree(task_image_dir)  # ★ 递归删除
        os.makedirs(task_image_dir, exist_ok=True)

        # 2. 依赖检查
        for dep_path, dep_desc in dependencies:
            if not os.path.exists(dep_path):
                raise Exception(f"核心依赖缺失：{dep_desc}")  # ★ 主动抛异常

        # 3. 调用 shell 脚本
        script_result = subprocess.run(
            ["/bin/bash", PATCH_LIST_SCRIPT_PATH, current_version, target_version, task_patch_list_path],
            check=True,  # ★ 非零退出码自动抛 CalledProcessError
            timeout=600  # ★ 超时保护
        )

        # 4. 验证结果
        tar_files = glob.glob(os.path.join(task_image_dir, "*.tar"))  # ★ 通配符匹配
        if not tar_files:
            raise Exception("镜像拉取失败")

        # 5. 打包
        subprocess.run(
            ["tar", "-czf", upgrade_path,
             "--transform", r"s|.*/||",
             "--transform", f"s|.*|{extract_dir}/&|",
             *files_to_pack],  # ★ * 解包列表为参数
            timeout=1800
        )

        # 6. 更新状态
        update_task_status(task_id, {
            "status": "complete",
            "package_size_mb": round(os.path.getsize(upgrade_path)/1024/1024, 2),  # ★ 浮点运算
            # ...
        })

    except Exception as e:
        update_task_status(task_id, {"status": "error", "message": f"构建失败：{e}"})
        traceback.print_exc()  # ★ 打印异常堆栈

    finally:
        # 无论成功失败，都清理临时文件
        if os.path.exists(task_image_dir):
            try:
                shutil.rmtree(task_image_dir)
            except Exception as clean_e:
                write_log(f"清理失败：{clean_e}", level="WARNING")
    finally:  # ★ 外层 finally：释放构建槽位
        release_build_slot(task_id)
```

**练习目标**：
- [ ] 能手写"创建目录 → 调用命令 → 验证结果 → 清理"的流程
- [ ] 理解 `try/except/finally` 的嵌套关系（两个 finally 各自职责）
- [ ] 理解 `glob.glob` / `shutil.rmtree` / `os.path.getsize` 这些常用函数
- [ ] 理解为什么清理代码里还要 `try/except`（避免清理失败导致整个任务失败）

#### E. SSE 推送 (1018-1066) — 练习"生成器 + Flask Response"

```python
def generate_sse():
    try:
        yield f"data: {json.dumps(initial_status)}\n\n"  # ★ yield 关键字
        while True:
            task_status = get_task_status(task_id)
            yield f"data: {json.dumps(task_status)}\n\n"
            if task_status.get('complete', False):
                break
            time.sleep(1)
    except GeneratorExit:  # ★ 客户端断开连接时触发
        pass
    except Exception as e:
        yield f"data: {json.dumps({'status': 'error', ...})}\n\n"

return Response(generate_sse(), mimetype='text/event-stream')  # ★ 流式响应
```

**练习目标**：
- [ ] 能手写一个 SSE generator
- [ ] 理解 `yield` 和 `return` 在 generator 中的区别
- [ ] 理解 `GeneratorExit` 异常什么时候抛出
- [ ] 理解 Flask `Response` 的 `mimetype='text/event-stream'`

---

## 四、worker.py 代码导读

### 4.1 整体结构

```
worker.py
├── 1-27     导入 + Worker ID 生成
├── 30-33    heartbeat_loop() 心跳线程
├── 36-57    reclaim_stale_tasks() 回收失活 worker 的任务
├── 60-63    reclaim_loop() 回收线程
├── 66-101   process_task() 任务处理（槽位等待 + 调用构建）
└── 104-140  main() 主循环
```

### 4.2 关键代码段

#### A. Worker ID 生成

```python
WORKER_ID = "{}-{}-{}".format(socket.gethostname(), os.getpid(), uuid.uuid4().hex[:8])
# 输出示例: "maintenance-worker-7b8f9d-12345-a1b2c3d4"
```

**为什么要三段？**
- `hostname`: 知道是哪个 Pod
- `pid`: 知道是哪个进程（同一 Pod 可能多进程）
- `uuid[:8]`: 避免进程重启后 ID 冲突

#### B. 心跳线程 (30-33)

```python
def heartbeat_loop():
    while True:
        redis_client.set(HEARTBEAT_KEY, str(int(time.time())), ex=15)  # ★ ex=15 设置 TTL
        time.sleep(5)
```

**练习目标**：
- [ ] 理解 `ex=15` TTL 机制：15 秒没更新就自动删除
- [ ] 理解为什么每 5 秒写一次（留 10 秒容错）
- [ ] 理解为什么用 daemon 线程（主线程退出时自动结束）

#### C. 失活任务回收 (36-57)

```python
def reclaim_stale_tasks():
    processing_pattern = "{}:build:processing:*".format(REDIS_CONFIG["key_prefix"])
    for processing_key in redis_client.scan_iter(match=processing_pattern):  # ★ SCAN 迭代
        worker_id = processing_key.split(":")[-1]
        heartbeat_key = worker_heartbeat_key(worker_id)
        if redis_client.exists(heartbeat_key):
            continue  # 心跳还在，worker 还活着

        # 心跳消失 → worker 已死，回收其任务
        pending_tasks = redis_client.lrange(processing_key, 0, -1)
        for raw_payload in reversed(pending_tasks):  # ★ 倒序恢复，保持原队列顺序
            redis_client.rpush(build_queue_key(), raw_payload)
        redis_client.delete(processing_key)
```

**练习目标**：
- [ ] 理解 `scan_iter` 比 `keys` 好在哪里（不阻塞 Redis）
- [ ] 理解为什么要 `reversed(pending_tasks)`
- [ ] 理解为什么回收后要调用 `reconcile_build_counter()`

#### D. 主循环 (104-140)

```python
def main():
    reclaim_stale_tasks()  # 启动时先清理一次
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=reclaim_loop, daemon=True).start()

    while True:
        raw_payload = redis_client.brpoplpush(  # ★ 阻塞式原子操作
            build_queue_key(),
            PROCESSING_QUEUE_KEY,
            timeout=5
        )
        if not raw_payload:
            continue  # 超时（队列空），继续等待

        payload = json.loads(raw_payload)
        task_id = payload.get("task_id", "unknown")

        try:
            process_task(payload)
        except Exception as exc:
            update_task_status(task_id, {"status": "error", ...})
        finally:
            redis_client.lrem(PROCESSING_QUEUE_KEY, 1, raw_payload)  # ★ 从处理队列移除
```

**练习目标**：
- [ ] 理解 `BRPOPLPUSH` 语义：阻塞等待 + 原子弹出 + 推入目标列表
- [ ] 理解为什么 `lrem` 放在 `finally`（确保任务一定被移除）
- [ ] 理解 `timeout=5` 的作用（避免死等）

---

## 五、Shell 脚本要点

### 5.1 get_patch_image_tag_list.sh

```bash
# 关键技巧：
TMP_FILE=$(mktemp "${TRASH}/patch_image_tag_list.XXXXXX")  # ★ 创建临时文件
cleanup() {
    rm -f "${TMP_FILE}"
}
trap cleanup EXIT  # ★ 脚本退出时自动清理

# 计算版本差异：
CURRENT_LINE=$(printf "%s\n" "${ALL_PATCH_LIST[@]}" | grep -n "^${VERSION}$" | cut -d: -f1)
# 用行号定位版本在列表中的位置

# 镜像去重：
cat file | sort -V | tac | awk -F: '!seen[$1]++' | tac
# sort -V：按版本号排序
# tac：倒序（让高版本在前）
# awk '!seen[$1]++'：按第一列去重（保留首次出现）
# tac：恢复顺序
```

### 5.2 pull_save.sh

```bash
# 参数解析模板：
while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--dir) save_dir="$2"; shift 2 ;;
        -f|--file) local_image_list="$2"; shift 2 ;;
        -*) echo "未知选项：$1"; exit 1 ;;
        *) POSITIONAL_ARGS+=("$1"); shift ;;
    esac
done

# 镜像名清洗：
local image_name=$(echo "$full_image_name" | awk -F':' '{print $1}' | awk -F'/' '{print $NF}' | sed 's/[^a-zA-Z0-9_-]//g')
# 用 awk 按分隔符切割，用 sed 删除非法字符
```

---

## 六、面试必讲的 6 个设计决策

### 决策 1：为什么 Web 和 Worker 分离？
> 见 3.2 Q1 答案

### 决策 2：并发控制怎么做？
> `INCR builds:active` 原子计数 + `max_concurrent_tasks=3` 上限
> 失败处理：`DECR` 回滚 + 负数保护 + `reconcile_build_counter()` 兜底

### 决策 3：任务失败怎么办？
> 三层兜底：
> 1. Worker try/except 捕获异常 → 更新状态为 error
> 2. Worker 崩溃 → 心跳 15s 过期 → reclaim_loop 30s 扫描回收任务
> 3. reconcile_build_counter() 修复计数泄漏

### 决策 4：SSE 为什么不用 WebSocket？
> 1. SSE 是单向推送，正好满足"服务端 → 前端"场景
> 2. 基于 HTTP，不需要额外协议，防火墙友好
> 3. Flask 原生支持，WebSocket 需要额外库（gevent/socketio）
> 4. 自带重连机制（浏览器 EventSource 自动重连）

### 决策 5：路径穿越攻击怎么防？
> `validate_package_filename()` 四重校验：
> 1. 禁止空文件名
> 2. 仅允许 `.tar.gz` 后缀
> 3. 禁止 `/` `\` 路径分隔符
> 4. `os.path.abspath()` 后校验是否在允许的目录内

### 决策 6：为什么用 subprocess 而不是 Python 直接实现？
> 1. `pull_save.sh` 调用 nerdctl，Python 没有好用的 nerdctl 客户端库
> 2. Shell 脚本已经存在，复用成本低
> 3. 关键：`Popen` + 逐行读取 + 实时推送 Redis，不能等命令跑完再处理输出

---

## 七、练习路线图

### 第一周：Python 基础恢复（每天 1-2 小时）

#### Day 1-2：dict + 文件 IO

**练习任务**：
1. 重写 `load_app_config()` 函数
   - 读取 YAML → 合并默认值 → 环境变量覆盖
   - 用到的：`open()`, `with`, `dict.get()`, `dict.update()`, `os.environ.get()`
2. 写一个函数：统计某个目录下各类型文件数量
   - 用到的：`os.listdir()`, `os.path.isfile()`, `dict` 计数

#### Day 3-4：字符串 + 正则

**练习任务**：
1. 重写 `_clean_shell_line()` 函数
   - 去掉 ANSI 码 + 去掉时间戳前缀
   - 用到的：`re.compile()`, `re.sub()`, `str.strip()`
2. 写一个函数：解析 OSS 版本列表（模拟 `get_oss_versions()` 的逻辑）
   - 输入：一段文本，每行一个文件名（如 `01-20250430-26798-BUG_x86_64.tar.gz`）
   - 输出：按序号排序的版本列表

#### Day 5-6：subprocess + 异常处理

**练习任务**：
1. 重写 `_run_and_stream()` 的简化版
   - 用 Popen 跑一个 `for i in range(10); do echo $i; sleep 1; done` 的脚本
   - 逐行读取输出并打印
   - 用到的：`subprocess.Popen`, `for line in iter(...)`, `try/except`
2. 写一个函数：执行某个命令，超时 60 秒，失败抛异常
   - 用到的：`subprocess.run`, `timeout=`, `check=True`

#### Day 7：生成器 + SSE

**练习任务**：
1. 手写一个 SSE generator
   - `def generate(): for i in range(10): yield f"data: {i}\n\n"; time.sleep(1)`
   - 用 Flask `Response(generate(), mimetype='text/event-stream')` 测试
2. 重写 `get_task_status()` + `update_task_status()` 函数（mock Redis）

### 第二周：项目核心逻辑

#### Day 8-9：Redis 操作

**练习任务**：
1. 重写所有 `*_key()` 函数（就是字符串拼接，热身）
2. 手写 `try_acquire_build_slot()` + `release_build_slot()` 的逻辑
   - 理解 INCR/DECR 原子性
   - 理解为什么要检查负数

#### Day 10-11：run_build_task() 简化版

**练习任务**：
1. 把 `run_build_task()` 的核心流程手写一遍：
   ```python
   def simple_build(task_id, version_from, version_to):
       # 1. 创建任务目录
       # 2. 检查依赖
       # 3. 调用 shell 脚本（可以用 sleep 10 模拟）
       # 4. 验证结果
       # 5. 打包
       # 6. 更新状态
       # 7. 清理临时文件
   ```
2. 在每一步加上异常处理和日志

#### Day 12-13：worker.py 简化版

**练习任务**：
1. 手写一个简易 Worker：
   ```python
   def simple_worker():
       while True:
           task = redis.brpoplpush(queue, processing, timeout=5)
           if not task: continue
           try:
               process(task)
           finally:
               redis.lrem(processing, 1, task)
   ```
2. 加一个心跳线程，每 5 秒更新一次 TTL

### 第三周：项目整合 + 面试准备

#### Day 15-16：把 auto_packing 代码完整抄一遍
- 不用完全一样，能跑通核心流程即可
- 重点理解每个函数的职责

#### Day 17-18：画架构图 + 准备话术
- 白板能画出：浏览器 → Flask → Redis → Worker → Shell 脚本的调用链
- 每个组件之间传什么数据，怎么保证可靠性

#### Day 19-20：模拟面试
- 对着本文档"第六节：6 个设计决策"自己讲一遍
- 准备踩坑故事（至少 3 个）：
  1. builds:active 计数泄漏 → 怎么发现、怎么修
  2. SSE 断连前端不重连 → 加重连机制
  3. 路径穿越攻击 → 加四重校验
  4. rstrip('_x86_64') 字符集剥离 → 改用 re.sub 后缀匹配
  5. subprocess 无 timeout → 脚本卡死 → 加超时保护
  6. 并发任务写同一个文件 → 任务独立目录 + 独立文件

---

## 八、自测清单

完成练习后，看这些问题能不能答上来：

### Python 基础
- [ ] dict 和 list 的查找时间复杂度分别是多少？
- [ ] `with open(...)` 比 `f = open(...)` 好在哪里？
- [ ] `try/except/finally` 中，如果 try 里 return 了，finally 还会执行吗？
- [ ] 装饰器的作用是什么？能手写一个简单的吗？
- [ ] 生成器和迭代器有什么区别？

### subprocess 相关
- [ ] `subprocess.run()` 和 `subprocess.Popen()` 的区别？
- [ ] 怎么实现"边执行边读取输出"？
- [ ] `timeout` 参数抛什么异常？怎么处理？
- [ ] `stdout=subprocess.PIPE, stderr=subprocess.STDOUT` 是什么意思？

### Redis 相关
- [ ] `INCR` 是原子的吗？为什么？
- [ ] `BRPOPLPUSH` 的语义是什么？
- [ ] Redis 的 `SCAN` 和 `KEYS` 命令有什么区别？
- [ ] TTL 过期后，Redis 什么时候删除 key？

### 项目设计
- [ ] 为什么用 Redis 队列不用 RabbitMQ？
- [ ] Worker 崩溃了任务怎么办？
- [ ] Web 崩溃了任务怎么办？
- [ ] 并发任务的文件冲突怎么避免？
- [ ] SSE 和 WebSocket 的区别？

---

## 九、下一步行动

1. **今天**：通读本文档，把不理解的概念记下来
2. **明天起**：按"第七节 练习路线图"开始写代码
3. **遇到问题**：先查 Python 官方文档 → 再问 AI → 再回来看本文档
4. **每完成一个练习**：在对应 checkbox 打勾，记录遇到的问题

记住原则：**不要看视频、不要看书、直接写代码、卡住了再查**。
