# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DeepFlow 补丁包构建与分发工具。从"单进程 Flask"演进为 **Flask Web + Redis 队列 + 独立 Worker** 的异步构建架构。支持两种构建模式：
- **V6**：选择起止补丁版本，自动计算镜像差异并打包
- **V7**：手动输入镜像清单，直接拉取打包

## How to run

```bash
# 本地开发（需要 Redis）
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
# Terminal 1: Flask API
python app.py                        # 0.0.0.0:8000, debug/threaded
# Terminal 2: 构建 worker
python worker.py
```

生产环境使用 gunicorn（见 Dockerfile:60）:
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
- `worker.py:process_task()` 中 `try_acquire_build_slot()` 用 `INCR builds:active` 做原子计数
- 超限时任务保持 `queued` 状态，worker 循环重试
- 构建结束（成功/失败）后 `release_build_slot()` 在 finally 中调用
- Worker 心跳 TTL 15s，失活后 `reclaim_loop` 将其 processing 队列任务回收到 build queue

### Redis key 设计

| Key pattern | 类型 | 用途 | TTL |
|---|---|---|---|
| `auto_packing:task:<id>:meta` | string (JSON) | 任务状态、进度、包路径 | success: 7200s, failure: 3600s |
| `auto_packing:task:<id>:logs` | list (JSON strings) | 前端日志条目，最多100条 | 同 meta |
| `auto_packing:task:<id>:slot` | string | 构建槽位标记 | 任务结束后删除 |
| `auto_packing:builds:active` | string (int) | 当前活跃构建数 | 永久 |
| `auto_packing:build:queue` | list | 待消费的构建任务 | 永久 |
| `auto_packing:build:processing:<worker>` | list | worker 正在处理的任务 | 跟随 worker |
| `auto_packing:worker:<worker>:heartbeat` | string (int) | worker 最后心跳时间戳 | 15s |

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
| `/versions?series=v6` | GET | OSS 补丁版本列表（v7 返回 501） |
| `/build?series=v6&current=&target=` | GET | V6 入队 + SSE 流 |
| `/build?series=v7&images=` | GET | V7 入队 + SSE 流 |
| `/task-status/<task_id>` | GET | 查询任务状态（含日志） |
| `/download/<task_id>` | GET | 下载构建产物（检查过期） |
| `/existing-packages` | GET | 列出 upgrade_packages/ 下所有 .tar.gz |
| `/download-existing/<filename>` | GET | 下载历史包（路径穿越校验） |

### Frontend (`templates/index.html`, 1828 lines)

单页工作台，三个区域：
- **顶部 Hero**：标题 + 系统概览（当前模式、已有包数量）
- **左侧工作区**（`main.workspace`）：
  - V6/V7 下拉切换（`#seriesModeSelect`），切换时改变 `body[data-series]` 触发不同配色主题
  - V6 Panel：两个可搜索版本选择器（原生 `<select>` + `fetchVersions()`）
  - V7 Panel：`<textarea>` 输入镜像列表
  - 任务状态区：进度条 + 日志 + 下载按钮 / 空闲占位
- **右侧资源区**（`aside.resource-panel`）：已有升级包列表 + 搜索 + 刷新

关键前端逻辑：
- `currentSeries` 全局变量跟踪当前模式
- 版本选择：`fetchVersions('v6')` / V7 切换时清空
- 构建：`EventSource` 连接 `/build?...`，`handleBuildStatus()` 处理 SSE 消息
- 日志去重：`lastLogMessage` 变量避免重复渲染
- 图片列表解析：`v7ImageList` 支持换行/逗号/中文逗号/分号分割

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
  max_logs: 100
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

### 仍存在的问题

- **V6 版本选择器**仍用原生 `<select>`，100+ 版本时无法搜索
- **无升级预览面板** — 选版本后不显示中间版本数/镜像数/预计大小
- **已有包无智能匹配** — 不提示"该路径已有现成包"
- **无取消构建按钮** — 任务提交后无法中止
- **`pull_save.sh:83`** 硬编码凭据（内网环境，暂不修复）
- **`debug=True`** 在生产代码中 (`app.py:954`)
- **无 OSS 版本缓存** — 每次 API 调用都执行 `ossutil ls`
- **无 `/health` 端点** — K8s 探针用 `/` 渲染完整 HTML
- **`upgrade_packages/` 无自动清理**
- **PVC 名拼写错误**: `auto-pancking-pvc`

## Directory structure (deploy branch)

```
.
├── app.py              # Flask API + 构建逻辑 + Redis 状态管理
├── worker.py           # Redis 队列消费者 + 心跳 + 任务回收
├── config.yaml         # Redis 配置
├── dockerfile          # 容器镜像 (python:3.6-slim, gunicorn)
├── entrypoint.sh       # 容器启动：克隆 nuwa + 生成 ossutilconfig
├── requirements.txt    # Flask + gunicorn + redis + PyYAML + dataclasses
├── .gitignore
├── bin/
│   ├── get_patch_image_tag_list.sh  # V6: 版本差异→镜像清单
│   ├── pull_save.sh                 # 拉取镜像→保存 tar
│   ├── deepflow_patch_upgrade.sh    # 部署端升级（打进包里）
│   ├── ossutil                      # 二进制
│   └── nerdctl                      # 二进制
├── templates/
│   └── index.html       # 单页工作台（1828行）
├── static/              # Bootstrap 5 + FontAwesome
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
- **maintenance-redis**: Redis 7.2-alpine, 密码认证, emptyDir 存储
- **maintenance-app**: Web API (maintenance-app:v1.7.3), gunicorn 2 workers, NodePort 30090
- **maintenance-worker**: 2 replicas, 同镜像但启动 `python worker.py`
- **maintenance-app-config**: ConfigMap 挂载 `/app/config/config.yaml`
- **maintenance-app-secrets**: Redis 密码
- 两者均挂载 `containerd.sock`、SSH key、共享 PVC
