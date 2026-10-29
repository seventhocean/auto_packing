# AGENTS.md

本文件面向所有 AI 编程助手和开发者，不绑定特定工具。修改代码前请先阅读本文件和
[`ISSUES.md`](./ISSUES.md)；使用 containerd 回退流程时另读
[`docs/CONTAINERD_RUNBOOK.md`](./docs/CONTAINERD_RUNBOOK.md)。

## 1. 项目概览

DeepFlow 补丁包构建与分发工具，当前规模为 1~2 个用户，**稳定优先**。
没有明确需求时，不大改构建主链路、队列模型或前端结构。

- `app.py`：Flask Web/API、任务入队、SSE、任务中心、归档与 OSS 交付逻辑。
- `worker.py`：独立 Redis 队列消费者，执行构建、心跳、回收和原 worker 上传重试。
- `image_refs.py`：后端与 Shell 共用的镜像引用校验、归一化与导出文件命名。
- `templates/index.html`、`static/app.css`、`static/app.js`：单页工作台。
- Redis：任务状态、日志、队列、执行者槽位与心跳。
- `config.yaml`：业务配置真源；部署清单中的 ConfigMap 由同步工具生成。

两种模式：`mode=patch` 按版本差异打包，支持 6.4.9/6.5.9/6.6.9/7.0.9/7.1.9；
`mode=custom` 按手工镜像清单打包。内部 `task_type=v6/v7` 为历史兼容字段，
分别对应 patch/custom，不应把它们等同于产品大版本。

## 2. 快速运行

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 两个终端都应设置同一 APP_CONFIG_PATH，开发配置需指向本地 Redis。
APP_CONFIG_PATH=/tmp/auto-packing-local.yaml python app.py
APP_CONFIG_PATH=/tmp/auto-packing-local.yaml python worker.py
```

本地配置示例、外部工具要求见 README。只启动 app 会正常入队，但不会执行任务。
自动化测试不需要业务 Redis、OSS 或镜像仓库凭据。

容器构建与默认 Web 启动：

```bash
docker build -f dockerfile -t auto_packing:latest .
gunicorn --workers 2 --threads 4 --worker-class gthread \
  --bind 0.0.0.0:8000 --timeout 0 app:app
```

## 3. 架构与并发

```text
浏览器 → Flask/app → Redis build:queue → worker → skopeo → tar.gz → OSS
          ↑ SSE/状态          ↑ 心跳/processing/slot             ↓ 签名下载
