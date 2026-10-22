# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DeepFlow 补丁包构建与分发工具。从"单进程 Flask"演进为 **Flask Web + Redis 队列 + 独立 Worker** 的异步构建架构。支持两种构建模式：
- **V6**：选择起止补丁版本，自动计算镜像差异并打包
- **V7**：手动输入镜像清单，直接拉取打包

## How to run

```bash
# 本地开发（需要 Python 3.12 与 Redis）
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
# Terminal 1: Flask API
python app.py                        # 0.0.0.0:8000, debug/threaded
# Terminal 2: 构建 worker
python worker.py
```

生产环境使用 gunicorn（见 Dockerfile CMD）:
```bash
gunicorn --workers 2 --threads 4 --worker-class gthread --bind 0.0.0.0:8000 --timeout 0 app:app
```

Docker:
```bash
docker build -f dockerfile -t auto-packing .
docker run -p 8000:8000 \
  -e REDIS_PASSWORD=... -e OSS_ACCESS_KEY_ID=... -e OSS_ACCESS_KEY_SECRET=... -e OSS_ENDPOINT=... \
  -v /path/to/ssh:/root/.ssh:ro \
  auto-packing
```

## Architecture

### Process model

```
浏览器 ──SSE──▶ app.py (Flask, gunicorn) ──LPUSH──▶ Redis build:queue
                    │                                    │
                    │                              BRPOPLPUSH
                    │                                    │
                    ▼                                    ▼
              Redis 读写状态                        worker.py (独立进程)
              - task:<id>:meta                     - heartbeat_loop() 每5s写心跳
              - task:<id>:logs                     - reclaim_loop() 每30s回收失活worker任务
              - builds:active                      - process_task() → run_build_task[_v7]()
              - build:queue
              - build:processing:<worker>
              - worker:<worker>:heartbeat
```

**关键设计决策**：Web 进程（app.py）只负责入队和状态查询，不执行构建。构建由 worker.py 消费 Redis 队列执行。这使 Web 和 Worker 可以独立扩缩。

### 并发控制

- `max_concurrent_tasks: 3`（config.yaml）控制全局最大并发构建数
- `worker.py:process_task()` 中 `try_acquire_build_slot()` 以 Redis 中的 `task:<id>:slot` 作为锁：
  先 `SET NX` 抢占 slot，再统计现存 slot 数量，超过上限则释放并重试
- slot 带 6 小时 TTL 兜底，worker 异常退出不会永久占用并发额度
- `builds:active` 仅作为观测值，由 slot 数量同步，不再作为准入依据
- 超限时任务保持 `queued` 状态，worker 循环重试
- 构建结束（成功/失败）后 `release_build_slot()` 在 finally 中调用
- 每个 worker 进程串行处理任务，实际并发上限 = `min(max_concurrent_tasks, worker 副本数)`；当前 2 个副本，适合 1~2 个用户同时构建
- Worker 心跳 TTL 60s（每 10s 写一次），失活后 `reclaim_loop` 将其 processing 队列任务回收到 build queue

### Redis key 设计

| Key pattern | 类型 | 用途 | TTL |
|---|---|---|---|
| `auto_packing:task:<id>:meta` | string (JSON) | 任务状态、进度、包路径 | success: 7200s, failure: 3600s |
| `auto_packing:task:<id>:logs` | list (JSON strings) | 前端日志条目，最多100条 | 同 meta |
| `auto_packing:task:<id>:slot` | string | 构建槽位标记 | 任务结束后删除 |
| `auto_packing:builds:active` | string (int) | 活跃构建数观测值（由 slot 数量同步） | 永久 |
| `auto_packing:build:queue` | list | 待消费的构建任务 | 永久 |
| `auto_packing:build:processing:<worker>` | list | worker 正在处理的任务 | 跟随 worker |
| `auto_packing:worker:<worker>:heartbeat` | string (int) | worker 最后心跳时间戳 | 60s |

### Build pipeline

#### V6: `run_build_task(task_id, current_version, target_version)`

