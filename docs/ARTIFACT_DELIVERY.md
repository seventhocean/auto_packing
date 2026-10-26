# 镜像交付与多节点架构评估

> 状态：方案评估，尚未实施  
> 目标：在保持客户侧“下载压缩包、逐个 `docker load`”体验不变的前提下，
> 降低当前方案对单台服务器、containerd 和本地文件的依赖，并为后续 3 节点部署做准备。

## 1. 背景与约束

当前系统已经能稳定产出升级包，但交付链路有几个强耦合：

- 镜像拉取和保存依赖节点上的 `containerd.sock` 和 `nerdctl`。
- `upgrade_packages/` 使用节点本地 hostPath PVC。
- Redis 使用节点本地 PVC。
- `nuwa` 通过 SSH key 克隆，OSS 通过容器内 `ossutil` 访问。
- 客户只接受**压缩包**，并且习惯解压后逐个 `docker load`。

因此，评估的重点不是“换一个更先进的工具”，而是在**不改变客户交付方式**的前提下，
选择可靠性更高、节点依赖更少、后续多节点扩展更简单的实现。

## 2. 当前实现与耦合点

| 依赖 | 当前方式 | 单节点是否可用 | 多节点问题 |
|---|---|---|---|
| 镜像拉取/保存 | `nerdctl pull` + `nerdctl save` | 是 | 依赖节点 containerd，layer 受 namespace/GC 影响 |
| 镜像存储 | 节点 containerd content store + 临时 tar | 是 | 每个节点独立缓存，无法共享 |
| 升级包存储 | hostPath PVC，RWO | 是 | Pod 漂移到其他节点可能挂载失败 |
| Redis | hostPath PVC，单实例 | 是 | Pod 漂移后数据不可用 |
| GitLab | SSH key Secret | 是 | 每个 Pod 需要能访问 GitLab；SSH key 需持续维护 |
| OSS | 容器内 `ossutil` + Secret | 是 | 无宿主机安装问题，但当前只用于版本列表 |
| 下载 | Flask `send_file` 本地文件 | 是 | 多 app 副本必须共享同一份文件 |
| 并发 | `max_concurrent_tasks=1` | 是 | 3 节点需要调大并做好调度 |

真正和服务器强绑定的是 **containerd.sock**；`ossutil` 和 GitLab key 都在镜像/Secret 中，
不需要每台宿主机安装。但 `containerd` 的本地缓存、GC、namespace 问题会放大到每个节点。

## 3. 目标与非目标

### 目标

1. 客户侧交付物保持“一个压缩包 + 多个镜像 tar + 逐个 `docker load`”。
2. worker 不再依赖节点 `containerd.sock`，可以调度到任意节点。
3. 支持 3 个 worker 并行构建，且每个节点不要求安装 `ossutil`/配置 GitLab SSH key。
4. 升级包不依赖单节点本地磁盘，可被多个 app 副本下载。
5. 镜像来源可追溯：保留完整镜像引用，尽量记录 digest。
6. 保持现有外层打包、升级脚本和客户操作方式不变。

### 非目标

- 不做镜像仓库的完整替代品。
- 不要求客户必须能访问镜像仓库。
- 不引入必须由客户安装的新工具。
- 不在本阶段重写任务队列或前端。

## 4. 可选方案

### 方案 A：保留 nerdctl + 共享 RWX 存储

- 每个节点一个 worker，继续挂载本机 `containerd.sock`。
- `upgrade_packages` 改为 RWX（NFS/CephFS/Longhorn）。
- Redis 改用网络存储或托管实例。
- 镜像推 registry，或者在每个节点手动导入。

**优点**：改动最小，`pull_save.sh` 不用动。  
**缺点**：仍然依赖节点 containerd；跨 namespace、GC、镜像缓存问题依旧存在；
多节点部署需要每个节点都有可用的 containerd 和足够磁盘。

### 方案 B：skopeo 直接生成 docker-archive（推荐）

把 `nerdctl pull + save` 替换为：

