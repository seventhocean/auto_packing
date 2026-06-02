# 前端审查报告 — deploy 分支

> 文件: `templates/index.html` | 审查日期: 2026-06-02

## 当前状态

deploy 分支前端为单页工作台 (~1900行)，三区布局：

```
.page-shell
├── .hero (grid: 1.55fr 0.65fr)
│   ├── .hero-card — 标题 + 描述
│   └── .status-card — 系统概览(模式, 已有包数)
└── .layout (flex)
    ├── main.workspace (flex: 1)
    │   ├── .mode-toolbar — V6/V7 下拉切换 + 模式说明
    │   └── .panel-grid
    │       ├── .form-shell — V6/V7 构建表单
    │       └── .task-shell — 进度条 + 日志 + 下载
    └── aside.resource-panel — 已有升级包列表
```

## 已修复 (本轮)

- ✅ SSE 断连指数退避重连 (3次: 2s/4s/8s)
- ✅ fetchVersions V6/V7 切换竞态 (`currentSeries` 守卫)
- ✅ 构建失败后 resetBuildState 恢复 idle 面板
- ✅ `user-scalable=no` 移除
- ✅ 标题层级 h1→h2→h5 修复
- ✅ `sanitizeLogMessage` 只移除末尾 `(XX%)` 标记
- ✅ 搜索 debounce (200ms)
- ✅ `prefers-reduced-motion` 媒体查询
- ✅ `cursor: not-allowed` 在 disabled 按钮
- ✅ form-control 移除死代码 `transform` 过渡
- ✅ 教程按钮/搜索框/V7 textarea 补 aria 属性
- ✅ beforeunload 构建中拦截关闭
- ✅ V7 改 POST 提交 (避免 URL 超长)
- ✅ V6↔V7 切换时版本数据缓存

## 仍存在的问题

### 功能 (3 个)

1. **版本选择器用原生 `<select>`** — 100+ 版本无法搜索，最大体验瓶颈
2. **无升级预览** — 选版本后不显示中间版本数/镜像数/预计大小
3. **已有包无智能匹配** — 不自动提示"该路径已有现成包"

### 性能 (1 个)

4. **日志渲染直接操作 DOM** — 每次 SSE 推送 `innerHTML +=`，无虚拟滚动

### 代码质量 (3 个)

5. **全局变量散落**: `versionData`, `eventSource`, `currentSeries`, `isBuilding`, `lastLogMessage`, `existingFilesData`
6. **JS/CSS/HTML 混在单文件 ~1900 行** — 无模块化
7. **首次加载渲染整页 HTML** — K8s 探针用 `GET /` 做健康检查
