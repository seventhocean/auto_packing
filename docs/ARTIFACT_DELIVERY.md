# 镜像交付与多节点部署现状

> 更新日期：2026-10-29。本文件描述当前仓库实现、剩余边界和目标环境验收。
> 原有 skopeo + OSS 方案已落到代码；不能再将整份方案标为“尚未实施”。
> 代码修复和自动化回归不等同于已完成生产发布、OSS 与客户 Docker 验收。

## 1. 交付约束

客户操作保持：下载 tar.gz、解压、逐个 docker load；包内仍包含镜像 tar、镜像清单和
现场升级脚本。当前导出平台固定 linux/amd64，默认使用 docker-archive，未切换为 OCI。

服务规模为 1~2 个用户，稳定优先。现有 Web 入队、worker 串行消费、Redis 状态与
执行权模型继续使用，不引入新队列框架或 registry 直推客户作为默认交付流程。

## 2. 已落地的实现

```text
Flask Web / SSE
       │ POST 入队、状态观察
       ▼
Redis：queue / processing / owner slot / heartbeat
       │
worker x3（每进程串行，调度尽量分散）
       ├─ nuwa 节点缓存 + OSS 版本列表 → patch 差异
       ├─ custom → 已校验的镜像清单
       ▼
image_refs.py → skopeo → 每镜像 docker-archive → 原子 tar.gz
                                                   │
                                   任务/attempt 独立本地归档
                                                   │
                                          OSS 独立对象
                                                   │
                          app 下载入口 → 302 签名 URL
```

| 项目 | 当前实现 | 仍存在的边界 |
|---|---|---|
| 镜像导出 | worker 使用 skopeo，固定平台；逐镜像临时文件后替换 | 客户 Docker 同版本 load 需要真实验收；未固定 digest |
| 引用与文件名 | API/脚本共用解析；完整仓库/端口；去重；路径引用追加哈希 | 不支持 digest 和隐式 latest；内部仓库之外的私有认证需另行配置 |
| 产物正确性 | 全部输入先校验；导出 TSV 与原清单逐项核对 | 有效非空文件检查不能替代客户实际导入验证 |
| OSS 交付 | 上传成功后才完成；每次交付独立对象；失败保留归档 | emptyDir 无法跨 Pod 重建恢复 |
| 下载 | 确认的对象签名；按钮直接导航；curl 跟随跳转 | bucket 权限、真实下载与网络仍需环境验收 |
| 保留策略 | 本地和纯 OSS 合并候选，数量/容量/最近/年龄保护 | OSS 生命周期若另有配置需与应用策略协调 |
| 配置 | config.yaml 真源，生成 ConfigMap，CI 检查漂移 | 修改后需同步并按发布流程使进程重启生效 |
| 发布 | registry 分发；按容器名同步镜像；受限单节点 import | 未自动执行生产发布；所有目标节点必须可拉取镜像 |
| 故障恢复 | 执行者 token、原子回收、原子日志、进程组取消/超时 | Redis 单节点故障与旧协议混跑不能由 AOF 解决 |

## 3. 产物与存储名称

逻辑包名保持客户可识别的格式：

```text
deepflow_patch_v669_01_13.tar.gz
deepflow_patch_custom_<task_id>.tar.gz
```

包内结构：

```text
deepflow_patch_v669_01_13/
  deepflow-agent_v6.6.6001.tar
  deepflow-server_v6.6.5617.tar
  patch_image_tag_list.txt
  deepflow_patch_upgrade.sh
```

默认 DeepFlow dev 仓库的单组件保留普通 tar 名；其他 namespace/registry 的同名镜像
带引用哈希，防止 team-a/agent 与 team-b/agent 覆盖。导入方式仍是遍历所有 *.tar，
不依赖从 tar 文件名重建镜像引用。

构建运行目录的 `image_exports.tsv` 包含完整引用和导出文件名，全部成功后原子发布，
后端验证引用、文件唯一性及非空文件再归档。它是运行时核对依据；当前没有宣称已向客户
交付正式 manifest.json、digest 或 SHA256SUMS，这些属于后续可选增强。

OSS 对象名和逻辑包名分开：task meta 的 `package_name` 为逻辑名，`oss_object_name`
为带唯一后缀的远端文件名，`oss_uri` 只在确认上传成功后设置。同版本对的独立构建不再
覆盖同一个对象；任务下载使用自己的对象，历史列表保留真实对象名。

## 4. 成功、上传失败与重试

1. OSS 模式先归档至 `.pending/<task_id>/<attempt>/`，独立于可下载的历史包根目录。
2. 上传有上限和有限自动重试；成功写入完成状态后才公布 download_url，并可删除本地副本。
3. 仍失败时，任务为交付错误，保留归档、没有成功下载入口，不把旧同名对象当作本次产物。
4. 默认失败状态保留一小时。原 worker 在线且归档仍在时，可请求
   `POST /build/<task_id>/retry-upload`，只上传已有文件，不重新导出镜像或归档。
5. 上传重试进专属 worker 队列；原 worker 已离线或文件不可用时明确要求重新构建。
   Pod 重建的 emptyDir 无法从新 worker 找回，不能把该功能描述成持久化失败恢复。
6. 过期且已无任务状态的失败归档随后续保留策略执行回收，不删除仍被任务使用的文件。

短期任务下载入口默认 90 秒，OSS 签名有效期默认 900 秒；历史包在对象仍保留时可重新
签名下载。签名本身不证明本次对象上传成功，任务下载需先有成功交付状态和对应 oss_uri。