1. 创建任务目录 `images/<current>_to_<target>_<task_id>/`
2. 依赖检查 → 验证 `get_patch_image_tag_list.sh` 和 `pull_save.sh` 存在
3. **刷新 OSS 缓存**：调用 `get_oss_versions()` 写 `.trash/oss_patch_version.txt`（worker Pod 不能依赖 app Pod 的本地文件）
4. 执行 `get_patch_image_tag_list.sh <current> <target> <output_path>`（传3个参数，第3个是任务专属输出路径）
5. 执行 `pull_save.sh -d <task_dir> -f <patch_list_file>`（nerdctl pull → save tar）
6. 打包 `tar -czf --transform s/.*\///`：镜像.tar + 清单 + `deepflow_patch_upgrade.sh`
7. 产物：`upgrade_packages/deepflow_patch_v669_<from_seq>_<to_seq>.tar.gz`

#### V7: `run_build_task_v7(task_id, images)`

与 V6 的区别：
- **跳过** OSS 版本列表和 `get_patch_image_tag_list.sh`
- 直接将 images 数组写入 `patch_image_tag_list.txt`
- `pull_save.sh` 支持两种格式：`name_tag: vX.Y.Z` 和 `name: vX.Y.Z`
- 产物：`upgrade_packages/deepflow_patch_v7_<task_id>.tar.gz`
- 前端通过 `GET /build?series=v7&images=img1,img2,...` 提交（逗号分隔）

### API routes

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | 前端工作台页面 |
| `/health` | GET | 轻量健康检查（Redis ping，供 K8s 探针） |
| `/versions?series=v6` | GET | OSS 补丁版本列表（v7 返回 501） |
| `/build` | POST | V6/V7 入队（JSON），返回 task_id；前端统一走此入口 |
| `/build/<task_id>/cancel` | POST | 取消排队中/执行中的任务 |
| `/build?task_id=` | GET | 观察已有任务的 SSE 流（重连不重复入队） |
| `/build?series=v6&current=&target=` | GET | V6 入队 + SSE 流（兼容旧调用） |
| `/build?series=v7&images=` | GET | V7 入队 + SSE 流（兼容旧调用） |
| `/task-status/<task_id>` | GET | 查询任务状态（含日志） |
| `/download/<task_id>` | GET | 下载构建产物（检查过期） |
| `/existing-packages` | GET | 列出 upgrade_packages/ 下所有 .tar.gz |
| `/download-existing/<filename>` | GET | 下载历史包（路径穿越校验） |

### Frontend (`templates/index.html` + `static/app.css` + `static/app.js`)

单页工作台，结构、样式、逻辑已拆分：
- 模板只保留 DOM 结构；样式在 `static/app.css`，逻辑在 `static/app.js`（`defer` 加载）。
- 顶部 Header：`h1` 标题 + V6/V7 `radiogroup`，切换时改变 `body[data-mode]` 触发不同配色主题。
- 工作区（`main.workspace`）：
  - V6 Panel：两个可搜索版本选择器（自定义 combobox + `fetchVersions()`），左右双列布局
  - V7 Panel：`<textarea>` 输入镜像列表，实时校验 `name:tag`、去重并提示非法行
  - 构建卡：进度条 + 状态行 + 可折叠日志；空闲态紧凑显示
- 右侧资源区（`aside.packages`）：已有升级包列表 + 搜索

关键前端逻辑：
- `currentSeries` 跟踪当前模式；`setMode()` 同步单选按钮、面板与主题
- 构建统一为 `POST /build` 拿 `task_id`，再用 `EventSource` 订阅 `/build?task_id=...`；重连不会重复入队
- `task_id` 写入 URL，刷新页面自动恢复任务
- 日志按 `lastLogSeq` 增量渲染，使用服务端时间戳；仅在接近底部时自动跟随
- 包名匹配使用 `/versions` 返回的 `package_name_template`，不在前端硬编码 `v669`

### Shell scripts

| Script | Role | Key changes from master |
|---|---|---|
| `bin/get_patch_image_tag_list.sh` | V6: 从 nuwa 仓库计算镜像差异 | 接受第3个参数指定输出路径；用 `mktemp` + `trap cleanup EXIT` 管理临时文件 |
| `bin/pull_save.sh` | 登录仓库 → 拉取镜像 → 保存 tar | **⚠️ 硬编码凭据**（line 83），master 用 env vars |
| `bin/deepflow_patch_upgrade.sh` | 部署端升级脚本（打进包里） | 未变化 |

### Configuration

`config.yaml`（可通过 `APP_CONFIG_PATH` 环境变量覆盖路径）:
```yaml
redis:
  host: maintenance-redis.getpatch.svc.cluster.local
  port: 6379
  password: ""              # 运行时由 REDIS_PASSWORD 环境变量覆盖
  key_prefix: auto_packing
  success_ttl_seconds: 7200
  failure_ttl_seconds: 3600
  download_ttl_seconds: 90
  max_logs: 2000
  max_concurrent_tasks: 3
```

