# 自动打补丁与部署系统

这是一个基于 Docker 和 Python 的轻量级自动化系统，用于管理、部署和维护补丁镜像。本项目旨在简化升级、拉取、保存和部署补丁镜像的流程，适用于离线或受控环境。

---

## 📁 项目结构
.
├── bin/
│   ├── auto_utilconfig
│   ├── deepflow_patch_upgrade.sh
│   ├── get_patch_image_tag_list.sh
│   └── pull_save.sh
│
├── images/
│   ├── latest_image_list
│   └── patch_image_tag_list.txt
│
├── logs/
│   ├── app.log
│   ├── oss_processor.log
│   └── pull_save.log
│
├── myenv/
│   ├── bin/
│   ├── lib/
│   └── lib64/
│   ├── pip-selfcheck.json
│   └── pyvenv.cfg
│
├── nuwa/
│   └── upgrade_packages/
│
├── webfonts/
│   └── .dockerignore
│
├── static/
│   ├── all.min.css
│   ├── bootstrap.bundle.min.js
│   ├── bootstrap.min.css
│   └── favicon.ico
│
├── app.py
├── app-test.py
├── auto_get_patch_dockerfile
├── get_patch_image_tag_list.sh
├── index.html
├── index-test.html
├── maintenance-app-deployment.yaml
├── oss-credentials-secret.yaml
├── pull_save.sh
└── README.md

---

## 🛠️ 核心组件说明

### 🔧 脚本文件

| 文件 | 用途 |
|------|------|
| `deepflow_patch_upgrade.sh` | 自动执行 DeepFlow 相关补丁镜像的升级流程 |
| `get_patch_image_tag_list.sh` | 从镜像仓库获取可用的补丁镜像标签列表 |
| `pull_save.sh` | 拉取 Docker 镜像并保存为本地 tar 文件（适用于离线环境） |
| `auto_get_patch_dockerfile` | 自动生成或获取用于构建补丁镜像的 Dockerfile |

### 📂 目录说明

- **`bin/`**  
  存放所有可执行脚本，用于自动化任务。

- **`images/`**  
  存放镜像版本信息：
  - `latest_image_list`：最新镜像列表
  - `patch_image_tag_list.txt`：补丁镜像标签清单

- **`logs/`**  
  各模块日志文件：
  - `app.log`：主应用日志
  - `oss_processor.log`：OSS（对象存储）操作日志
  - `pull_save.log`：镜像拉取与保存日志

- **`myenv/`**  
  Python 虚拟环境，包含依赖库和配置（由 `pyvenv.cfg` 定义）。

- **`nuwa/upgrade_packages/`**  
  存放升级所需的软件包或二进制文件（可能用于内部工具链）。

- **`webfonts/`**  
  前端 Web 字体资源（含 `.dockerignore` 忽略规则）。

- **`static/`**  
  前端静态资源，包括 CSS、JS 和图标文件。

---

## 💡 工作流程

1. **获取镜像标签**  
   运行 `get_patch_image_tag_list.sh`，将可用的补丁镜像标签写入 `patch_image_tag_list.txt`。

2. **拉取并保存镜像**  
   使用 `pull_save.sh` 从远程仓库拉取镜像，并保存为本地 tar 文件，便于离线分发。

3. **执行补丁升级**  
   `deepflow_patch_upgrade.sh` 负责协调整个升级过程，可能包括重建容器或替换旧镜像。

4. **Kubernetes 部署**  
   通过 `maintenance-app-deployment.yaml` 在 Kubernetes 中部署维护应用。

5. **OSS 凭据管理**  
   敏感凭据存储在 `oss-credentials-secret.yaml` 中（**切勿提交到公开代码库**）。

---

## ⚙️ 快速开始

### 前提条件
- 已安装 Docker 或者 nerdctl
- Python 3.x 环境
- 若需部署到集群，请确保已配置 kubectl 和 Kubernetes 环境
- 具备镜像仓库访问权限（如 Harbor、AWS ECR 等）

