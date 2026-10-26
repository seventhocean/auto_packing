# AGENTS.md

本文件面向所有 AI 编程助手和开发者，不绑定特定工具。修改代码前请先阅读本文件和
[`ISSUES.md`](./ISSUES.md)；containerd 相关问题另外参考
[`docs/CONTAINERD_RUNBOOK.md`](./docs/CONTAINERD_RUNBOOK.md)。

## 1. 项目概览

DeepFlow 补丁包构建与分发工具。当前架构：

- `app.py`：Flask Web/API、任务入队、SSE 状态流、任务中心、下载接口。
- `worker.py`：独立 Redis 队列消费者，执行镜像拉取、保存和打包。
- `templates/index.html` + `static/app.css` + `static/app.js`：单页工作台。
- Redis：任务状态、日志、队列、槽位、worker 心跳。
- Shell 脚本：版本差异计算、镜像拉取、部署端升级脚本。

两种构建模式：

- **V6**：选择起止补丁版本，自动计算中间 patch 的镜像差异并打包。
- **V7**：手工输入镜像清单，校验、去重后直接打包。

当前使用规模为 1~2 个用户，**稳定优先**。没有明确需求时，不要大改构建主链路、
队列模型或前端结构。

## 2. 快速运行

```bash
# 本地开发（Python 3.12 + Redis）
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Terminal 1: Web API，0.0.0.0:8000
python app.py

# Terminal 2: 构建 worker
python worker.py
```

生产使用 gunicorn（见 Dockerfile CMD）：

```bash
gunicorn --workers 2 --threads 4 --worker-class gthread \
  --bind 0.0.0.0:8000 --timeout 0 app:app
```

Docker 构建：

```bash
docker build -f dockerfile -t auto_packing:latest .
```

只启动 `app.py` 不启动 `worker.py` 时，页面和入队正常，但任务不会执行。

## 3. 架构与并发

```text
浏览器 ──SSE──▶ app.py (Flask/gunicorn) ──LPUSH──▶ Redis build:queue
                    │                                  │ BRPOPLPUSH
                    │                                  ▼
                    │                           worker.py (独立进程)
                    ▼                                  │
             Redis 状态/日志/槽位                pull/build/package
```

关键设计：

- Web 进程只负责入队、状态查询和展示，不执行构建。
- 每个 worker 进程串行处理任务，实际并发上限 =
  `min(max_concurrent_tasks, worker 副本数)`。
- 当前 `max_concurrent_tasks: 1`，单节点同一时刻只跑一个构建，避免并行拉镜像
  打满磁盘和网络。
- 槽位以 `task:<id>:slot` 为准：`SET NX` 抢占后统计 slot 数量，超限则释放重试。
- worker 心跳间隔 10s，TTL 60s；失活任务每 30s 回收一次。
- 回收流程带 `build:reclaim:lock`（120s TTL）互斥锁，回收前会删除失效 worker
  对应任务的 slot。这个清理不能删掉，否则单并发下旧 slot 会阻塞后续任务。

## 4. Redis key 设计

| Key | 类型 | 用途 | TTL |
|---|---|---|---|
| `auto_packing:task:<id>:meta` | string(JSON) | 任务状态、进度、包信息 | 成功 7200s / 失败 3600s |
| `auto_packing:task:<id>:logs` | list(JSON) | 前端日志，最多 `max_logs` 条 | 同 meta |
| `auto_packing:task:<id>:log_seq` | string(int) | 日志增量游标 | 同 meta |
| `auto_packing:task:<id>:cancel` | string | 取消标记 | 失败 TTL |
| `auto_packing:task:<id>:slot` | string | 构建槽位 | 6h |
| `auto_packing:builds:active` | string(int) | 活跃构建数观测值 | 永久 |
| `auto_packing:build:queue` | list | 待消费任务 | 永久 |
| `auto_packing:build:processing:<worker>` | list | worker 正在处理的任务 | 跟随 worker |
| `auto_packing:worker:<worker>:heartbeat` | string(int) | 最后心跳时间戳 | 60s |
| `auto_packing:build:reclaim:lock` | string | 回收互斥锁 | 120s |

## 5. 构建流程

### V6

1. `POST /build` 入队，返回 `task_id`；前端订阅 `GET /build?task_id=...&since=...`。
2. worker 创建任务目录，刷新 OSS 版本缓存。
3. `bin/get_patch_image_tag_list.sh` 计算 patch 差集并生成镜像清单。
4. `bin/pull_save.sh` 登录仓库、拉取镜像、保存为 `.tar`。
5. 打包镜像 + 清单 + `deepflow_patch_upgrade.sh`，产物：
   `upgrade_packages/deepflow_patch_v669_<from>_<to>.tar.gz`。
