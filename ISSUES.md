# 问题清单 — dev 分支

> 更新日期：2026-10-26
> 当前策略：**稳定优先**，非必要不修改构建主链路；凭据和升级包清理暂不处理。

## P1：优先关注

> **2026-10-28 已处理**：节点上 `redis:7.2-alpine` 这个 tag 的镜像内容原本是 Redis **7.0.12**，
> 而 PVC 里的 AOF 由 **7.2.5** 写入（RDB version 11），所以重建 redis Pod 会 CrashLoop：
> `Can't handle RDB format version 11 ... AOF loading aborted`。
> 已导入真正的 Redis **7.2.17** 覆盖该 tag，并先用数据副本验证可正常加载后才切换；
> 同时给 redis 加了 `strategy: Recreate`（单实例 + RWO 卷，绝不能新旧 Pod 并写）
> 和 nodeSelector 钉到 apk01。切换前数据备份在 apk01 的 `/root/redis-data-backup-*.tar.gz`。

**1. containerd 跨 namespace 历史残留**
- `default` namespace 的历史镜像可能导致 `nerdctl -n k8s.io save` 报
  `content digest ... not found`。
- 已做低风险治理：固定 `CONTAINERD_NAMESPACE=k8s.io`、只读巡检
  `bin/check_containerd.py`、处理手册 `docs/CONTAINERD_RUNBOOK.md`。
- 未修改 `pull_save.sh` 主流程。

**2. 多节点部署限制**
- 升级包和 Redis 都使用 hostPath + ReadWriteOnce，且没有 nodeAffinity。
- 多节点集群中 Pod 调度到其他节点可能挂载失败。
- 当前按单节点部署理解；多节点需要 nodeAffinity 或共享存储。

**3. `upgrade_packages/` 无自动清理**（暂不处理）
- 历史包只增不减，长期会占满 20Gi PVC。

**4. 镜像仓库凭据硬编码**（暂不处理）
- `bin/pull_save.sh` 中有明文账号密码；内网环境暂不调整。

## P2：体验/可靠性

**5. SSE 断连后允许直接新建构建**
- `handleStreamLost()` 会把 `isBuilding` 设为 `false`，但旧任务可能仍在运行。
- 用户可能重复提交，且当前界面会丢失旧任务跟踪。
- 建议保留“任务恢复中”状态，或提示先去任务中心确认。

**6. 构建中切换 V6/V7 会被强制拉回**
- `handleBuildStatus()` 每条 SSE 都按 `task_type` 校准面板。
- 用户切到另一模式准备下一单时会被拉回；建议只在首次恢复任务时校准。

**7. Worker 在线阈值偏短**
- `app.py` 使用 30 秒判断，心跳 TTL 是 60 秒。
- Redis 抖动时可能短暂显示“Worker 离线”；建议阈值与 TTL 对齐。

**8. 任务中心轮询无超时**
- `refreshTaskOverview()` 没有 AbortController/超时。
- 请求挂住时 `overviewLoading` 会一直为 `true`，后续轮询被跳过。

**9. V7 不支持带端口的 registry**
- 校验正则的镜像名部分不允许 `:`，`registry:5000/image:tag` 会被判为格式错误。
- 同时确认 `pull_save.sh` 对完整 registry 地址的前缀处理。

**10. 没有 CI**
- 当前有 23 个后端单测，但只能手动执行。
- 建议加最小 CI：安装依赖 + 单测 + 镜像构建。

**11. `logs/app.log` 无轮转**
- 长期运行会持续增长，建议加大小限制或启动截断。

**12. 任务中心只保留最近 5 条完成记录**
- 更早记录只能通过 Redis TTL 或升级包列表查看；需要历史可加查询接口。

## P3：细节/文档

- HTTP 环境下 `window.isSecureContext=false`，浏览器通知功能实际不会工作。
- `copyCurlCommand` 使用 `window.location.origin`，部署到子路径/Ingress 下会缺路径。
- `bin/nerdctl` / `bin/ossutil` 约 46MB 仍在仓库中。
- PVC 名拼写 `auto-pancking-pvc`。
- `deepflow_patch_upgrade.sh` 的目标仓库、组件列表、`upgrade_version` 仍写死。
- `max_concurrent_tasks=1` 与 2 个 worker 副本：单节点可把副本降为 1；保留 2 个则多一个空闲副本作为故障备份。

## 已完成的重点

- 回收失活任务时清理 slot，并增加 `build:reclaim:lock` 互斥锁，避免单并发阻塞和重复入队。
- 23 个后端单测，覆盖槽位、取消、回收、日志、SSE、下载、任务中心等。
- 任务中心：进行中/最近完成、Worker 在线、队列、槽位、磁盘余量。
- 前端：可搜索版本选择器、V7 胶囊/批量编辑、流水线步骤条、历史包抽屉、SSE 断线恢复。
- 拉取超时保护、原子打包、V6 参数防路径穿越、Redis AOF + PVC、OSS/GitLab 重试。
- Python 3.12 / Flask 3.1 / gunicorn 23 / redis 5.2。
- `bin/redeploy.sh` 唯一 tag 发布，部署清单镜像 tag 固化。
- 仓库清理和 containerd 只读巡检 + runbook。

> 历史修复明细通过 `git log --oneline` 查看，不在本文件重复维护。
