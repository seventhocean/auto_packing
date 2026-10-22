# auto_packing

`auto_packing` 是一个面向 DeepFlow 补丁升级场景的自动打包服务。当前项目已经从“单进程内直接执行任务”的轻量模式，演进为“Flask Web + Redis 队列 + 独立 Worker”的异步构建架构，用于统一完成以下工作：

- 提供 Web 工作台，支持 V6 与 V7 两种构建模式。
- 从 OSS 获取可选补丁版本列表。
- 根据版本差异或手工镜像清单生成 `patch_image_tag_list.txt`。
- 拉取镜像并保存为本地 `.tar` 文件。
- 组装升级包 `tar.gz`。
- 提供实时进度、日志、临时下载入口和历史升级包下载。

## 1. 当前项目的真实架构

### 1.1 运行角色

- `app.py`
  - Flask 应用入口。
  - 提供页面、REST API、SSE 实时推送。
  - 负责任务入队、状态读写、下载接口、已有升级包列表接口。
- `worker.py`
  - 独立后台消费者。
  - 从 Redis 队列取任务并执行实际构建。
  - 维护心跳，并回收失活 worker 遗留的 processing 队列任务。
- Redis
  - 任务状态存储。
  - 构建队列和 processing 队列。
  - 并发槽位计数。
  - 日志缓存。
- Shell 脚本
  - `bin/get_patch_image_tag_list.sh`：V6 模式下根据补丁版本差异生成镜像清单。
  - `bin/pull_save.sh`：登录仓库、拉取镜像、保存为 tar。
  - `bin/deepflow_patch_upgrade.sh`：打到升级包中的部署端升级脚本。

### 1.2 与旧实现相比的重要变化

- 不再依赖 `Semaphore(3)` 这类进程内并发控制。
- 当前并发控制由 Redis 中的 `active_builds` 计数和 `max_concurrent_tasks` 配置驱动。
- Web 进程与构建执行已经解耦，Web 只负责入队和状态查询，真正构建由 `worker.py` 执行。
- 任务状态和日志不再只存在内存里，而是保存在 Redis，便于多副本部署。
- 前端界面已改为双模式工作台：
  - `V6 Patch`：自动计算补丁差异。
  - `V7 Image List`：手工输入镜像列表直接打包。

## 2. 核心流程

### 2.1 V6 构建流程

V6 模式的目标是：给定当前补丁版本和目标补丁版本，自动找出中间 patch 的镜像差异并打包。

执行顺序如下：

1. 前端调用 `POST /build`，JSON body 为 `{"series":"v6","current":"<current>","target":"<target>"}`，拿到 `task_id`。
2. `app.py` 生成唯一 `task_id`，初始化任务状态为 `queued`，并把任务写入 Redis 队列；前端随后通过 `GET /build?task_id=<task_id>` 订阅 SSE。
3. `worker.py` 从 Redis 队列取出任务，等待并发槽位可用。
4. worker 调用 `run_build_task(task_id, current_version, target_version)`。
5. 为该任务创建独立临时目录：
   - `images/<current>_to_<target>_<task_id>/`
6. 执行 `bin/get_patch_image_tag_list.sh`：
   - 先 `git pull` 更新本地 `nuwa/` 仓库。
   - 从 `.trash/oss_patch_version.txt` 中读取后端刚写入的 OSS 补丁列表。
   - 计算当前版本到目标版本之间的 patch 差集。
   - 遍历 `nuwa/6.6/6.6.9/<patch>/make.sh`，提取镜像与 tag。
   - 生成当前任务专属 `patch_image_tag_list.txt`。
7. 执行 `bin/pull_save.sh -d <task_dir> -f <patch_list>`：
   - 登录镜像仓库。
   - 逐条拉取镜像。
   - 保存为多个 `.tar` 文件。
8. 打包输出：
   - `.tar` 镜像文件
   - `patch_image_tag_list.txt`
   - `bin/deepflow_patch_upgrade.sh`
