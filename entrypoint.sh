#!/bin/bash
set -e

# 创建必要目录
mkdir -p /app/bin /app/logs /app/.trash /app/latest_image_list

# -----------------------------
# 动态拉取 nuwa 项目（如果未存在）
# -----------------------------
if [ ! -d "/app/nuwa" ]; then
  echo "正在拉取 nuwa 项目..."
  
  # 使用 SSH 克隆（需配置密钥），网络抖动时重试 3 次
  clone_ok=0
  for attempt in 1 2 3; do
    if git clone git@gitlab.yunshan.net:yunshan/deepflow-group/nuwa.git /app/nuwa; then
      clone_ok=1
      break
    fi
    echo "nuwa 第 ${attempt}/3 次拉取失败，10 秒后重试..."
    sleep 10
  done
  if [ "$clone_ok" -ne 1 ]; then
    echo "nuwa 拉取失败！请检查网络或 SSH 配置。"
    exit 1
  fi
  echo "nuwa 项目拉取成功"
fi

# -----------------------------
# 生成 OSS 配置（如提供凭据）
# -----------------------------
if [ -n "$OSS_ACCESS_KEY_ID" ] && [ -n "$OSS_ACCESS_KEY_SECRET" ] && [ -n "$OSS_ENDPOINT" ]; then
  mkdir -p /app/bin
  cat > /app/bin/.ossutilconfig <<EOF
[default]
language=CH
accessKeyId=$OSS_ACCESS_KEY_ID
accessKeySecret=$OSS_ACCESS_KEY_SECRET
endpoint=$OSS_ENDPOINT
region=${OSS_REGION:-cn-beijing}
bucketUrlStyle=Path
retryTimes=10
EOF
  chmod 600 /app/bin/.ossutilconfig
  echo "OSS config generated at /app/bin/.ossutilconfig"
fi

# -----------------------------
# 执行主命令
# -----------------------------
exec "$@"