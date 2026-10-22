# containerd 跨 namespace 镜像问题处理手册

> 适用场景：`nerdctl pull` 显示成功，但 `nerdctl save` 报
> `failed to get reader: content digest sha256:...: not found`。

## 1. 现象

```text
镜像拉取成功：hub.deepflow.yunshan.net/dev/deepflow-agent:v6.6.6001
开始保存镜像到：xxx.tar
level=fatal msg="failed to get reader: content digest sha256:901f6a4c...: not found"
保存失败（第 1/3 次）...
```

重试 `save` 没有意义：缺失的是 content 记录，不是本次写文件失败。

## 2. 根因

containerd 的 blob 文件按 digest 全局存放在磁盘上，但 content 元数据/引用是
**按 namespace 隔离**的。

- 镜像记录在 `k8s.io`；
- 部分 layer 的 content 记录只在 `default`；
- `nerdctl -n k8s.io save` 只能读 `k8s.io` 名下的记录，于是报
  `content digest ... not found`。

常见来源：历史脚本/工具在 `default` namespace 拉过镜像，后续镜像记录被
复制或引用到 `k8s.io`，但 layer 记录没有跟着补齐。

## 3. 只读巡检

仓库提供 `bin/check_containerd.py`，只执行 `ls` / `inspect`，不删除、不写入：

```bash
# 在节点上（需要 ctr；nerdctl 可以用仓库自带二进制）
python3 bin/check_containerd.py --nerdctl ./bin/nerdctl

# 只检查 default 是否为空
python3 bin/check_containerd.py --quick

# 指定可执行文件路径
python3 bin/check_containerd.py \
  --ctr /usr/bin/ctr \
  --nerdctl /path/to/nerdctl
```

输出示例：

```text
namespace: k8s.io  镜像: 30  content: 1234
namespace: default  镜像: 0
检查完成：未发现问题
```

发现 `default` 有镜像或 layer 缺失时退出码为 1，可直接用于巡检告警。

## 4. 应急修复（先确认、再执行）

> 以下操作会读取并写入 containerd content，属于应急修复，不是日常步骤。
> 只在确认某个 digest 在目标 namespace 缺失、且在另一个 namespace 可读时使用。

```bash
DIGEST=sha256:901f6a4c...

# 1. 确认目标 namespace 缺失
ctr -n k8s.io content ls | grep "${DIGEST#sha256:}" || echo "missing in k8s.io"

# 2. 确认来源 namespace 有该 content
ctr -n default content ls | grep "${DIGEST#sha256:}"

# 3. 从来源 namespace 读 blob，ingest 到目标 namespace
ctr -n default content get "$DIGEST" | ctr -n k8s.io content ingest "$DIGEST"

# 4. 验证并重新 save
ctr -n k8s.io content ls | grep "${DIGEST#sha256:}"
nerdctl -n k8s.io save --platform linux/amd64 -o /tmp/test.tar <image>
```

注意：

- digest 必须带 `sha256:` 前缀；
- `content get` 会把整个 blob 流出来，只用于单个/少量 layer 的修复；
- 不要用 `save` 重试代替修复。

## 5. 如果重新 pull 也拉不下来

`nerdctl pull` 可能因为镜像记录已存在而跳过缺失内容。此时：

1. 确认该镜像没有被任何 Pod 使用；
2. 删除镜像记录（不要直接删 digest/content）；
3. 重新拉取并立即保存：

```bash
nerdctl -n k8s.io rmi <image>
nerdctl -n k8s.io pull --unpack=false --platform linux/amd64 <image>
nerdctl -n k8s.io save --platform linux/amd64 -o /tmp/test.tar <image>
```

`--unpack=false` 可以避免 containerd 丢弃 layer；`--platform` 可以避免
多架构/attestation layer 缺失。

## 6. 清理规范（避免误删）

1. **先盘点再删除**
   ```bash
   kubectl get pods -A -o jsonpath='{range .items[*]}{range .spec.containers[*]}{.image}{"\n"}{end}{end}' | sort -u
   crictl images
   ctr -n k8s.io images ls
   ```
2. 生成保留清单，dry-run 审核候选列表；
3. 优先删除镜像记录/tag，让 containerd GC 回收 blob，不要手删 digest 引用；
4. 分批执行，每批后检查 Pod、apiserver、镜像数量；
5. 重操作加超时和优先级：
   ```bash
   timeout 300 nice -n 19 ionice -c3 du -sx /var/lib/containerd
   ```
6. 远程执行确保能杀掉整个进程组，本地客户端断开不代表远端进程结束；
7. 维护窗口内操作，提前备份关键镜像/记录，确认 registry 可达。

## 7. 预防

- 所有镜像操作固定 `-n k8s.io`；worker 容器设置
  `CONTAINERD_NAMESPACE=k8s.io`。
- 定期运行 `bin/check_containerd.py`，`default` namespace 非空即告警。
- 节点 GC / containerd 重启后，跑一次“pull + save”冒烟验证。
- 长期方案可评估专用 containerd namespace 或 `skopeo copy`，避免依赖
  containerd 本地内容状态；在稳定运行前不做大改。