9. 产物写入 `upgrade_packages/`，文件名格式：
   - `deepflow_patch_v669_<current_patch_seq>_<target_patch_seq>.tar.gz`
10. 更新任务状态为 `complete`，通过 SSE 推送给前端。
11. 删除任务临时镜像目录和临时任务日志。

### 2.2 V7 构建流程

V7 模式不依赖 OSS 版本差异，而是直接使用用户手工输入的镜像清单。

执行顺序如下：

1. 前端调用 `POST /build`，JSON body 为 `{"series":"v7","images":["<image>", ...]}`，拿到 `task_id`。
2. `app.py` 把镜像列表写入 Redis 队列；前端随后通过 `GET /build?task_id=<task_id>` 订阅 SSE。
3. `worker.py` 取任务后执行 `run_build_task_v7(task_id, images)`。
4. worker 直接把用户提交的镜像条目写入任务专属 `patch_image_tag_list.txt`。
5. 后续拉取、保存、打包逻辑与 V6 基本一致。
6. 产物命名为：
   - `deepflow_patch_v7_<task_id>.tar.gz`

### 2.3 实时状态流

前端不是轮询 `/task-status/<task_id>`，而是通过 `/build` 返回的 SSE 流持续接收状态。

任务状态字段主要包括：

- `status`
  - `queued`
  - `progress`
  - `complete`
  - `error`
- `percent`
- `message`
- `complete`
- `error`
- `download_url`
- `package_path`
- `package_name`
- `package_size_mb`
- `download_expire_at`
- `logs`
- `cancelled`（任务被用户取消时为 `true`）

## 3. Redis 设计

### 3.1 配置来源

Redis 默认配置在 `app.py` 的 `DEFAULT_CONFIG` 中，运行时会再加载：

- `config.yaml`
- 环境变量 `REDIS_PASSWORD`

当前仓库内 `config.yaml` 默认值为：

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

### 3.2 Redis 中存的是什么

- 任务元信息
  - `auto_packing:task:<task_id>:meta`
- 任务日志列表
  - `auto_packing:task:<task_id>:logs`
- 任务占用槽位标记
  - `auto_packing:task:<task_id>:slot`
- 当前活跃构建数
  - `auto_packing:builds:active`
- 构建队列
  - `auto_packing:build:queue`
- worker processing 队列
  - `auto_packing:build:processing:<worker_id>`
- worker 心跳
  - `auto_packing:worker:<worker_id>:heartbeat`

### 3.3 并发控制与回收

- 每个 worker 真正开始执行前，会调用 `try_acquire_build_slot()`。
- 槽位以 `task:<id>:slot` 为准：先 `SET NX` 抢占，再统计现有 slot 数量；超过 `max_concurrent_tasks` 时释放并继续排队。
- slot 带 6 小时 TTL 兜底，worker 异常退出后不会永久占用并发额度。
- `builds:active` 仅作为观测值，由实际 slot 数量同步。
- 每个 worker 进程串行处理任务，实际并发上限为 `min(max_concurrent_tasks, worker 副本数)`；单节点部署下配置为 1，同一时刻只执行一个构建；其余任务保持排队，避免并行拉镜像把本机磁盘和网络打满。
- worker 通过 `heartbeat_loop()` 每 10 秒写一次心跳，TTL 为 60 秒（避免 Redis 短暂抖动导致任务被误回收）。
- 另一个 `reclaim_loop()` 每 30 秒扫描一次 processing 队列：
  - 若某个 worker 的心跳不存在，则把其 processing 队列中的任务重新放回 build queue。

这意味着项目支持：

- 多 worker 副本并发消费。
- worker 异常退出后的任务回收。
- 状态在 Web 重启后仍可继续读取。

## 4. 目录与职责

### 4.1 主目录

- `app.py`
  - Web 服务、接口、任务状态管理、SSE、打包主逻辑。
- `worker.py`
  - Redis 队列消费者。
- `config.yaml`
  - Redis 配置。
- `entrypoint.sh`
  - 容器启动入口，负责准备目录、拉取 `nuwa`、生成 `.ossutilconfig`。