```bash
skopeo copy \
  --override-os linux \
  --override-arch amd64 \
  --src-creds "$REGISTRY_USER:$REGISTRY_PASSWORD" \
  docker://hub.deepflow.yunshan.net/dev/deepflow-agent:v6.6.6001 \
  docker-archive:/work/images/deepflow-agent_v6.6.6001.tar:hub.deepflow.yunshan.net/dev/deepflow-agent:v6.6.6001
```

产物仍然是**每个镜像一个 docker-archive tar**，客户可以直接 `docker load`。
外层仍然由现有逻辑打成 `.tar.gz`。

**优点**：

- 不需要 `containerd.sock`，worker 可以在任意节点运行。
- 不经过 containerd content store，规避跨 namespace/GC 导致的 `content digest not found`。
- 可以直接按平台导出（`--override-arch amd64`），避免多架构 layer 问题。
- 保留完整镜像引用，客户 load 后 tag 与现在一致。
- 不需要宿主机安装 `nerdctl`/`ossutil`，工具都在镜像里。

**缺点**：

- 需要把 `skopeo` 加入 worker 镜像。
- 需要确认 registry 认证/TLS 配置。
- 每个镜像仍要占用临时磁盘；外层 gzip 对已压缩镜像层收益有限，但客户要求压缩包，保留即可。

### 方案 C：registry-to-registry 直接同步

如果客户有可访问的镜像仓库，可以完全跳过 tar：

```bash
skopeo copy \
  docker://source/deepflow-agent:v6.6.6001 \
  docker://customer-registry/deepflow-agent:v6.6.6001
```

**优点**：交付物最小，客户不需要下载大包。  
**缺点**：客户必须能访问仓库；很多售后场景不具备该条件。  
**定位**：作为可选的高级交付方式，不作为默认路径。

### 方案 D：OCI archive

skopeo 也可以生成 `oci-archive`，但不建议作为默认交付格式：

- 客户当前使用 `docker load`，对 OCI archive 的兼容性不如 docker-archive。
- 现有升级脚本按 `*.tar` 遍历，docker-archive 更匹配。

因此，**默认坚持 docker-archive**；OCI archive 只作为内部或未来工具链选项。

## 5. 方案对比

| 维度 | A: nerdctl + RWX | B: skopeo + OSS | C: registry 直推 |
|---|---|---|---|
| 客户压缩包/逐个 load | 保持 | 保持 | 不适用 |
| 去掉 containerd.sock | 否 | **是** | 是 |
| 多节点 worker 调度 | 受限 | **自由** | 自由 |
| 规避 namespace/GC 问题 | 否 | **是** | 是 |
| 需要客户仓库 | 否 | 否 | 是 |
| 临时磁盘 | 需要 | 需要 | 很少 |
| 运维复杂度 | 中 | 中 | 高（依赖客户环境） |
| 推荐度 | 过渡方案 | **默认推荐** | 可选高级方案 |

结论：**方案 B 最适合当前“给客户发压缩包”的现实约束**；方案 C 作为未来可选能力，
不替代默认交付。

## 6. 推荐目标架构

```text
Ingress / Service
        │
   app x2~3（无状态）
        │
   Redis（托管 / Sentinel）
        │
   worker x3（每节点一个，不再挂 containerd.sock）
        │
   skopeo ──► 每镜像 docker-archive tar
        │
   OSS（最终 .tar.gz、历史包、签名下载）
```

关键点：

- **worker 无状态**：只依赖镜像、registry、OSS、Redis；不依赖节点 containerd。
- **app 无状态**：下载走 OSS 签名 URL，不再依赖本地 `upgrade_packages`。
- **Redis 外置**：托管 Redis 最省事；自建 Sentinel 需要适配 app.py 的连接方式。
- **镜像分发**：app/worker 镜像必须推 registry，不能只在单节点 `nerdctl load`。
- **GitLab**：优先改为 HTTPS read-only token，减少 SSH key 维护；如果继续用 SSH，
  Secret 挂载即可，不需要宿主机安装 key。
- **OSS**：`ossutil` 继续放在镜像内，不需要宿主机安装。

## 7. 交付物规范

建议最终压缩包结构保持不变，同时增加清单和校验：

