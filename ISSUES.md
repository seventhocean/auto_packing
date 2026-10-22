# 问题清单 — dev 分支

> 更新日期：2026-10-22
> 当前策略：**稳定优先**，非必要不修改构建主链路；凭据和升级包清理暂不处理。

## 仍需关注

### P1

**1. containerd 跨 namespace 历史残留**
- `default` namespace 的历史镜像可能导致 `nerdctl -n k8s.io save` 报
  `content digest ... not found`。
- 已做低风险治理：固定 `CONTAINERD_NAMESPACE=k8s.io`、新增只读巡检
  `bin/check_containerd.py`、处理手册 `docs/CONTAINERD_RUNBOOK.md`。
- 未修改 `pull_save.sh` 主流程。

**2. 多节点部署限制**
- `auto-pancking-pvc` 和 Redis PVC 都基于 hostPath + ReadWriteOnce。
- 没有 nodeAffinity；多节点集群中 Pod 调度到其他节点可能挂载失败。
- 当前单节点环境不受影响。

**3. `upgrade_packages/` 无自动清理**
- 历史包只增不减，长期会占满 20Gi PVC。
- 按当前策略暂不处理，建议先人工清理。

**4. 镜像仓库凭据硬编码**
- `bin/pull_save.sh` 中有明文账号密码。
- 内网环境暂不处理。

**5. 没有 CI**
- `tests/test_app_logic.py` 目前有 19 项单测，但需要手动执行。
- 建议后续加一个最小 CI job：安装依赖 + 跑单测 + 构建镜像。

**6. 升级预览不完整**
- 已显示“跨越 N 个 patch”。
- 镜像数量和预计包大小需要后端扫描 nuwa 差异，暂未实现。

### P2（当前规模可接受）

- **SSE 长连接占用 gunicorn 线程**：1~2 个用户规模下 2 workers × 4 threads 足够。
- **`logs/app.log` 无轮转**：日志量不大，但长期运行建议加轮转或启动截断。
- **PVC 名拼写错误**：`auto-pancking-pvc`，改名需要迁移，暂不动。
- **`bin/nerdctl` / `bin/ossutil` 随仓库分发**：约 46MB，可后续改为构建时下载。
- **`deepflow_patch_upgrade.sh` 目标环境写死**：目标仓库、组件列表、`upgrade_version` 需要按客户环境修改。
- **`tar.gz` 压缩收益低**：镜像层已压缩，长期可评估 OCI archive 或 `skopeo` 直出。
- **滚动更新会中断构建**：当前使用频率低，构建一般不会和更新重叠，暂不处理。

## 已完成的重点

- 拉取超时保护、升级包原子写入、V6 版本参数防路径穿越。
- 并发槽位改为 slot 锁 + TTL；Redis AOF + PVC 持久化。
- worker 心跳 10s/60s；Redis 异常、非法队列消息、已结束任务的处理。
- OSS/GitLab/仓库登录/拉取/保存重试；`/versions` 缓存与回退。
- SSE 增量日志与断线重连；任务可取消；V6 可搜索版本选择器。
- V7 输入校验/去重；升级预览与已有包匹配提示。
- Python 3.12 基础镜像与 Flask 3.1 等依赖升级。
- `redeploy.sh` 唯一 tag 发布；部署清单镜像 tag 固化。
- 仓库清理（跟踪文件 1469 → 29）；新增 19 项后端测试。
- containerd 只读巡检脚本与 runbook。

> 历史修复明细可通过 `git log --oneline` 查看，不在本文件重复维护。