- `dockerfile`
  - 容器镜像构建文件。
- `requirements.txt`
  - Python 依赖。

### 4.2 业务目录

- `bin/`
  - 关键脚本和二进制：
  - `get_patch_image_tag_list.sh`
  - `pull_save.sh`
  - `deepflow_patch_upgrade.sh`
  - `redeploy.sh`
  - `check_containerd.py`
  - `ossutil`
  - `nerdctl`
- `templates/`
  - 前端页面模板。
- `static/`
  - `app.css`、`app.js`、`favicon.ico`（图标使用内联 SVG）。
- `latest_image_list/`
  - 默认镜像清单输出目录。
- `images/`
  - 每个任务的临时镜像 tar 工作目录。
- `upgrade_packages/`
  - 最终升级包目录。
- `logs/`
  - `app.log` 与任务运行日志。
- `.trash/`
  - 临时文件和 OSS 补丁版本缓存。
- `apply/`
  - Kubernetes 部署清单。
- `tests/`
  - 后端单元测试（`python -m unittest tests.test_app_logic`）。
- `docs/`
  - 运维与巡检手册（`CONTAINERD_RUNBOOK.md`）。

### 4.3 运行时依赖目录

- `nuwa/`
  - 不在仓库中提交，运行时由 `entrypoint.sh` 自动克隆。
  - V6 模式生成镜像差异强依赖该目录。

## 5. API 说明

### 5.1 页面

- `GET /`
  - 返回工作台页面。
- `GET /health`
  - 轻量健康检查，返回 Redis 连通状态；K8s 探针使用。

### 5.2 版本列表

- `GET /versions?series=v6`
  - 从 OSS 读取补丁列表。
  - 仅 `v6` 已接入。
  - 同时返回 `package_name_template`，前端用它匹配“已有同名升级包”，避免硬编码包名前缀。
- `GET /versions?series=v7`
  - 当前返回 `501`，属于占位逻辑。

返回数据中的版本项包含：

- `value`
- `display`
- `seq_num`
- `date`

### 5.3 启动构建

- `POST /build`
  - V6：`{"series":"v6","current":"<v>","target":"<v>"}`
  - V7：`{"series":"v7","images":["<img>", ...]}`
  - 只入队并返回 `task_id`，不承载 SSE。
- `GET /build?task_id=<task_id>&since=<seq>`
  - 返回 `text/event-stream`，观察任务进度。
  - 重连只重放日志，不会重复创建任务；前端把 `task_id` 写入 URL，刷新后可恢复。

兼容旧调用：`GET /build?series=v6&current=&target=` 与 `GET /build?series=v7&images=` 仍会入队并直接返回 SSE，但新代码不应再使用。

### 5.4 任务状态查询

- `GET /task-status/<task_id>`
  - 支持 `?since=<seq>` 只返回该日志游标之后的新日志。

### 5.5 取消任务

- `POST /build/<task_id>/cancel`
  - 对排队中或执行中的任务提交取消请求。
  - 执行中的构建会在 1 秒内检测到取消并终止整个进程组。
  - 任务最终状态为 `error + cancelled=true`，前端显示“已取消”。

### 5.6 下载

- `GET /download/<task_id>`
  - 下载本次刚生成的包。
  - 会检查任务状态和 `download_expire_at`。
- `GET /tasks/overview`
  - 任务中心数据：进行中任务、最近完成任务，以及 Worker 在线、队列长度、并发槽位和磁盘余量。
  - 响应带 `Cache-Control: no-store`，前端每 5 秒轮询。
- `GET /existing-packages`
  - 列出 `upgrade_packages/` 中已有包，按修改时间倒序。
- `GET /download-existing/<filename>`
  - 下载历史包。
  - 内部会做文件名合法性校验，避免路径穿越。

## 6. 前端页面逻辑

当前前端只有一个页面：`templates/index.html`，但不是简单表单，已经演进成完整工作台。样式与逻辑分别拆分到 `static/app.css`、`static/app.js`。

### 6.1 页面分区

