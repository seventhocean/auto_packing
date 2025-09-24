/home/auto_packing_no_delete/
├── version_data/               # 版本数据主目录（新增核心目录）
│   ├── 64-20250825/            # 版本A目录
│   │   ├── patch_image_tag_list.txt  # 版本A的镜像列表
│   │   └── images/              # 版本A的镜像tar包
│   ├── 65-20250826/            # 版本B目录
│   │   ├── patch_image_tag_list.txt
│   │   └── images/
│   └── ...                      # 其他历史版本
├── task_records/               # 任务记录主目录（新增核心目录）
│   ├── task_1756375683_64to65/  # 具体任务目录
│   │   ├── upgrade_64to65.zip   # 增量升级包
│   │   ├── diff_images/         # 差异镜像tar包
│   │   ├── diff_list.txt        # 差异镜像列表
│   │   └── task_info.json       # 任务元数据
│   └── ...                      # 其他任务记录
├── latest_image_list/          # 保留现有最新列表目录（兼容旧逻辑）
├── logs/                       # 统一日志目录
│   ├── app.log                  # 后端应用日志
│   ├── pull_save.log            # 镜像拉取日志
│   ├── oss_processor.log        # OSS同步日志
│   └── version_manager.log      # 版本管理日志
├── myenv/                      # Python虚拟环境
├── app.py                       # 后端接口（已修改）
├── oss_patch_processor.sh       # OSS同步脚本（已修改）
├── pull_save.sh                 # 镜像拉取脚本（已修改）
├── version_manager.sh           # 版本管理脚本（新增）
└── requirements.txt             # 依赖清单


corn -w 1 -b 0.0.0.0:8000 --timeout 60 app:app
