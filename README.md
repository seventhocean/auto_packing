# auto_packing

轻量级的 DeepFlow 补丁包构建与分发工具。它通过 Flask 提供 Web/REST 界面，编排本地 Shell 脚本自动完成版本差异计算、镜像拉取、打包生成升级包，并支持已生成包的列举与下载。

## 架构速览
- 后端：`app.py`（Flask 1.1）提供页面、REST API 与 SSE；并发任务用线程 + `Semaphore(3)` 控制。
- 前端：`templates/index.html` 单页，使用 bootstrap，SSE 实时显示进度/日志。
- 核心脚本：`bin/get_patch_image_tag_list.sh` 生成镜像差异清单；`bin/pull_save.sh` 拉取并保存镜像；`bin/deepflow_patch_upgrade.sh` 部署端执行升级。
- 产物：`upgrade_packages/` 下的 `deepflow_patch_v669_<from>_<to>.tar.gz`，包含镜像 tar、镜像清单、升级脚本。
- 外部依赖：阿里 OSS (ossutil) 拉取版本列表；DeepFlow 镜像仓库 `hub.deepflow.yunshan.net/dev`；可选 Kubernetes 部署 yaml 在 `Apply/`。

## 目录结构
- `app.py`：后端主逻辑与 API。
- `templates/index.html`：前端界面与交互脚本。
- `static/`：前端静态资源。
- `bin/`：自动化脚本 (`get_patch_image_tag_list.sh`、`pull_save.sh`、`deepflow_patch_upgrade.sh`、`ossutil`、`nerdctl`)。
- `latest_image_list/`：生成的 `patch_image_tag_list.txt`。
- `images/`：按任务临时存放镜像 tar。
- `upgrade_packages/`：生成的升级包输出目录。
- `logs/`：`app.log` 及任务日志。
- `nuwa/`：DeepFlow 配置/源码仓库本地副本（`get_patch_image_tag_list.sh` 需要）。
- `entrypoint.sh`：容器入口，负责拉取 nuwa、生成 ossutil 配置。
- `auto_get_patch_dockerfile`：构建镜像的 Dockerfile。
- `Apply/maintenance-app-deployment.yaml`、`persistent-volume.yaml`：K8s 部署样例。

## 运行要求
- Python 3.6+，Flask 1.1，依赖见 `requirements.txt`。
- 容器工具：`nerdctl`（默认）或 `docker`，二进制已随项目提供。
- Git 访问 `nuwa` 仓库；镜像仓库访问凭据；可选 OSS 访问凭据（环境变量 `OSS_ACCESS_KEY_ID/SECRET/ENDPOINT`）。
- 足够磁盘空间存放镜像 tar 与打包产物。

## 本地启动
```bash
cd auto_packing
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python app.py  # 默认 0.0.0.0:8000
```
运行后浏览器访问 `http://<host>:8000/`。

## Docker 方式
项目包含 `auto_get_patch_dockerfile`（Dockerfile）与 `entrypoint.sh`：
```bash
docker build -f auto_get_patch_dockerfile -t auto-packing .
docker run -p 8000:8000 \
  -e OSS_ACCESS_KEY_ID=... -e OSS_ACCESS_KEY_SECRET=... -e OSS_ENDPOINT=... \
  auto-packing
```

## 关键流程
1) 前端选择当前/目标补丁版本 → GET `/build?current=...&target=...` 启动任务。  
2) 后端线程执行：
   - 运行 `bin/get_patch_image_tag_list.sh current target` 生成 `latest_image_list/patch_image_tag_list.txt`（基于 `nuwa` 版本差异与 OSS 列表）。
   - 运行 `bin/pull_save.sh -d images/<task>` 拉取镜像并保存为 tar。
   - 将镜像 tar + 清单 + `bin/deepflow_patch_upgrade.sh` 打包为 `upgrade_packages/deepflow_patch_v669_<from>_<to>.tar.gz`。
3) SSE 将进度/日志推送给前端；完成后可通过 `/download/<task_id>` 下载。
4) 历史包可通过 `/existing-packages` 列举，`/download-existing/<filename>` 直接下载。

## API 速查
- `GET /`：前端页面。
- `GET /versions`：从 OSS 拉取补丁版本列表（含序号、日期）。
- `GET /build?current=<v>&target=<v>`：启动构建任务（SSE）。
- `GET /task-status/<task_id>`：查询任务状态。
- `GET /download/<task_id>`：下载刚构建的包。
- `GET /existing-packages`：列出已生成包。
- `GET /download-existing/<filename>`：下载指定历史包。

## 部署要点
- 若在 K8s 部署，可参考 `Apply/maintenance-app-deployment.yaml` 与 `persistent-volume.yaml`，并挂载可写目录到 `/app/images`, `/app/upgrade_packages`, `/app/logs`.
- 设置环境变量以生成 OSS 配置，或预置 `/app/bin/.ossutilconfig`。
- `nuwa` 目录需存在且可 `git pull`，否则 `get_patch_image_tag_list.sh` 会失败。

## 常见问题
- 未生成 `patch_image_tag_list.txt`：检查 OSS 凭据、网络或 `nuwa` 仓库是否最新。
- 拉取镜像失败：确认仓库凭据、`nerdctl`/`docker` 可用；磁盘剩余空间。
- 下载 404：任务状态可能已过期（默认 90 秒清理）；重新构建或使用 `/existing-packages` 下载历史包。

## 开发提示
- 前端逻辑集中在 `templates/index.html`，使用原生 JS + SSE，无打包工具。
- 后端日志写入 `logs/app.log`，任务日志 `logs/task_<id>.log` 会在任务结束后清理。
- 并发上限由 `build_semaphore = Semaphore(3)` 控制。
- 若调整镜像仓库或登录方式，修改 `bin/pull_save.sh` 中 `repo_login` / `repo`。

## 下一步可考虑
- 将配置（仓库地址、并发数、清理保留时长）抽取到 env / config 文件。
- 为 API 与脚本补充单元/集成测试（`test/` 目前仅示例）。
- 增加鉴权/审计与下载链接失效策略。