```text
deepflow_patch_v669_01_13.tar.gz
└── deepflow_patch_v669_01_13/
    ├── deepflow-agent_v6.6.6001.tar
    ├── deepflow-server_v6.6.5617.tar
    ├── patch_image_tag_list.txt
    ├── manifest.json
    ├── SHA256SUMS
    └── deepflow_patch_upgrade.sh
```

`manifest.json` 建议包含：

```json
{
  "package": "deepflow_patch_v669_01_13.tar.gz",
  "created_at": "2026-10-26T12:00:00Z",
  "source": {"current": "01-20250430-26798-BUG", "target": "13-20250528-4661-BUG"},
  "platform": "linux/amd64",
  "images": [
    {
      "ref": "hub.deepflow.yunshan.net/dev/deepflow-agent:v6.6.6001",
      "digest": "sha256:...",
      "size": 12345678,
      "file": "deepflow-agent_v6.6.6001.tar"
    }
  ],
  "sha256": "整包 sha256，可选"
}
```

客户侧流程保持：

```bash
tar -xzf deepflow_patch_v669_01_13.tar.gz
cd deepflow_patch_v669_01_13

# 可选校验
sha256sum -c SHA256SUMS

# 逐个导入
for f in *.tar; do
  docker load -i "$f"
done

# 或使用包内升级脚本
./deepflow_patch_upgrade.sh
```

## 8. skopeo 关键命令

单镜像导出：

```bash
skopeo copy \
  --override-os linux \
  --override-arch amd64 \
  --src-creds "$REGISTRY_USER:$REGISTRY_PASSWORD" \
  docker://hub.deepflow.yunshan.net/dev/deepflow-agent:v6.6.6001 \
  docker-archive:/work/images/deepflow-agent_v6.6.6001.tar:hub.deepflow.yunshan.net/dev/deepflow-agent:v6.6.6001
```

固定 digest（避免 tag 漂移）：

```bash
DIGEST=$(skopeo inspect \
  --creds "$REGISTRY_USER:$REGISTRY_PASSWORD" \
  docker://hub.deepflow.yunshan.net/dev/deepflow-agent:v6.6.6001 \
  --format '{{.Digest}}')

skopeo copy \
  --override-os linux --override-arch amd64 \
  --src-creds "$REGISTRY_USER:$REGISTRY_PASSWORD" \
  "docker://hub.deepflow.yunshan.net/dev/deepflow-agent@${DIGEST}" \
  "docker-archive:/work/images/deepflow-agent_v6.6.6001.tar:hub.deepflow.yunshan.net/dev/deepflow-agent:v6.6.6001"
```

注意：

- `docker-archive:path:ref` 的最后一段决定客户 `docker load` 后看到的镜像名。
- 不要用 `oci-archive` 作为默认客户交付格式。
- 内网自签证书需要 `--src-tls-verify=false` 或配置 CA，建议优先配置 CA。
- 每个镜像单独一个 tar，符合客户逐个 load 的习惯。

## 9. 多节点部署要求

### app

- `replicas: 2~3`，用 topologySpread/anti-affinity 打散。
- 不再需要 `containerd.sock`。
- 下载走 OSS 签名 URL 时，不需要共享 `upgrade_packages` PVC。
- Ingress 需要关闭 response buffering，并放宽 SSE 读超时。

### worker

- `replicas: 3`，每个节点一个，用 topologySpread 约束。
- 去掉 `containerd.sock` 挂载（skopeo 方案）。
- 需要临时磁盘；用 emptyDir 并设置 sizeLimit，或使用节点本地盘。
- 需要访问 registry、OSS、GitLab/Redis。
- `max_concurrent_tasks` 调到 2~3，先观察网络和磁盘，不建议直接 3 并发拉大镜像。

### Redis

- 推荐托管 Redis，app/worker 直接使用现有 host/port 配置，不需要改代码。
- 自建 Sentinel 需要把 app.py 的 `redis.Redis` 改为 `redis.Sentinel`，并增加配置项。
- 不要继续使用单节点 hostPath + AOF 作为 3 节点方案。

### 镜像分发

- `auto_packing:tag` 必须推 registry；`NERDCTL_IMPORT` 只适合单节点。
- 每个节点都需要能拉取 app/worker 镜像。
- 建议 `bin/redeploy.sh` 使用 `REGISTRY=...` 推送，而不是只在本机导入。

