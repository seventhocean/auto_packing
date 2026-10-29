# 问题清单 — dev 分支

> 更新日期：2026-10-29。策略：小规模使用、稳定优先。
> 本文件区分已修复代码与仍需真实环境验收；历史审查证据保留在
> [项目 Review 快照](docs/PROJECT_REVIEW_2026-10-29.md)，执行进度见
> [开发清单](docs/DEVELOPMENT_CHECKLIST.md)。

## 仍需处理或验收

### 1. Redis 仍依赖单节点存储

Redis 为 apk01 上的单实例 PVC + AOF，Recreate 防止两个实例并写。AOF 能恢复进程重启，
不能解决节点和本地盘故障；app 的旧包 PVC 也仍固定在 apk01。
未来多副本 Web 或 Redis 高可用需要另行设计存储/故障切换，不在本轮自动迁移。

### 2. 仓库凭据和客户升级脚本仍为内部定制

按现有策略，`pull_save.sh` 中仓库凭据暂不治理；不要在日志或报告里重复输出。
`deepflow_patch_upgrade.sh` 的现场路径、目标仓库、组件列表仍需交付前核对。
patch 模式自动填入 upgrade_version，不表示脚本已适配每个版本线或每个客户。
自定义清单虽能导出任意合法引用，包内定制升级脚本并不因此成为通用 registry 迁移工具。

### 3. 日志无轮转

Redis 日志有条数和 TTL 上限，`logs/app.log` 本地文件仍会增长。后续可以配置大小轮转或
接入容器日志采集；本轮未改变持久化日志策略。

### 4. 上游空 patch / 缺 make.sh 契约未确认

`get_patch_image_tag_list.sh` 仍允许跳过缺少 make.sh 的 patch。仓库中可能存在合法空变更、
仅基础镜像或其他发布形态，需上游明确哪些文件必须存在。不能在没有契约时把所有缺失
都当成错误，也不能把当前行为当作完整性已得到端到端证明。

### 5. 真实外部链路和发布待目标环境验收

本轮自动化主要使用临时文件、工具替身及可选本机 Redis。发布前仍需验证：

- 真实 skopeo 导出的 docker-archive 能在客户同版本 Docker 上逐个 load，镜像 tag/数量正确。
- OSS 上传失败和恢复、任务下载、历史包下载、命令行重定向及纯 OSS 保留策略。
- registry 对三个节点的镜像可达性、ConfigMap 生效、app/worker rollout。
- 原 worker 重建后上传重试必须明确失败或重新构建，不能声称 emptyDir 归档可跨重建恢复。

本轮 owner token 协议与旧 worker 不兼容，发布时应先等旧执行任务结束，再切换 worker。
清单和发布脚本使用 Recreate 策略，避免不同协议的执行者混跑同一批消息。

### 6. 其他保留的范围限制

- 任务中心仅展示最近少量完成记录，完整状态仍受 TTL 约束，尚无长期审计库。
- 页面显示的磁盘信息属于 app 所在目录，不代表每个 worker 节点磁盘状况。
- HTTP 下通知受浏览器安全上下文限制；子路径部署未作为完整支持场景验收。
- 不支持 digest 镜像引用；当前交付面向 linux/amd64，tag 固定到 digest 和正式校验清单留待后续。
- 仓库附带的 nerdctl/ossutil 是 Linux 工具，containerd 旧回退仍可能受历史 namespace 残留影响。

## 本轮已修复（代码与回归已加入，真实发布另行验收）

| Review 项目 | 当前处理 |
|---|---|
| R1 单测可能删除 OSS | 普通测试隔离存储并拦截未模拟外部调用，工具测试使用临时替身 |
| R2 部署配置漂移 | config.yaml 为真源，sync_manifest.py 生成/检查完整 ConfigMap，独立 worker 强制 OSS |
| R3 上传失败假成功 | 有限重试后返回交付失败、保留归档；原 worker 在线时支持仅重试上传 |
| R4 同名镜像漏包 | 完整引用唯一命名、整批校验，后端核对 image_exports.tsv |
| R5 回收重复执行 | Lua 原子回收、worker/attempt 执行权、状态与日志写入校验、compare-delete 释放 |
| R6 异常日志撤销超时 | 异常/超时/取消/失去执行权均回收子进程组 |
| R7 本机 import 漏多节点 | 多节点使用 registry；本机 import 发布前验证实时 app/worker 固定节点 |
| R8 完整镜像双前缀 | API/脚本复用 image_refs.py，支持完整地址和端口 |
| R9 curl 保存跳转页 | 命令跟随跳转并检查 HTTP 错误，按钮取消跨域 HEAD 前置检查 |
| R10 连续发布 YAML 旧 tag | 按业务容器名称更新镜像，不再依赖旧镜像前缀正则 |
| R11 纯 OSS 不回收 | 合并本地与远端候选，保留数量/时间/当前包保护，列表失败停止回收 |
| R12 日志游标漏日志 | 序号与日志原子追加，仅确认实际返回的日志 |
| R13 晚阶段取消无效 | 归档和上传使用可取消、检查执行权的子进程执行器 |

同时修复：SSE 失联保留任务并轮询恢复；任务中心请求超时；构建期间锁定版本线；
registry 端口校验；worker 在线阈值与心跳 TTL 对齐；排队不因固定十分钟失败；
归档去除 GNU --transform 依赖。新增统一 `bash bin/check.sh` 和 Linux CI。
自动化回归覆盖正常路径与故障注入，测试数量随用例更新，以检查入口的实际输出为准。

## 历史运维记录

2026-10-28 曾处理 Redis 镜像内容与 tag 不符：节点上标记为 7.2 的镜像实际为 7.0，
无法读取 7.2 写入的 AOF/RDB。更换正确镜像并验证数据副本后恢复，增加 Recreate 和
apk01 节点约束；备份在 apk01 的 `/root/redis-data-backup-*.tar.gz`。

containerd 旧问题的巡检与处理规范继续保留在
[CONTAINERD_RUNBOOK.md](docs/CONTAINERD_RUNBOOK.md)。其他历史修复通过 git log 查询。