## Key environment variables

| Variable | Consumer | Purpose |
|---|---|---|
| `REDIS_PASSWORD` | app.py | Redis 认证（覆盖 config.yaml 中的 password） |
| `APP_CONFIG_PATH` | app.py, worker.py | config.yaml 路径，默认 `/app/config.yaml` |
| `OSS_ACCESS_KEY_ID/SECRET/ENDPOINT` | entrypoint.sh | 生成 ossutil 配置 |
| `OSS_REGION` | entrypoint.sh | OSS 区域，默认 cn-beijing |
| `GUNICORN_WORKERS/GUNICORN_THREADS` | Dockerfile CMD | gunicorn 配置 |
| `PULL_TIMEOUT_SECONDS` | app.py | 镜像拉取超时，默认 7200s |
| `ARCHIVE_TIMEOUT_SECONDS` | app.py | 打包超时，默认 1800s |
| `OSS_QUERY_TIMEOUT_SECONDS` | app.py | `ossutil ls` 超时，默认 120s |
| `SLOT_TTL_SECONDS` | app.py | 构建槽位兜底 TTL，默认 21600s |
| `OSS_VERSIONS_CACHE_TTL_SECONDS` | app.py | `/versions` 进程内缓存，默认 300s |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` | worker.py | 心跳间隔，默认 10s |
| `WORKER_HEARTBEAT_TTL_SECONDS` | worker.py | 心跳 TTL，默认 60s |
| `WORKER_RECLAIM_INTERVAL_SECONDS` | worker.py | 失活任务回收扫描间隔，默认 30s |
| `FLASK_DEBUG` | app.py `__main__` | 设为 `1` 才启用 debug |
| `REGISTRY_USERNAME/PASSWORD` | ❌ **已移除** | `pull_save.sh` 现在硬编码凭据 |

## Known issues

> 完整清单见 [ISSUES.md](./ISSUES.md)。已修复的问题标记为 ✅。

### 已修复 (本轮)

- ✅ `rstrip('_x86_64')` 字符集剥离 → `re.sub` 后缀匹配 (`app.py:268`)
- ✅ subprocess 无 timeout → 脚本 600s / pull 3600s / tar 1800s
- ✅ SSE generator 无异常处理 → try/except GeneratorExit + Exception
- ✅ `update_progress` 100 次 Redis 写 → 直接跳到目标百分比
- ✅ `builds:active` 计数泄漏 → `reconcile_build_counter()` + 回收时自动修复
- ✅ `process_task` 无限等待 → 10 分钟超时
- ✅ `get_existing_packages` 文件删除竞态 → try/except FileNotFoundError
- ✅ `schedule_task_cleanup` 全量读写 → 直接用 `expire()`
- ✅ shebang `/bin/env` → `/usr/bin/env bash`
- ✅ 硬编码 v6.6 → 环境变量 `NUWA_MAJOR/MINOR/TAG_PREFIX`
- ✅ `grep -P` PCRE 依赖 → `sed -n` 替代
- ✅ rollback `ls -t` pipefail 崩溃 → `find | sort -r`
- ✅ rollback 部分状态丢失 → `cp -f` 原子恢复
- ✅ `sed` 中 `.` 未转义 → 转义 image 名中的点
- ✅ V7 GET URL 超长 → `POST /build` JSON 入队 + `GET /build?task_id=` SSE 观察
- ✅ git pull 失败静默继续 → 阻断流程 `exit 1`
- ✅ import_images 无 tar 静默成功 → 显式报错
- ✅ 镜像名纯净化冲突 → 保存前检查文件是否已存在
- ✅ 前端 SSE 断连不重试 → 指数退避重连 (3 次)
- ✅ 前端 fetchVersions 竞态 → `currentSeries` 守卫
- ✅ 前端构建失败后 UI 不恢复 → 显示 idle 面板
- ✅ 前端 `user-scalable=no` → 移除限制
- ✅ 前端标题层级 h1→h3 → h1→h2→h5
- ✅ 前端 sanitizeLogMessage 误删内容 → 只移除末尾 `(XX%)`
- ✅ 前端搜索无 debounce → `debounce(fn, 200)`
- ✅ 前端 `prefers-reduced-motion` → 添加媒体查询
- ✅ 前端 beforeunload 警告 → 构建中拦截关闭
- ✅ `_run_and_stream` 无超时 → 拉取超时后强杀子进程（默认 2h，`PULL_TIMEOUT_SECONDS` 可调）
- ✅ V6 版本参数未校验 → 拒绝路径穿越字符（`_is_safe_version_value`）
- ✅ 升级包直接写入目标文件 → 先写临时文件再 `os.replace` 原子替换
- ✅ OSS 版本缓存非原子写入 → 临时文件 + 原子替换；`ossutil ls` 增加 120s 超时
- ✅ 并发计数漂移 → 以 slot key 为准准入，slot 带 TTL
- ✅ worker 收到非法队列消息会崩溃 → 跳过并告警
- ✅ `debug=True` 硬编码 → 由 `FLASK_DEBUG=1` 控制
- ✅ 缺少 `/health` → 新增轻量健康检查，K8s 探针已切换
- ✅ SSE 全量推送日志 → 日志带 `seq`，支持 `?since=<seq>` 增量推送，前端按游标重连
- ✅ V6 原生 `<select>` 不可搜索 → 改为自定义可搜索下拉，支持键盘操作
- ✅ 无取消构建按钮 → 任务执行中可在前端取消，worker 1s 内终止进程组
- ✅ 前端构建失败时引用未定义变量 `bl` 导致报错 → 修正为直接操作日志面板

### 仍存在的问题

- **V6 版本选择器**仍用原生 `<select>`，100+ 版本时无法搜索
- **升级预览不完整** — 已显示跨越 patch 数；镜像数与预计包大小需后端统计
- **`pull_save.sh:84`** 硬编码凭据（内网环境，暂不修复）
- **无 OSS 版本缓存** — 每次 API 调用都执行 `ossutil ls`
- **SSE 长连接占用 gunicorn 线程** — 按当前 1~2 个用户的规模可接受（2 workers × 4 threads 足够），暂不改造
- **`upgrade_packages/` 无自动清理**
- **PVC 名拼写错误**: `auto-pancking-pvc`

## Directory structure (deploy branch)

```
.
├── app.py              # Flask API + 构建逻辑 + Redis 状态管理
├── worker.py           # Redis 队列消费者 + 心跳 + 任务回收
├── config.yaml         # Redis 配置
├── dockerfile          # 容器镜像 (python:3.12-slim-bookworm, gunicorn)
├── entrypoint.sh       # 容器启动：克隆 nuwa + 生成 ossutilconfig
├── requirements.txt    # Flask + gunicorn + redis + PyYAML
├── .gitignore
├── bin/
│   ├── get_patch_image_tag_list.sh  # V6: 版本差异→镜像清单
│   ├── pull_save.sh                 # 拉取镜像→保存 tar
│   ├── deepflow_patch_upgrade.sh    # 部署端升级（打进包里）
│   ├── redeploy.sh                  # 构建唯一 tag 镜像并滚动更新 K8s
│   ├── ossutil                      # 二进制
│   └── nerdctl                      # 二进制
├── templates/
│   └── index.html       # 单页结构
├── static/              # app.css / app.js / favicon
├── apply/
│   ├── maintenance-app-deployment.yaml  # K8s: app + worker + redis + config
│   └── persistent-volume.yaml
├── latest_image_list/   # 镜像清单输出
├── images/              # 构建临时目录
├── upgrade_packages/    # 产物 .tar.gz
├── logs/                # app.log + task 日志
├── .trash/              # OSS 版本缓存 + 脚本临时文件
├── nuwa/                # 运行时 git clone（不在仓库中）
└── test/                # 旧版代码快照（非正式测试）
```

## K8s deployment (from apply/maintenance-app-deployment.yaml)

Namespace `getpatch` 内包含：
- **maintenance-redis**: Redis 7.2-alpine, 密码认证, AOF 持久化 PVC
- **maintenance-app**: Web API (maintenance-app:v1.7.3), gunicorn 2 workers, NodePort 30090
- **maintenance-worker**: 2 replicas, 同镜像但启动 `python worker.py`
- **maintenance-app-config**: ConfigMap 挂载 `/app/config/config.yaml`
- **maintenance-app-secrets**: Redis 密码
- 两者均挂载 `containerd.sock`、SSH key、共享 PVC