## 5. worker 执行权与回收

slot 值是 worker/attempt token。Lua 原子检查容量、绑定执行权，重复消息不能把
“相同 task_id 的 slot 已存在”当作自己的执行成功。状态、日志、slot 释放和取消清理
校验执行者，失去执行权的旧尝试不能覆盖新尝试状态。

失活回收在 Redis 同一次 Lua 操作中检查心跳、移除对应旧执行者 slot、转移 processing
消息。回收锁用于串行扫描；原子移动用于解决“入队成功但回包丢失”后的重试，不再依赖
分开的 RPUSH 和 DELETE。保留失活 slot 清理是必要条件。

心跳默认 10 秒 / TTL 60 秒，展示在线阈值与 TTL 对齐。等槽位不因固定十分钟失败。
每次构建使用独立 attempt 临时目录和日志，取消、超时、日志写入异常或执行权失效时
结束子进程组，归档和上传也检查执行权与取消。

本轮协议变更不能和不识别 owner 的旧 worker 随意混跑。真实发布前先等旧执行任务结束，
再切换 worker；生产发布和回滚步骤需把这一点作为执行条件。

## 6. 当前 Kubernetes 存储边界

| 角色 | 存储与调度 |
|---|---|
| app | 单副本，仍挂 apk01 的旧包 PVC，nodeSelector 固定 apk01；SKIP_NUWA=1 |
| worker | 三副本，topologySpread 尽量分散；产物 emptyDir 20Gi；无 containerd.sock |
| nuwa | 每节点 hostPath 缓存，通过 NUWA_DIR 指定；浅克隆，克隆/pull 共用 flock |
| Redis | 单实例、apk01 本地 PVC + AOF、Recreate；仍是单节点故障点 |

worker 的 `REQUIRE_OSS_STORAGE=1` 防止独立产物目录误配置为 local 后端。
app 的历史 containerd socket 挂载仍在清单中，但新 worker 主流程不再依赖它。
删除 app 历史挂载、扩展 app 副本、外置 Redis 属于后续独立变更。

修改 app/Redis nodeSelector 之前必须确认存储可达性；Recreate 防止两个 Redis 实例
同时写一份 AOF，但不提供节点故障自动切换。三个 worker 能跨节点不等于服务已高可用。

## 7. 配置与镜像分发

业务配置只维护 `config.yaml`，生成/检查部署 ConfigMap：

```bash
python bin/sync_manifest.py
python bin/sync_manifest.py --check
```

发布机需要 Python 3/PyYAML、Docker 和 kubectl。多节点使用可被所有目标节点拉取的仓库：

```bash
REGISTRY=registry.example.com/team ./bin/redeploy.sh
```

脚本同步完整 ConfigMap，按 app/worker 容器名称更新任意原镜像前缀，连续发布后清单
仍保留最新 tag。直接应用已同步清单与脚本发布的业务配置来源一致。

本机 import 只允许实时 app/worker 都固定在导入节点时执行，IMPORT_NODE 默认 hostname；
不满足就拒绝，不能只把镜像导入 apk01 后让新 worker 去 apk02/03 调度。
首次部署仍需先准备 Secret/PVC、推送业务镜像，再同步镜像并 apply；同步工具不构建或推送镜像。

## 8. 保留策略

保留策略对本地包和纯 OSS 对象共同计算数量与容量，同名的本地/远端对象合并为一个候选。
默认最多 40 个或 60GiB，同时保护最新至少 3 个、30 分钟内的新对象和当前交付对象。
回收使用 Redis 锁；远端列举异常时停止，不能用部分列表计算保护范围。
远端删除失败时保留候选，dry_run 只报告而不删除。

运行前需确认 OSS 前缀属于本工具管理范围，避免把其他业务对象混入候选。已有 OSS
生命周期配置如需沿用，应与应用保留策略一并核对；本轮未自动修改 bucket 生命周期。

## 9. 检查、目标环境验收与后续方向

`bash bin/check.sh` 运行普通回归和配置检查。可选 TEST_REDIS_URL 只连接本地可丢弃
Redis，使用随机前缀验证真实 Lua、队列和过期行为。Linux CI 同时运行临时 Redis 集成
与镜像构建。自动化测试数量以当前检查输出为准，避免文档与用例数再次漂移。

生产发布前仍需目标环境验证：

- 每个节点可以拉取业务镜像，并独立完成一次构建。
- 真实 registry 导出 tar 后在客户同版本 Docker 上逐个 load，镜像名/tag/数量正确。
- 两个不同路径的同名镜像均存在；同版本对同时构建的对象不互相覆盖。
- OSS 上传失败不公布成功，原 worker 在线可仅重试上传；离线/重建则明确不可恢复。
- 任务下载、历史列表、浏览器下载和复制 curl 命令都获得正确包。
- 纯 OSS 保留策略和保护规则实际符合预期，取消/进程退出/worker 失活没有遗留执行。
- 发布前旧任务结束，配置生效，连续发布和回滚的镜像/状态协议一致。

尚未实施的方向按需求独立推进：Redis 外置或故障切换、app 完全去本地历史卷、多副本
Web、镜像 digest 固定与客户校验清单、日志轮转、通用现场升级脚本。registry-to-registry
直推客户和 OCI archive 可另做可选模式，不替代当前压缩包交付。

worker 发布采用 Recreate 以隔离执行协议升级；发布前应排空进行中的构建。
