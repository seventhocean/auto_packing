# 问题清单 — deploy 分支

> 审查日期: 2026-06-02 | 总计: 54 个问题 | ✅ 已修复: 27 | 剩余: 27

---

## 已修复 ✅

| # | 类别 | 问题 | 文件 |
|---|------|------|------|
| 1 | P0 | `rstrip('_x86_64')` 字符集剥离 | app.py:268 |
| 2 | P0 | subprocess 无 timeout（5处） | app.py |
| 3 | P0 | SSE generator 无异常处理 | app.py:858 |
| 4 | P0 | 前端 SSE 断连不重试 | index.html:1602 |
| 5 | P0 | 前端 fetchVersions 竞态 | index.html:1314 |
| 6 | P0 | 前端构建失败后 UI 不恢复 | index.html:1584 |
| 7 | P0 | `builds:active` 计数泄漏 | app.py:188 + worker.py:35 |
| 8 | P0 | `process_task` 无限等待 | worker.py:65 |
| 9 | P1 | `update_progress` 100 次 Redis 写 | app.py:231 |
| 10 | P1 | `schedule_task_cleanup` 全量读写 | app.py:184 |
| 11 | P1 | `get_existing_packages` 文件删除竞态 | app.py:317 |
| 12 | P1 | V7 GET URL 超长 | app.py + index.html |
| 13 | P1 | git pull 失败静默继续 | get_patch_image_tag_list.sh:81 |
| 14 | P1 | shebang `/bin/env` 错误 | get_patch_image_tag_list.sh:1 |
| 15 | P1 | 硬编码 v6.6 | get_patch_image_tag_list.sh:109 |
| 16 | P1 | `grep -P` PCRE 依赖 | pull_save.sh:145 |
| 17 | P1 | help 默认容器错误 | pull_save.sh:51 |
| 18 | P1 | rollback `ls -t` pipefail 崩溃 | deepflow_patch_upgrade.sh:237 |
| 19 | P1 | rollback 部分状态丢失 | deepflow_patch_upgrade.sh:254 |
| 20 | P1 | `sed` 中 `.` 未转义 | deepflow_patch_upgrade.sh:147 |
| 21 | P1 | import_images 无 tar 静默成功 | deepflow_patch_upgrade.sh:92 |
| 22 | P1 | 镜像名纯净化冲突 | pull_save.sh:104 |
| 23 | P1 | `sanitizeLogMessage` 误删内容 | index.html:1722 |
| 24 | P1 | `user-scalable=no` 阻止缩放 | index.html:6 |
| 25 | P1 | 标题层级跳跃 h1→h3 | index.html |
| 26 | P2 | 搜索无 debounce | index.html:1456 |
| 27 | P2 | SSE 连接重试 + beforeunload 警告 | index.html |
| 28 | P2 | `prefers-reduced-motion` 缺失 | index.html |
| 29 | P2 | `cursor: not-allowed` 缺失 | index.html:534 |
| 30 | P2 | form-control `transform` 死代码 | index.html:461 |
| 31 | P2 | `sleep 2` ×6 无意义延迟 | deepflow_patch_upgrade.sh |
| 32 | P2 | 教程按钮/搜索框 aria-label 缺失 | index.html |
| 33 | P2 | V6↔V7 切换版本数据缓存 | index.html:1341 |

---

## 仍未修复

### 🔴 高优先级（影响日常使用）

**1. ~~V6 版本选择器用原生 `<select>`，100+ 版本无法搜索~~**
已修复：替换为自定义可搜索下拉（支持关键字过滤、键盘上下键/回车选择、点击外部关闭）；目标版本按 `seq_num` 过滤。

**2. 升级预览不完整**
已部分修复：选择两个版本后显示“将跨越 N 个 patch”；镜像数量与预计包大小仍需后端统计，暂未实现。

**3. ~~已有包无智能匹配~~**
已修复：选择版本对后，如果 `upgrade_packages/` 中存在同名包，会在表单下方提示并给出下载链接。

**4. ~~无取消构建按钮~~**
已修复：新增 `POST /build/<task_id>/cancel`；worker 每秒检查取消标记并终止整个进程组，前端提供取消按钮与“已取消”状态。

### 🟡 中优先级

**5. `pull_save.sh:83` 硬编码凭据**
内网环境，暂不修复。后续应恢复为环境变量。

**6. ~~`debug=True` 在生产代码中~~**
已修复：`app.py` 仅在 `FLASK_DEBUG=1` 时启用 debug。

