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

**1. V6 版本选择器用原生 `<select>`，100+ 版本无法搜索**
`index.html:1110` — 需替换为自定义可搜索下拉组件。最大体验瓶颈。

**2. 无升级预览面板**
`index.html` — 选版本后不显示中间跨越版本数、预计镜像数、预计包大小。

**3. 已有包无智能匹配**
`index.html:1202` — 已选版本对与已有包匹配时不自动提示。

**4. 无取消构建按钮**
前端 + worker — 任务提交后无法中止（现在有 subprocess timeout 兜底，但无显式取消机制）。

### 🟡 中优先级

**5. `pull_save.sh:83` 硬编码凭据**
内网环境，暂不修复。后续应恢复为环境变量。

**6. `debug=True` 在生产代码中**
`app.py:954` — debug 模式启动 reloader 和 werkzeug debugger。

**7. 无 OSS 版本缓存**
`app.py:241` — 每次构建都执行 `ossutil ls` 网络请求。应加 TTL 缓存。

**8. SSE 每次推送完整日志列表**
`app.py:131` — 应推送增量日志而非全量。前端已有去重逻辑作为补偿。

**9. `retry_on_timeout=True` 可能导致 INCR 重复**
`app.py:91` — 非幂等 Redis 操作超时重试会重复计数。

### 🟢 低优先级

**10. `reclaim_stale_tasks` 逆序重入队**
`worker.py:48` — `reversed()` + `rpush` 改变任务优先级。

**11. 无 `/health` 端点**
K8s 探针用 `GET /` 渲染完整 HTML。

**12. `upgrade_packages/` 无自动清理**
历史包只增不减。

**13. PVC 名拼写错误**: `auto-pancking-pvc`

**14. Dockerfile 混用 Debian 版本仓库**
`python:3.6-slim` 用 bullseye 仓库。

**15. `ossutil`/`nerdctl` 二进制随仓库分发**

**16. `deepflow_patch_upgrade.sh` 组件列表硬编码** `:21-49`

**17. `apply/` vs `Apply/` 目录重复**