```

- Web 只入队和观察，worker 单进程串行执行。实际并发为配置与可用 worker 数的较小值。
- 仓库配置 `max_concurrent_tasks=3`，部署清单为三个 worker；调度仅尽量分散到各节点。
- slot 存放 `worker_id:attempt_token`。Lua 原子检查容量并获得执行权，只有同 token 的
  重试幂等；重复消息不能共用执行权。
- 运行中的状态、日志和 slot 释放带执行者校验，旧尝试失去执行权后不得覆盖新状态。
- 心跳默认每 10s 写入、60s TTL；在线展示与 TTL 对齐，回收默认每 30s 扫描。
- 回收锁默认 120s；Lua 在同一次操作中检查失活、删除属于旧 worker 的 slot、移动
  processing 消息。**不能删除失活 slot 清理逻辑**，否则旧槽位仍可能阻塞构建。
- 等待槽位不再因为固定 10 分钟而失败；取消仍可终止排队任务。
- 临时目录和日志包含独立 attempt，避免恢复时旧执行者清掉新执行者的文件。

## 4. Redis key 设计

以下 key 均带配置的 `key_prefix`，默认 `auto_packing`：

| 后缀 | 类型 | 用途 |
|---|---|---|
| `task:<id>:meta` | JSON string | 状态、参数、逻辑包名、OSS 对象名、上传重试信息 |
| `task:<id>:logs` / `log_seq` | list / int | 有界日志和增量序号，日志追加与序号分配为原子操作 |
| `task:<id>:cancel` | string | 取消标记 |
| `task:<id>:slot` | string | 当前执行者 token，默认 6h 兜底 TTL |
| `build:queue` | list | 普通待消费任务 |
| `build:processing:<worker>` | list | 该 worker 正在处理的消息 |
| `build:upload:<worker>` | list | 仅原 worker 能处理的上传重试消息 |
| `worker:<worker>:heartbeat` | string | 心跳，默认 60s TTL |
| `build:reclaim:lock` | string | 失活回收锁 |
| `packages:cleanup:lock` | string | 本地/OSS 保留策略互斥锁 |
| `builds:active` | int | 观测值，不作为执行权依据 |

成功状态默认保留 7200s、失败状态 3600s，日志跟随状态过期。上传失败的可重试窗口
默认也是 3600s；它依赖原 worker 在线且本地保留文件仍在，不能跨 Pod 重建恢复。

## 5. 构建与交付流程

1. `POST /build` 校验并入队，前端拿 task_id 后订阅 SSE；刷新按 task_id 恢复。
2. patch 模式刷新对应 OSS 版本列表，再从 nuwa 的对应版本线生成镜像差异清单。
   custom 模式直接使用已校验、归一化、去重后的清单。
3. `pull_save.sh` 先校验完整清单，再通过 skopeo 导出每个镜像的 docker-archive。
   默认仓库单组件保留旧 tar 文件名，其他路径/仓库追加完整引用哈希以防重名。
4. 脚本全部成功后原子写入 `image_exports.tsv`。后端核对引用、文件名、文件数量及
   非空 tar，缺项/重名/非法项不得生成成功包。
5. 归档使用临时文件和 `os.replace`，顶层目录与逻辑包名一致。
6. OSS 模式在任务/attempt 专属 `.pending` 目录归档，上传到独立 `oss_object_name`；
   `package_name` 仍是客户可识别的逻辑包名。同版本对不会覆盖其他任务的远端对象。
7. 只有交付成功才公布完成状态和下载入口；随后可删除本地副本并按保留策略回收旧包。

patch 包名为 `deepflow_patch_v<版本线代码>_<起始序号>_<目标序号>.tar.gz`；
custom 包名为 `deepflow_patch_custom_<task_id>.tar.gz`。

上传失败会自动有限重试，仍失败则标记错误、不给下载入口、保留已完成归档。
任务中心“重试上传”调用专用接口，由原 worker 只上传归档，不再拉镜像或重新打包。
原 worker 离线、任务已过期或本地文件丢失时，需要重新构建。

默认拉取超时 2h、归档超时 30min、单次上传超时 1h；拉取、归档和上传都受取消与
执行权检查保护。修改这些上限或原子写入逻辑必须有对应回归。

## 6. API

| Route | Method | Purpose |
|---|---|---|
| `/` / `/health` | GET | 工作台 / Redis 健康检查 |
| `/channels` / `/versions?channel=7.1.9` | GET | 版本线 / 对应 OSS patch 列表 |
| `/build` | POST | `mode=patch` 或 `custom` 入队，返回 task_id |
| `/build?task_id=&since=` | GET | SSE 观察已有任务 |
| `/task-status/<id>?since=` | GET | 状态与增量日志 |
| `/build/<id>/cancel` | POST | 取消任务 |
| `/build/<id>/retry-upload` | POST | 仅重试已保留归档的 OSS 上传 |
| `/tasks/overview` | GET | 进行中、最近完成任务和运行状态 |
| `/download/<id>` | GET | 下载本次成功交付的产物，检查短期下载窗口 |
| `/existing-packages` | GET | 合并本地与 OSS 历史产物 |
| `/download-existing/<filename>` | GET | 下载历史对象或本地包 |
| `/packages/cleanup` | POST | 按当前策略回收旧包 |

保留了旧 GET 入队兼容逻辑，新功能统一使用 POST 创建和 GET 观察。

## 7. 前端

- 模板保留 DOM，样式和行为分别在 app.css/app.js，不引入前端框架。
- 模式与版本线选择在构建和恢复观察期间锁定；任务 id 写入 URL。
- SSE 重试失败后继续保持当前任务，通过有超时的状态查询恢复，不能把连接失败当成任务结束。
- 任务中心默认 5s 轮询，请求有超时，失败后后续轮询仍可继续。
- 日志按实际返回序号推进游标，接近底部才自动跟随。
- 下载直接导航到应用入口，不以跨域 HEAD 成功作为前提；复制 curl 带重定向和 HTTP 错误检测。
- 镜像输入支持带端口 registry；后端的 image_refs.py 为最终格式校验依据，不支持 digest 引用。

## 8. Shell 与辅助工具

| 文件 | 职责 |
|---|---|
| `bin/get_patch_image_tag_list.sh` | 按版本线从 nuwa 生成差异清单；git pull 有锁与重试 |
| `bin/pull_save.sh` | 校验/去重清单，skopeo 导出；`COPY_TOOL=archive` 保留旧回退 |
| `image_refs.py` | 镜像名、端口、tag、默认仓库和唯一文件命名规则 |
| `bin/deepflow_patch_upgrade.sh` | 客户部署端脚本，目标现场配置仍为定制件 |
| `bin/sync_manifest.py` | 配置与两个业务容器镜像同步；`--check` 只读检查漂移 |
| `bin/redeploy.sh` | 构建、分发、同步配置/镜像、滚动更新 |
| `bin/check.sh` | 本地与 CI 共用检查入口 |
| `bin/check_containerd.py` | 旧 containerd 回退流程的只读巡检 |

仓库凭据仍按当前内部工具策略保留，不在无明确需求时调整。

## 9. 配置与环境变量

配置真源是 `config.yaml`。修改后同步并检查：

```bash
python bin/sync_manifest.py
python bin/sync_manifest.py --check
```

同步工具需要 Python 3 与 requirements 中的 PyYAML，不访问集群。不要手工维护部署
ConfigMap 的另一份参数；当前独立 worker 部署必须 `package_storage.backend=oss`。

- Redis：并发 3、成功 TTL 7200s、失败 TTL 3600s、下载入口窗口 90s、最多 2000 条日志。
- 存储：OSS 后端，签名有效期 900s，上传成功后删除本地副本。
- 保留策略：最多 40 个 / 60GiB，保留最近至少 3 个、30 分钟内的新包和本次产物；
  候选包括纯 OSS 对象。远端列表获取失败时停止回收，不能拿不完整列表计算保护数量。
- `dry_run=true` 只报告候选，不删除；过期的失败上传副本随后续回收处理。

| 环境变量 | 用途 |
|---|---|
| `APP_CONFIG_PATH` / `REDIS_PASSWORD` | 覆盖配置路径 / Redis 密码 |
| `REQUIRE_OSS_STORAGE=1` | worker 独立目录部署启动校验；非 OSS 后端拒绝启动 |
| `COPY_TOOL` | `skopeo` 或 `archive` |
| `NUWA_DIR` / `SKIP_NUWA` | 仓库缓存路径 / app 跳过 nuwa 克隆 |
| `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` / `OSS_ENDPOINT` / `OSS_REGION` | entrypoint 生成 ossutil 配置 |
| `PULL_TIMEOUT_SECONDS` / `ARCHIVE_TIMEOUT_SECONDS` | 拉取 / 归档超时 |
| `OSS_QUERY_TIMEOUT_SECONDS` / `OSS_VERSIONS_CACHE_TTL_SECONDS` | 版本查询超时 / 页面版本缓存 TTL |
| `SLOT_TTL_SECONDS` | 槽位兜底 TTL |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` / `WORKER_HEARTBEAT_TTL_SECONDS` / `WORKER_RECLAIM_INTERVAL_SECONDS` | 心跳 / 回收周期 |
| `GUNICORN_WORKERS` / `GUNICORN_THREADS` / `FLASK_DEBUG` | Web 进程、线程与开发 debug |
| `CONTAINERD_NAMESPACE` | archive 回退默认 namespace，生产为 k8s.io |