- 顶部 Header 区
  - `h1` 标题。
  - V6 / V7 单选式分段切换（支持键盘方向键）。
- 左侧工作区
  - V6：当前版本 → 目标版本双列选择。
  - V7：镜像清单输入与实时校验。
  - 构建卡：实时进度条、状态、可折叠日志、取消按钮。
  - 任务中心：进行中/最近完成任务、Worker 状态、队列、并发与磁盘余量。
  - 完成后可直接下载升级包，或返回任务中心。
  - 刷新页面后通过 URL 中的 `task_id` 自动恢复任务。
- 右侧资源区
  - 已有升级包列表。
  - 搜索与下载。

### 6.2 V6 交互逻辑

- 页面加载时自动请求 `/versions?series=v6`。
- 当前版本使用可搜索下拉：输入关键字过滤，支持方向键 + 回车选择。
- 目标版本只列出 `seq_num` 大于当前版本的项目。
- 选择两个版本后，表单下方显示“将跨越 N 个 patch”。
- 若 `upgrade_packages/` 中已存在同名包，会提示并给出直接下载链接。
- 按钮禁用条件：
  - 未选当前版本。
  - 未选目标版本。
  - 正在构建中。

### 6.3 V7 交互逻辑

- 用户在文本框中输入镜像列表。
- 支持按换行、逗号、中文逗号、分号拆分。
- 实时校验 `name:tag` 格式（支持 registry 路径），自动去重并提示非法行。
- 存在非法行时构建按钮禁用，避免把脏数据写入镜像清单。

注意：

- 前端推荐格式是：
  - `deepflow-agent_tag: v6.6.5617`
  - `deepflow-server_tag: v6.6.5617`
- 但实际提交给后端时，是以分隔符切成单条字符串数组再传递。
- 后端不会再次做严格语法校验，真正格式兼容主要依赖 `pull_save.sh` 的解析规则。

### 6.4 日志显示逻辑

- 每条日志带单调递增的 `seq` 游标。
- SSE 连接支持 `?since=<seq>`，只推送该游标之后的新日志，避免每秒重复传输全量列表。
- 前端记录 `lastLogSeq`，断线重连时带上该游标，既不会重复也不会漏日志。
- 日志面板按到达顺序追加，并保留 `ERROR/WARNING` 级别样式。

## 7. Shell 脚本逻辑

### 7.1 `bin/get_patch_image_tag_list.sh`

职责：

- 拉取最新 `nuwa` 仓库。
- 读取 `.trash/oss_patch_version.txt`。
- 计算从当前 patch 到目标 patch 之间的差集。
- 从 `nuwa/6.6/6.6.9/<patch>/make.sh` 中提取 `:v6.6` 镜像行。
- 生成最终的 `patch_image_tag_list.txt`。

输出格式类似：

```text
deepflow-server_tag: v6.6.5617
pcap_tag: v6.6.220
```

脚本特点：

- 只支持 `6.6 / 6.6.9` 这条目录结构，属于强耦合实现。
- 依赖 OSS 列表缓存文件先由后端刷新好。
- 执行前会强制 `git pull --ff-only`，失败时重试 3 次。

### 7.2 `bin/pull_save.sh`

职责：

- 登录 `hub.deepflow.yunshan.net`。
- 读取镜像列表文件或直接处理命令行镜像。
- 拉取镜像。
- 保存为 `.tar`。

支持两种镜像列表格式：

- `name_tag: vX.Y.Z`
- `name: vX.Y.Z`

当前实现要点：

- 默认容器工具是 `nerdctl -n k8s.io`（用 bash 数组保存，支持 `-c "nerdctl -n k8s.io"` 这种带参数的写法）。
- 也支持 `-c docker` 切换。
- 仓库前缀默认是 `hub.deepflow.yunshan.net/dev/`。
- 仓库登录、镜像拉取、保存失败时都会重试 3 次。
- 镜像操作默认使用 `k8s.io` namespace；历史 namespace 残留问题见 [`docs/CONTAINERD_RUNBOOK.md`](./docs/CONTAINERD_RUNBOOK.md)。
- 产出的 tar 文件名格式：
  - `<image_name>_<tag>.tar`