6. 清理临时目录，更新任务状态。

### V7

1. 前端校验、去重镜像清单，`POST /build` 入队。
2. worker 直接写入任务专属 `patch_image_tag_list.txt`。
3. 后续拉取、保存、打包与 V6 一致，产物：
   `upgrade_packages/deepflow_patch_v7_<task_id>.tar.gz`。

打包使用临时文件 + `os.replace` 原子替换；拉取超时默认 2h、打包超时 30min。
不要在未测试的情况下改变这些超时或原子写入逻辑。

## 6. API

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | 工作台页面 |
| `/health` | GET | 轻量健康检查（Redis ping） |
| `/versions?series=v6` | GET | OSS 补丁版本列表（v7 返回 501） |
| `/build` | POST | V6/V7 入队（JSON），返回 `task_id` |
| `/build?task_id=&since=` | GET | 订阅已有任务 SSE（重连不重复入队） |
| `/build/<task_id>/cancel` | POST | 取消排队中/执行中的任务 |
| `/task-status/<task_id>?since=` | GET | 查询状态与增量日志 |
| `/tasks/overview` | GET | 任务中心：进行中/最近完成 + Worker/队列/磁盘状态 |
| `/download/<task_id>` | GET | 下载本次构建产物（临时链接） |
| `/existing-packages` | GET | 列出历史升级包 |
| `/download-existing/<filename>` | GET | 下载历史包（路径校验） |

## 7. 前端

- 模板只保留 DOM；样式在 `static/app.css`，逻辑在 `static/app.js`（`defer`）。
- 顶部 `h1` + V6/V7 radiogroup，切换写入 `?series=`。
- V6：双列可搜索版本选择器，目标版本按 `seq_num` 过滤，显示跨越 patch 数和同名包提示。
- V7：镜像胶囊流 + 批量编辑弹窗；实时校验 `name:tag`，去重并提示非法项。
- 构建卡：流水线步骤条、进度、可折叠日志、取消、下载/返回任务中心。
- 构建统一 `POST /build` 拿 `task_id`，再用 `EventSource` 订阅；`task_id` 写入 URL，
  刷新后可恢复。
- 日志按 `lastLogSeq` 增量追加，仅接近底部时自动跟随。
- 任务中心每 5 秒轮询 `/tasks/overview`，支持查看/取消进行中任务。

## 8. Shell 脚本

| 脚本 | 作用 |
|---|---|
| `bin/get_patch_image_tag_list.sh` | V6：从 nuwa 计算镜像差异；`git pull` 重试 3 次 |
| `bin/pull_save.sh` | 登录仓库 → pull → save；容器命令用数组保存，失败重试 3 次 |
| `bin/deepflow_patch_upgrade.sh` | 部署端升级脚本（打进包内，目标环境写死） |
| `bin/redeploy.sh` | 唯一 tag 构建、可选推送/导入、同步 ConfigMap/YAML tag、滚动更新 |
| `bin/check_containerd.py` | 只读巡检 default 残留与镜像 layer 完整性 |

`pull_save.sh` 中仍有硬编码仓库凭据，内网环境暂不处理。

## 9. 配置与环境变量

`config.yaml`（可通过 `APP_CONFIG_PATH` 覆盖）：

```yaml
redis:
  host: maintenance-redis.getpatch.svc.cluster.local
  port: 6379
  db: 0
  password: ""
  key_prefix: auto_packing
  success_ttl_seconds: 7200
  failure_ttl_seconds: 3600
  download_ttl_seconds: 90
  max_logs: 2000
  max_concurrent_tasks: 1
```