**7. 无 OSS 版本缓存**
`get_oss_versions()` — 每次构建/页面加载都执行 `ossutil ls` 网络请求。可考虑短 TTL 缓存。

**8. ~~SSE 每次推送完整日志列表~~**
已修复：日志条目带单调递增 `seq`，SSE/`/task-status` 支持 `?since=<seq>` 只返回增量日志。

**9. ~~`retry_on_timeout=True` 可能导致 INCR 重复~~**
已修复：并发准入改为 `SET NX` slot，不再依赖非幂等的 `INCR builds:active`。

### 🟢 低优先级

**10. `reclaim_stale_tasks` 逆序重入队**
`worker.py:48` — `reversed()` + `rpush` 改变任务优先级。

**11. ~~无 `/health` 端点~~**
已修复：新增 `GET /health`，K8s 探针已切换。

**12. `upgrade_packages/` 无自动清理**
历史包只增不减。

**13. PVC 名拼写错误**: `auto-pancking-pvc`

**14. Dockerfile 混用 Debian 版本仓库**
`python:3.6-slim` 用 bullseye 仓库。

**15. `ossutil`/`nerdctl` 二进制随仓库分发**

**16. `deepflow_patch_upgrade.sh` 组件列表硬编码** `:21-49`

**17. `apply/` vs `Apply/` 目录重复**

---

## 2026-10-20 dev 分支修复批次

| # | 类别 | 问题 | 处理 |
|---|------|------|------|
| 1 | P0 | `_run_and_stream` 中 `Popen` 无超时，拉镜像卡死会永久占用槽位 | 增加看门狗定时器，超时 `kill` 并抛出 `TimeoutError`（默认 2h，可配） |
| 2 | P0 | V6 版本参数未校验，可借 `../` 影响任务目录/包名 | 新增 `_is_safe_version_value`，非法版本返回 400 |
| 3 | P0 | 升级包直接写入最终路径，并发/下载可能读到半成品 | 改为临时文件 + `os.replace` 原子替换 |
| 4 | P1 | 并发计数依赖 `INCR/DECR`，异常退出会漂移 | 改为以 slot key 为锁与准入依据，slot 带 TTL；`builds:active` 仅作观测 |
| 5 | P1 | `ossutil ls` 无超时；版本缓存非原子写入 | 增加 120s 超时；缓存改为临时文件 + 原子替换 |
| 6 | P1 | worker 遇到无法解析的队列消息会直接崩溃 | 捕获解析异常，跳过消息并记录告警 |
| 7 | P1 | V6/V7 入队失败时 `raise` 返回 HTML 500 | 统一返回 JSON 500 |
| 8 | P2 | `debug=True` 硬编码 | 改为 `FLASK_DEBUG=1` 控制 |
| 9 | P2 | 无 `/health`，K8s 探针渲染整页 | 新增 `/health` 并更新探针路径 |
| 10 | P2 | 前端日志/包名直接拼 `innerHTML`，存在注入风险 | 日志改用 `textContent`；包名增加转义 |
| 11 | P2 | `newPackageName` 永不清空，NEW 标记一直存在 | 渲染后清空 |
| 12 | P2 | shell 参数错误时返回 0；nuwa 缺失时报错不清晰 | 修正退出码并增加 nuwa 目录检查 |
| 13 | P1 | 心跳/回收线程遇 Redis 异常会静默退出，导致任务被误回收 | 线程循环内捕获异常并记录告警 |
| 14 | P2 | worker 主循环 Redis 异常会直接退出 | 捕获后 sleep 5s 重试 |
| 15 | P1 | SSE/状态接口每秒推送完整日志列表 | 日志增加 `seq` 游标，支持 `?since=<seq>` 增量返回，前端按游标重连 |
| 16 | P1 | V6 版本选择器为原生 select，100+ 版本不可搜索 | 替换为可搜索下拉组件，支持过滤/键盘操作，目标版本按 `seq_num` 过滤 |
| 17 | P1 | 任务提交后无法中止（拉取最长 2h） | 新增取消接口与前端按钮；worker/SSE 监听取消标记并终止进程组 |
| 18 | P1 | 构建失败时前端 `handleBuildStatus` 引用未定义的 `bl`，错误处理直接抛异常 | 修正为直接操作日志面板并同步展开状态 |
| 19 | P2 | 已选版本对与已有升级包无关联提示 | 表单下方提示跨越 patch 数；命中同名包时给出直接下载链接 |