### 7.3 `bin/deepflow_patch_upgrade.sh`

该脚本不会在服务端执行，而是作为升级包内容的一部分交付给使用方。

主要能力：

- 导入 `.tar` 镜像。
- 推送镜像到目标仓库。
- 备份并修改 `values.yaml`、`values-custom.yaml`。
- 执行 DeepFlow 组件升级。
- 检查升级后的镜像版本。
- 回滚配置并重新部署。

它默认面向某套固定部署环境，内部写死了：

- `container_cmd="docker"`
- `values_yaml=/usr/local/deepflow/templates/values.yaml`
- `target_registry=hubmgt-uat.paic.com.cn/deepflow`
- 一组固定组件名单

所以这个脚本是“随包交付的运维脚本”，不是一个通用升级器。

## 8. 输出产物

### 8.1 V6 输出包

文件名：

```text
deepflow_patch_v669_<from_patch_seq>_<to_patch_seq>.tar.gz
```

例如：

```text
deepflow_patch_v669_01_13.tar.gz
```

### 8.2 V7 输出包

文件名：

```text
deepflow_patch_v7_<task_id>.tar.gz
```

### 8.3 包内容

每个升级包内通常包含：

- 若干镜像 tar 文件
- `patch_image_tag_list.txt`
- `deepflow_patch_upgrade.sh`

打包时使用了：

```bash
tar -czf ... --transform 's|.*/||' --transform 's|.*|<包名>/&|'
```

因此解压后会得到一个与包名同名的目录，所有文件位于该目录内（不再散落在当前目录）。打包先写入临时文件，完成后原子替换目标包，避免下载到半成品。

## 9. 生命周期与清理策略

### 9.1 任务状态保留

- 成功任务状态保留：
  - `success_ttl_seconds = 7200`
- 失败任务状态保留：
  - `failure_ttl_seconds = 3600`

### 9.2 临时下载链接

- `/download/<task_id>` 对应的临时下载有效期为：
  - `download_ttl_seconds = 90`

注意：

- 90 秒过后，并不是文件会被删除。
- 文件仍可能存在于 `upgrade_packages/`。
- 只是“按任务下载”入口会失效。
- 用户仍可通过 `/existing-packages` 和 `/download-existing/<filename>` 下载历史包。

### 9.3 任务临时文件

任务结束后会清理：

- `images/<task_dir>/`
- `logs/task_<task_id>.log`

不会清理：

- `upgrade_packages/` 中已生成的升级包
- `logs/app.log`

## 10. 启动方式

### 10.1 本地直接运行

先准备：

- Python 3.12（与容器镜像一致；Flask 3.1 要求 3.9+）
- Redis 可访问
- `nuwa/` 仓库访问能力
- `ossutil` 配置
- `nerdctl` 或 `docker`

安装依赖：

```bash
pip install -r requirements.txt
```

启动 Web：

```bash
python app.py
```

启动 Worker：

```bash
python worker.py
```

如果只启动 `app.py` 而不启动 `worker.py`：

- 页面能打开
- 任务能入队
- 但不会真正执行

### 10.2 容器方式

项目提供：

- `dockerfile`
- `entrypoint.sh`

构建：

```bash
docker build -f dockerfile -t auto_packing:latest .
```

运行 Web 容器示例：

```bash
docker run -p 8000:8000 \
  -e OSS_ACCESS_KEY_ID=... \
  -e OSS_ACCESS_KEY_SECRET=... \
  -e OSS_ENDPOINT=... \
  -e REDIS_PASSWORD=... \
  auto_packing:latest
```

如果需要独立跑 worker，可基于同一镜像执行：

```bash
docker run \
  -e OSS_ACCESS_KEY_ID=... \
  -e OSS_ACCESS_KEY_SECRET=... \
  -e OSS_ENDPOINT=... \
  -e REDIS_PASSWORD=... \
  auto_packing:latest python worker.py
```

