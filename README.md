BASE_DIR/                  # 服务根目录（启动脚本所在路径）
├── app.py                 # 服务主程序（Flask路由、核心逻辑）
├── index.html             # 前端页面（用户交互入口，通过Flask的static服务提供）
├── pull_save.sh           # 镜像拉取脚本（核心脚本1：读取镜像列表并拉取/保存镜像）
├── get_patch_image_tag_list.sh  # 版本对比脚本（核心脚本2：生成镜像列表文件）
├── image_tar/             # 镜像/升级包存储目录（核心数据目录）
│   ├── *.tar              # 拉取的镜像文件（如xxx.tar，由pull_save.sh生成）
│   └── upgrade_*.zip      # 最终生成的升级包（含镜像+列表文件，由app.py打包）
├── latest_image_list/     # 镜像列表存储目录（中间文件目录）
│   └── patch_image_tag_list.txt  # 镜像列表文件（由get_patch_image_tag_list.sh生成）
├── logs/                  # 日志目录
│   └── app.log            # 服务日志（含时间戳、级别、内容，由write_log函数写入）
└── ossutil                # OSS工具（隐含依赖，需提前安装，用于获取OSS版本列表）


BASE_DIR/                  # 服务根目录
├── app.py                 # 服务主程序
├── index.html             # 前端页面
├── bootstrap.min.css      # CSS
├── pull_save.sh           # 镜像拉取脚本
├── get_patch_image_tag_list.sh  # 版本对比脚本
├── images/             # 镜像根目录
│   └── {current}_to_{target}_task_12345/  # 任务专属镜像目录（临时）
│       └── *.tar          # 当前任务拉取的镜像文件
├── upgrade_packages/      # 升级包存储目录（永久）
│   └── {current}_to_{target}.zip  # 最终升级包
├── latest_image_list/     # 镜像列表目录
│   └── patch_image_tag_list.txt  # 镜像列表文件
└── logs/                  # 日志目录
    └── app.log            # 服务日志