| 变量 | 用途 |
|---|---|
| `REDIS_PASSWORD` | Redis 密码，覆盖 config.yaml |
| `APP_CONFIG_PATH` | config.yaml 路径，默认 `/app/config.yaml` |
| `OSS_ACCESS_KEY_ID/SECRET/ENDPOINT` | entrypoint 生成 ossutil 配置 |
| `OSS_REGION` | OSS 区域，默认 cn-beijing |
| `GUNICORN_WORKERS/THREADS` | gunicorn 配置 |
| `PULL_TIMEOUT_SECONDS` | 镜像拉取超时，默认 7200s |
| `ARCHIVE_TIMEOUT_SECONDS` | 打包超时，默认 1800s |
| `OSS_QUERY_TIMEOUT_SECONDS` | `ossutil ls` 超时，默认 120s |
| `OSS_VERSIONS_CACHE_TTL_SECONDS` | `/versions` 缓存，默认 300s |
| `SLOT_TTL_SECONDS` | 槽位兜底 TTL，默认 21600s |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` | 心跳间隔，默认 10s |
| `WORKER_HEARTBEAT_TTL_SECONDS` | 心跳 TTL，默认 60s |
| `WORKER_RECLAIM_INTERVAL_SECONDS` | 回收扫描间隔，默认 30s |
| `CONTAINERD_NAMESPACE` | nerdctl 默认 namespace，镜像内为 `k8s.io` |
| `FLASK_DEBUG` | 设为 `1` 才启用 debug |

## 10. 测试与检查

```bash
python -m unittest tests.test_app_logic -v
python -m py_compile app.py worker.py bin/check_containerd.py
bash -n bin/*.sh entrypoint.sh
node --check static/app.js
```

当前有 23 个后端单测，使用 FakeRedis，不依赖真实 Redis。没有 CI，改动后请手动执行上述检查；
涉及前端时建议再用浏览器或 jsdom 做一次 smoke test。

## 11. 部署与发布

- `apply/maintenance-app-deployment.yaml` 包含 app/worker/redis/service。
- app/worker 使用同一镜像，tag 固化在清单中；发布建议使用 `bin/redeploy.sh`：
  ```bash
  NERDCTL_IMPORT=1 ./bin/redeploy.sh
  # 或
  REGISTRY=registry.example.com ./bin/redeploy.sh
  ```
- 脚本会构建唯一 tag、可选推送/导入本机 containerd、同步 ConfigMap 和 YAML tag，
  然后更新两个 Deployment 并等待 rollout。
- Redis 使用 PVC + AOF；app/worker 挂载 `/app/upgrade_packages` 共享 PVC。
- app/worker 默认 `CONTAINERD_NAMESPACE=k8s.io`；探针使用 `/health`。
- 多节点集群下 hostPath + ReadWriteOnce 没有 nodeAffinity，存在挂载/调度风险；
  当前按单节点部署理解。

## 12. 已知问题与修改建议

当前问题见 [`ISSUES.md`](./ISSUES.md)。重点注意：

- containerd `default` namespace 历史残留可能导致 `nerdctl save` 报
  `content digest not found`；先跑 `bin/check_containerd.py`，不要盲目重试 save。
- `max_concurrent_tasks=1`，回收时必须清理失效 slot；不要删除
  `reclaim_stale_tasks()` 里的 slot 清理逻辑。
- SSE 断连后前端会恢复可构建状态，旧任务可能仍在运行；改动前先确认任务中心状态。
- 构建中切换 V6/V7 会被 SSE 强制拉回；这是已知体验问题，尚未修。
- V7 校验暂不支持带端口的 registry（如 `registry:5000/image:tag`）。
- 仓库凭据、升级包清理、多节点存储等按当前策略暂不处理。

改动原则：

1. 不要在不了解现有队列/槽位/取消逻辑的情况下重构 worker。
2. 不要删除用户已有的运行时数据或未提交改动。
3. 修改后至少跑一遍第 10 节的检查。
4. 大改前先确认是否需要，当前项目稳定优先。

## 13. 目录结构

```text
.
├── app.py                 # Flask API、任务状态、构建逻辑
├── worker.py              # 队列消费、心跳、回收
├── config.yaml            # Redis 配置
├── dockerfile             # python:3.12-slim-bookworm + gunicorn
├── entrypoint.sh          # 克隆 nuwa、生成 ossutil 配置
├── requirements.txt       # Flask/gunicorn/redis/PyYAML
├── templates/index.html   # 页面结构
├── static/                # app.css / app.js / favicon
├── bin/
│   ├── get_patch_image_tag_list.sh
│   ├── pull_save.sh
│   ├── deepflow_patch_upgrade.sh
│   ├── redeploy.sh
│   ├── check_containerd.py
│   ├── ossutil
│   └── nerdctl
├── apply/
│   ├── maintenance-app-deployment.yaml
│   └── persistent-volume.yaml
├── tests/                 # 后端单测
├── docs/                  # containerd runbook
├── latest_image_list/     # 运行时镜像清单
├── images/                # 构建临时目录
├── upgrade_packages/      # 升级包产物
├── logs/                  # app.log / task 日志
├── .trash/                # OSS 版本缓存与脚本临时文件
└── nuwa/                  # 运行时 git clone（不在仓库中）
```
