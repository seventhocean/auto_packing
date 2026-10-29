# auto_packing

DeepFlow 补丁包构建与分发工具。Web 提交任务，Redis 保存状态与队列，独立 worker
导出镜像、生成升级包并交付到 OSS。客户收到的仍是一个 tar.gz，解压后逐个 docker load。
当前服务面向 1~2 个内部用户，以稳定和可恢复为优先。

## 当前架构

```text
浏览器 → Flask/gunicorn → Redis 队列 → worker x3
  ↑          │                          ├─ patch：OSS 版本列表 + nuwa 差异
  │ SSE/状态 │                          ├─ custom：手工镜像清单
  │          │                          └─ skopeo → docker-archive → tar.gz → OSS
  └──────────┴────────────── 下载入口 → OSS 签名 URL
```

| 文件 | 主要职责 |
|---|---|
| app.py | Flask API、SSE、任务/日志协议、归档、OSS 交付与保留策略 |
| worker.py | 串行消费、执行权、心跳、原子回收、原 worker 上传重试 |
| image_refs.py | 共用的镜像引用解析、去重与文件命名 |
| templates/index.html + static/ | 工作台界面、任务恢复、镜像编辑与历史包列表 |
| bin/get_patch_image_tag_list.sh | 从 nuwa 对应版本线生成差异清单 |
| bin/pull_save.sh | 整批校验后导出镜像并生成导出核对清单 |
| bin/sync_manifest.py | 将业务配置和镜像同步到部署清单，支持只读漂移检查 |
| bin/redeploy.sh | 构建、分发镜像、同步配置并等待 app/worker rollout |

`mode=patch` 支持 6.4.9、6.5.9、6.6.9、7.0.9、7.1.9 五条版本线；`mode=custom`
接受手工镜像列表。内部 task_type 的 v6/v7 为历史兼容命名，分别对应这两种构建方式。

## 开发与检查

需要 Python 3.12、Node.js；真实构建另需 Redis、OSS、nuwa、镜像仓库以及对应工具访问。
普通自动化回归不访问业务外部服务，也不需要业务凭据。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
bash bin/check.sh
```

统一检查包括 Python 回归、Python 编译、逐个 Shell 语法、前端 Node 状态回归和部署
ConfigMap 漂移检查。外部工具回归通过临时替身执行，未模拟 OSS 调用会导致测试失败。
FakeRedis 支持测试需要的 TTL/列表/原子协议行为。

可选的真实 Redis 回归仅允许连接可丢弃的本机实例：

```bash
TEST_REDIS_URL=redis://127.0.0.1:6379/15 bash bin/check.sh
```

测试使用随机 key 前缀，只清理自身数据，不运行 flushdb。不要指向业务 Redis。
GitHub Actions 的 `.github/workflows/check.yml` 在 Linux 上运行相同检查入口、临时
Redis 集成测试及 Docker 镜像构建；真实 registry/OSS/客户 Docker 链路单独验收。

本地启动时，新建 `/tmp/auto-packing-local.yaml`，至少设置：

```yaml
redis:
  host: 127.0.0.1
  port: 6379
  password: ""
package_storage:
  backend: local