## 10. 测试与检查

```bash
bash bin/check.sh
# 可选：只连接可丢弃的本机 Redis，验证真实 Lua、TTL 和队列协议。
TEST_REDIS_URL=redis://127.0.0.1:6379/15 bash bin/check.sh
```

普通回归使用临时目录、FakeRedis 和外部工具替身，未模拟 OSS 调用会直接失败。
真实 Redis 测试仅接受 localhost/回环地址，使用随机 key 前缀清理自身数据，不使用 flushdb。
不要给 TEST_REDIS_URL 设置业务 Redis。

check.sh 执行 Python 回归、Python 编译、逐个 Shell 语法检查、Node 前端状态回归及
部署配置漂移检查。GitHub Actions 在 Linux 上运行相同入口、临时 Redis 集成测试和镜像构建。
真实 registry/OSS/客户 Docker 的端到端验收独立进行，普通单测不得依赖业务凭据。

## 11. 部署与发布

发布机需要 Python 3/PyYAML、Docker、kubectl 和对应访问权限。多节点推荐：

```bash
REGISTRY=registry.example.com/team ./bin/redeploy.sh
```

脚本构建唯一 tag、推仓库、同步 ConfigMap 和 app/worker 镜像并等待 rollout。
连续发布使用容器名称定位镜像，不能按旧仓库前缀做字符串替换。