### 存储

- 推荐 OSS 存升级包，避免 RWX PVC。
- 如果暂时不用 OSS，必须提供 RWX 存储给 `upgrade_packages`。
- Redis 使用网络块存储或托管实例，不要放 NFS。

## 10. 分阶段落地

### 阶段 0：POC（1 天）

1. 在一个普通 Pod/VM 里运行 skopeo，导出一个镜像到 docker-archive。
2. 用客户同版本 Docker 执行 `docker load -i`，确认镜像名、tag、层完整。
3. 对比现有的 `nerdctl save` 产物，确认升级脚本无需修改。

### 阶段 1：替换单镜像导出

1. Dockerfile 安装 skopeo。
2. `pull_save.sh` 增加 `COPY_TOOL=skopeo` 分支，保留 nerdctl 回退。
3. 保持输出文件名、外层打包、`patch_image_tag_list.txt` 和升级脚本不变。
4. 跑现有 23 个测试 + 一个真实镜像的端到端验证。

### 阶段 2：去除 containerd 依赖

1. worker 去掉 `containerd.sock` 挂载。
2. `max_concurrent_tasks` 调到 3，worker 副本 3，加 topologySpread。
3. 验证每个节点都能独立完成一次构建。

### 阶段 3：OSS 与无状态 app

1. worker 打包完成后上传 OSS。
2. task meta 记录 OSS object key；`/download` 返回签名 URL。
3. `/existing-packages` 改为 OSS 列举，配置生命周期清理。
4. app 扩到 2~3 副本，移除共享 `upgrade_packages` PVC。

### 阶段 4：GitLab/Redis 收敛

1. GitLab 改 HTTPS read-only token，或继续 Secret 挂载 SSH key（无需宿主机安装）。
2. Redis 改托管或 Sentinel。
3. 增加 package manifest/digest 校验。

## 11. 风险与验证

| 风险 | 应对 |
|---|---|
| skopeo 导出的 tar 客户 `docker load` 不兼容 | 阶段 0 用客户同版本 Docker 实测 |
| 多架构镜像选错平台 | 固定 `--override-os linux --override-arch amd64` |
| registry 自签证书/TLS | 配置 CA，临时可 `--src-tls-verify=false` |
| tag 被覆盖 | 先 inspect 取 digest，再按 digest 复制 |
| 3 并发打满网络/磁盘 | 先 2 并发，监控节点磁盘和 registry 带宽 |
| OSS 上传/下载成本 | 使用生命周期清理，控制保留天数 |
| app 多副本下载不一致 | 走 OSS 签名 URL，或 RWX 共享存储 |
| Redis 单点 | 托管 Redis 或 Sentinel；AOF/PVC 不能解决节点漂移 |
| 镜像分发遗漏 | 用 registry 推送；不要依赖单节点 import |
| V6 同版本对包名冲突 | 保持原子替换；必要时在包名中加入 task id |

验收标准：

- 同一个镜像用 skopeo 导出的 tar 能被客户 `docker load`，镜像名/tag 与现在一致。
- 包内每个 `.tar` 可单独加载，升级脚本仍可遍历执行。
- 3 个 worker 在 3 个节点上并发构建，均不需要 `containerd.sock`。
- 任一 app 副本都能生成/返回同一个包的下载地址。
- 节点故障后 Redis/worker 恢复，任务能回收或重试。
- `default` namespace 在各节点保持为空（如果仍保留 nerdctl 回退）。

## 12. 结论

- **客户交付形态不变**：仍然是一个 `.tar.gz`，里面是每个镜像的 docker-archive tar。
- **默认推荐方案 B**：skopeo 直接生成 docker-archive，外层继续用现有 tar.gz 打包。
- **多节点关键不是安装 ossutil/SSH key**，而是去掉对节点 containerd 的依赖、把升级包放到 OSS、
  把 Redis 外置，并保证每个节点能拉取 app/worker 镜像。
- **方案 C（registry 直推）** 适合有仓库访问能力的客户，作为可选模式，不替代离线压缩包。
- 建议先做阶段 0/1 的 POC，验证客户 `docker load` 兼容性，再进入多节点改造。