### 10.3 `entrypoint.sh` 做了什么

容器启动时：

1. 创建 `/app/bin`、`/app/logs`、`/app/.trash`、`/app/latest_image_list`
2. 若 `/app/nuwa` 不存在，则从 GitLab 克隆
3. 若提供 OSS 环境变量，则生成 `/app/bin/.ossutilconfig`
4. 最后执行传入命令

### 10.4 发布/更新镜像

推荐使用 `bin/redeploy.sh`。它会为每次发布生成唯一 tag（时间戳 + git 短 hash），构建后更新 `maintenance-app` / `maintenance-worker` 两个 Deployment，避免 `:latest` + `IfNotPresent` 命中节点旧镜像。

```bash
# 镜像直接导入本机 k8s.io containerd
NERDCTL_IMPORT=1 ./bin/redeploy.sh

# 或者推送到镜像仓库
REGISTRY=registry.example.com ./bin/redeploy.sh
```

脚本依赖 `docker` 和 `kubectl`，默认命名空间 `getpatch`。如果镜像已通过其他方式分发到节点，也可以直接运行 `./bin/redeploy.sh`，脚本会提示确认镜像存在。

脚本默认还会：

- 用仓库里的 `config.yaml` 同步更新 `maintenance-app-config` ConfigMap（不会重置 Deployment 的镜像 tag）。
- 把 `apply/maintenance-app-deployment.yaml` 中两个 `auto_packing:` 镜像 tag 更新为本次发布 tag。**发布后请提交这个 YAML 变更**，否则下次用旧 YAML `kubectl apply` 会回退镜像。

不需要同步配置或 YAML 时，可以设置 `SYNC_CONFIG=0` / `UPDATE_MANIFEST=0`。

镜像 tag 现在固化在清单中，日常发布建议继续使用 `bin/redeploy.sh`；直接 `kubectl apply -f apply/maintenance-app-deployment.yaml` 也会使用清单里固定的 tag。

## 11. Kubernetes 部署说明

`apply/maintenance-app-deployment.yaml` 已经体现出当前推荐部署模型：

- 一个 `maintenance-app` Deployment
  - 提供 Web/API/SSE
- 一个 `maintenance-worker` Deployment
  - 负责后台构建
- 一个 `maintenance-redis` Deployment
  - 提供 Redis
- 一个 `maintenance-svc`
  - 暴露 Web 服务

### 11.1 关键点

- app 与 worker 使用同一个镜像，tag 固化在清单中（当前 `auto_packing:20261022-2b453e0`，由 `bin/redeploy.sh` 自动更新）
- worker 通过 `args: ["python", "worker.py"]` 启动
- Redis 密码通过 Secret 注入
- app / worker 都依赖：
  - `OSS_ACCESS_KEY_ID`
  - `OSS_ACCESS_KEY_SECRET`
  - `OSS_ENDPOINT`
  - `APP_CONFIG_PATH`
- 通过 `hostPath` 挂载 `/run/containerd/containerd.sock`，供 `nerdctl` 使用
- app / worker 默认 `CONTAINERD_NAMESPACE=k8s.io`，避免镜像操作误用 `default` namespace
- 通过 PVC 挂载 `/app/upgrade_packages`
- Redis 使用独立 PVC + AOF（`appendonly yes`），Pod 重建后任务队列和状态不会丢失

### 11.2 当前清单里的实际持久化情况

当前 K8s 清单显式持久化了：

- `/app/upgrade_packages`（升级包）
- `/data`（Redis AOF 数据）

这意味着：

- 已生成升级包会保留在 PVC 上。
- Redis 重启/Pod 重建后，任务状态、队列和日志仍可恢复。
- `images/`、`logs/`、`latest_image_list/` 默认仍在容器文件系统内。
- Pod 重建后，运行中的临时文件和本地日志不会保留。

如果希望进一步增强可观测性与恢复能力，建议额外挂载：

- `/app/logs`
- `/app/images`
- `/app/latest_image_list`
- 视情况缓存 `/app/nuwa`