`NERDCTL_IMPORT=1` 只向本机 containerd 导入；仅当实时 app/worker 的 nodeSelector 都
固定到 `IMPORT_NODE`（默认 hostname）时允许执行。三节点分散 worker 必须走可拉取仓库。

部署清单：app 单副本仍使用 apk01 的旧包 PVC；Redis 单实例 PVC + AOF 并使用 Recreate，
app/Redis 保留 apk01 的 nodeSelector。worker 三副本不挂 containerd.sock、不共享产物 PVC，
使用 emptyDir 和每节点 hostPath nuwa 缓存，`SKIP_NUWA=1` 用于 app。
**更改 app/Redis nodeSelector 前确认存储；不要让两个 Redis 实例同时写一份 AOF。**

新 worker 与旧 worker 的执行权协议不同，发布本轮 worker 变更时应先等正在执行的任务结束，
再切换 worker。不要让不识别 owner token 的旧镜像与新 worker 混跑同一批任务。

## 12. 已知问题与修改原则

未完成事项见 ISSUES，交付边界见 docs/ARTIFACT_DELIVERY.md：

- Redis 仍是 apk01 单节点存储，不具备故障自动切换能力。
- logs/app.log 尚无轮转；客户升级脚本仍需按现场核对。
- 缺少 make.sh 的 patch 是否合法为空，需要上游数据契约，不能贸然按所有缺文件失败。
- 真实 registry → OSS → 客户 Docker load 和 Kubernetes 发布仍需目标环境验收。

修改时维持小范围、有回归的修正，不删除运行数据或已有未提交改动；保护失活 slot 清理、
独立 attempt 目录和归档原子替换。完成代码改动后执行第 10 节检查。

## 13. 目录结构

```text
app.py / worker.py / image_refs.py
config.yaml / dockerfile / entrypoint.sh
templates/ / static/
bin/                     # 构建、发布、同步与检查工具
apply/                   # 部署和存储清单
tests/                   # Python、Node 与可选真实 Redis 回归
.github/workflows/       # Linux CI
docs/                    # Review 快照、开发清单、交付说明、旧 containerd runbook
images/ / latest_image_list/ / logs/ / .trash/ / upgrade_packages/  # 运行时数据
nuwa/                    # 默认开发仓库目录；生产由 NUWA_DIR 指向节点缓存
```

worker 发布使用 Recreate，执行所有权协议升级不可新旧混跑；发布前等待在途构建结束。