```

在两个终端使用同一配置启动：

```bash
APP_CONFIG_PATH=/tmp/auto-packing-local.yaml python app.py
APP_CONFIG_PATH=/tmp/auto-packing-local.yaml python worker.py
```

页面位于 8000 端口。只有 Web 没有 worker 时，任务能入队但不会执行。
普通检查可在 macOS 运行；仓库附带的 ossutil/nerdctl 为 Linux 二进制，真实构建推荐使用
项目容器或目标 Linux 环境。真实运行仍需要准备工具配置与外部服务访问权限。

## 业务配置与部署真源

`config.yaml` 是业务配置真源，`APP_CONFIG_PATH` 可覆盖运行时路径，`REDIS_PASSWORD`
覆盖文件中的 Redis 密码。修改仓库配置后执行：

```bash
python bin/sync_manifest.py
python bin/sync_manifest.py --check
```

同步工具只操作本地文件，需要 Python 3 和 PyYAML。不要分别手工维护 config.yaml 和
部署清单 ConfigMap；CI 会检查两者漂移。redeploy 也会同步完整业务配置。

当前仓库配置的关键值：

| 配置 | 当前值与含义 |
|---|---|
| redis.max_concurrent_tasks | 3，上限还受可用 worker 数限制 |
| success_ttl_seconds / failure_ttl_seconds | 成功状态 7200s / 失败状态 3600s |
| download_ttl_seconds | 任务下载入口有效 90s，过期仍可从保留的历史对象下载 |
| package_storage.backend | oss；仅本机开发或确实共享目录的部署可使用 local |
| presign_seconds / upload_timeout_seconds | OSS 签名 900s / 单次上传上限 3600s |
| delete_local_after_upload | true；确认交付并写入完成状态后删除本地副本 |
| package_retention | 最多 40 个、60GiB；至少保留最新 3 个、30 分钟内的新包和当前产物 |

独立 emptyDir worker 设置 `REQUIRE_OSS_STORAGE=1`，配置误回退 local 会拒绝启动。
OSS 保留策略包含纯远端对象；远端列举失败时停止回收，防止以不完整候选集误删。
`dry_run=true` 可只报告候选；`POST /packages/cleanup` 触发即时回收。

常用环境变量另见 AGENTS 第 9 节。OSS 凭据为 `OSS_ACCESS_KEY_ID`、
`OSS_ACCESS_KEY_SECRET`、`OSS_ENDPOINT` 和 `OSS_REGION`，entrypoint 生成私有工具配置。
不要把生成的 `.ossutilconfig` 或运行时数据加入版本库。

## 构建、失败恢复与下载

1. 页面或 API 通过 `POST /build` 创建任务。Web 只入队，不在请求中执行构建。
2. worker 从 Redis 移入自己的 processing 队列，原子获得带 worker/attempt 的执行权。
3. patch 模式刷新 OSS 对应版本线缓存，从 nuwa 生成镜像差异；custom 模式使用镜像清单。
4. `image_refs.py` 校验全部引用、补默认仓库、去重。脚本用 skopeo 生成每镜像一个
   docker-archive，后端核对 `image_exports.tsv`，不允许漏镜像后成功。
5. 临时归档生成后使用 os.replace 原子发布。OSS 模式先保留在任务/attempt 独立
   `.pending` 路径，上传独立对象后才公布成功与下载入口。
6. 交付后执行保留策略；任务临时镜像、列表和日志按执行尝试清理。

镜像输入示例：

```text
deepflow-agent:v7.1.1
team-a/agent:v7.1.1
hub.deepflow.yunshan.net/dev/deepflow-server:v6.6.5550
registry.example.com:5000/team/image:_release-1
```

支持带端口 registry，tag 最长 128 字符；仓库路径小写，不支持 digest 引用或隐式 latest。
默认仓库单组件仍使用原来的 `name_tag.tar` 文件名，其他路径或仓库追加引用哈希，防止
team-a/agent 和 team-b/agent 互相覆盖。旧 `name_tag: tag` 生成清单仍可读。

逻辑包名：

- patch：`deepflow_patch_v<版本线代码>_<from>_<to>.tar.gz`。
- custom：`deepflow_patch_custom_<task_id>.tar.gz`。

OSS 实际对象名独立存于 `oss_object_name`，带唯一后缀，逻辑包名存于 `package_name`。
相同版本对的任务不会覆盖彼此对象，任务下载使用该任务已确认上传的对象；历史列表展示
实际对象名。包内仍包含顶层目录、镜像 tar、patch_image_tag_list.txt 和客户升级脚本。
运行时导出核对清单不等同于已提供正式 digest/SHA256 客户校验文件。

上传自动有限重试后仍失败时，任务为错误状态，无成功下载入口；已完成归档留在原 worker。
失败状态默认保留一小时，原 worker 在线且归档仍在时，可在任务中心点击“重试上传”，
或调用 `POST /build/<task_id>/retry-upload`。重试仅上传已有包，无需重新拉镜像。
Pod 重建、worker 失活、状态过期或本地包丢失后，需要重新构建；emptyDir 不是持久恢复存储。

默认镜像拉取上限两小时、归档半小时。拉取、归档、上传的异常/超时/取消/执行权失效均会
终止进程组。恢复期间旧执行者不能覆盖新任务状态或释放新槽位，重复消息不能共享执行权。
回收检查失活、删除旧 slot、转移消息使用同一次 Redis Lua 操作，响应丢失重试不会重复入队。

下载按钮直接导航应用下载入口，OSS 后端跳转到签名 URL，不进行跨域 HEAD 检查。
“复制 curl”命令跟随重定向并检测 HTTP 错误。90 秒任务入口窗口与 OSS 的 900 秒签名
期限是两层机制，历史包只在保留策略尚未删除时可用。

## API

```json
{"mode":"patch","channel":"7.1.9","current":"01-...","target":"03-..."}
```

```json
{"mode":"custom","images":["deepflow-agent:v7.1.1"]}
```

两种请求均发往 `POST /build`，成功返回 task_id。后续统一使用已有任务观察入口，
避免 SSE 重连创建新任务。

| 路由 | 方法 | 用途 |
|---|---|---|
| / / /health | GET | 页面 / Redis 健康检查 |
| /channels / /versions?channel=7.1.9 | GET | 版本线 / OSS patch 列表 |
| /build?task_id=...&since=... | GET | SSE 增量日志与状态 |
| /task-status/<id>?since=... | GET | 状态查询 |
| /tasks/overview | GET | 进行中与最近完成任务 |
| /build/<id>/cancel | POST | 排队或执行阶段取消 |
| /build/<id>/retry-upload | POST | 原 worker 仅重试上传 |
| /download/<id> | GET | 本次交付产物，受任务入口窗口限制 |
| /existing-packages | GET | 合并本地与远端历史列表 |
| /download-existing/<filename> | GET | 历史对象或本地文件下载 |
| /packages/cleanup | POST | 按当前保留策略回收 |

旧 series=v6/v7 和 GET 创建接口保留兼容；新调用使用 mode/channel 与 POST。

## 前端状态

模式和版本线在构建期间锁定，任务 id 写入 URL，刷新可恢复。SSE 重试耗尽后保持当前
任务锁定，使用有超时的状态请求轮询确认；观察连接失败不会直接变成“可新建”。
确认终态后才能结束观察，任务中心请求挂住也不会永久阻断后续刷新。
日志游标只推进到实际返回的序号，靠近底部时自动跟随。普通失败重试回填参数，上传失败
的重试则直接请求原 worker 上传归档。

## 容器与 Kubernetes 发布

```bash
docker build -f dockerfile -t auto_packing:latest .
REGISTRY=registry.example.com/team ./bin/redeploy.sh
```

发布机需安装 Python 3/PyYAML、Docker、kubectl。脚本创建唯一 tag、推送仓库、同步完整
ConfigMap 与 app/worker 镜像并等待滚动更新；重复发布可正确更新已有 registry 前缀镜像。

清单首次部署需预先准备 Namespace、PVC 和必要 Secret，并将可被所有目标节点拉取的
业务镜像写入清单：

```bash
python bin/sync_manifest.py --image registry.example.com/team/auto_packing:20261029-example
python bin/sync_manifest.py --check
kubectl apply -f apply/maintenance-app-deployment.yaml
```

该同步命令不会构建或推送镜像，首次部署前需自行完成镜像分发与存储准备。
`NERDCTL_IMPORT=1 ./bin/redeploy.sh` 仅适用于实时 app/worker 都通过 nodeSelector 固定
到本机节点的单节点部署；`IMPORT_NODE` 默认 hostname。多节点 worker 不能仅导入一个节点。

当前清单的实际存储边界：

| 角色 | 当前部署 |
|---|---|
| app | 单副本，旧历史包 PVC，固定 apk01；不克隆 nuwa |
| worker | 三副本、尽量跨节点；skopeo，无 containerd.sock；产物 emptyDir 20Gi |
| nuwa 缓存 | 每节点 hostPath；浅克隆，克隆/pull 共用文件锁 |
| Redis | 单副本、apk01 PVC + AOF、Recreate；仍是单节点故障点 |

业务镜像安装 skopeo，保留 archive 旧回退；entrypoint 从 Secret/环境生成 OSS 配置，
app 设置 SKIP_NUWA=1，worker 通过 NUWA_DIR 使用节点缓存。app 的历史 socket 挂载仍在
清单中，但新 worker 不依赖节点 containerd。

本轮 worker owner 协议变更发布前，应等待旧任务结束再切换 worker，避免不同协议混跑。
改 app/Redis 的 nodeSelector 前先确认存储，不能让两个 Redis 实例并写一份 AOF。

## 继续开发与已知边界

先读 [AGENTS.md](AGENTS.md) 和 [ISSUES.md](ISSUES.md)。完整审查证据见
[Review 快照](docs/PROJECT_REVIEW_2026-10-29.md)，当前实施进度见
[开发清单](docs/DEVELOPMENT_CHECKLIST.md)，交付现状见
[ARTIFACT_DELIVERY.md](docs/ARTIFACT_DELIVERY.md)。

保留的约束包括 Redis 单节点、仓库凭据策略、日志轮转缺失、客户升级脚本定制、上游空
patch/缺 make.sh 数据契约，以及真实 registry → OSS → 客户 Docker load 与生产发布验收。
当前不需要更换队列框架或重写前端；修改应小范围并保持对应故障回归。

发布中的 worker 使用 `Recreate` 策略，避免执行所有权协议的新旧实现混跑。
发布前先等待进行中的构建结束；脚本会显式同步该策略，因为仅更新镜像不会应用 YAML 中的策略变更。