## 12. 依赖与约束

### 12.1 Python 依赖

当前 `requirements.txt` 里包含：

- Flask 3.1.0
- gunicorn 23.0.0
- redis 5.2.1
- PyYAML 6.0.2

容器基础镜像为 `python:3.12-slim-bookworm`，默认使用 Gunicorn 启动。

### 12.2 外部依赖

- Redis
- GitLab `nuwa` 仓库
- 阿里 OSS
- DeepFlow 镜像仓库
- `nerdctl` 或 `docker`
- `tar`

### 12.3 当前实现中的强耦合点

- V6 的 `get_patch_image_tag_list.sh` 写死了 `6.6 / 6.6.9` 路径结构。
- 只处理 `:v6.6` 镜像行。
- `/versions` 只支持 `series=v6`。
- 前端虽然叫 V7，但服务端本质上只是“手工镜像列表打包模式”。
- `deepflow_patch_upgrade.sh` 内目标环境、目标仓库、组件列表大量写死。

## 13. 已知风险与注意事项

### 13.1 安全与凭据

当前仓库中存在明显需要继续治理的点：

- `bin/pull_save.sh` 中写死了镜像仓库登录用户名和密码。
- `apply/maintenance-app-deployment.yaml` 中示例 Secret 含明文 Redis 密码。

这些内容说明该仓库当前更偏内部工具形态，不适合作为公开安全基线。

### 13.2 可移植性

项目不是开箱即用的通用产品，运行依赖较强的内部环境约束：

- GitLab 地址固定
- 镜像仓库固定
- OSS 路径固定
- `nuwa` 目录结构固定
- 目标升级脚本面向固定安装路径

### 13.3 测试覆盖不足

仓库有 `tests/` 目录和基础单元测试，但还没有 CI 流水线，shell 脚本和端到端构建仍以人工验证为主。

### 13.4 containerd namespace 一致性

- containerd 的 content 元数据按 namespace 隔离，历史遗留的 `default` 镜像可能导致
  `nerdctl save` 报 `content digest ... not found`。
- worker 已通过 `CONTAINERD_NAMESPACE=k8s.io` 固定默认 namespace。
- 只读巡检：
  ```bash
  python3 bin/check_containerd.py
  ```
- 现象、应急修复和清理规范见 [`docs/CONTAINERD_RUNBOOK.md`](./docs/CONTAINERD_RUNBOOK.md)。

## 14. 推荐的阅读顺序

如果要继续维护这个项目，建议按下面顺序读代码：

1. `app.py`
   - 先理解配置、Redis key、任务状态结构、接口定义。
2. `worker.py`
   - 再理解队列消费、心跳、回收与并发控制。
3. `templates/index.html` + `static/app.js`
   - 理解前端如何发起任务、恢复任务和消费 SSE。
4. `bin/get_patch_image_tag_list.sh`
   - 理解 V6 的 patch 差异来源。
5. `bin/pull_save.sh`
   - 理解镜像拉取与保存。
6. `bin/deepflow_patch_upgrade.sh`
   - 理解最终交付物如何被使用。
7. `apply/maintenance-app-deployment.yaml`
   - 理解线上部署形态。
8. `CLAUDE.md`
   - AI 助手的代码库指南，包含架构速览、命令、环境变量、已知问题。
9. `ISSUES.md`
   - 当前已知问题与已完成优化摘要。
10. `docs/CONTAINERD_RUNBOOK.md`
    - containerd 跨 namespace 问题的巡检、应急修复与清理规范。

## 15. 一句话总结当前项目逻辑

当前 `auto_packing` 的本质不是单纯“打包脚本集合”，而是一个围绕 DeepFlow 升级包生成流程构建的异步任务平台：

- Web 负责发起任务和展示结果
- Redis 负责排队、限流、状态与日志缓存
- worker 负责实际构建
- shell 脚本负责差异计算、镜像拉取和升级交付
- `upgrade_packages/` 负责沉淀可复用的历史升级包
