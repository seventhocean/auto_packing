#!/bin/bash
set -e

# 创建必要目录
mkdir -p /app/bin /app/logs /app/.trash /app/latest_image_list

# -----------------------------
# 动态拉取 nuwa 项目
#   NUWA_DIR  : 克隆目标目录，默认 /app/nuwa；
#               生产上挂 hostPath 缓存（每节点一份），Pod 重建就不再重复克隆
#   SKIP_NUWA : 设为 1 时完全跳过（app 容器不需要 nuwa）
# 全量仓库 446MB / 2566 文件，每次 Pod 启动都克隆会把节点磁盘 I/O 打满，
# 所以：浅克隆 + 缓存目录 + 文件锁，三者配合。
# -----------------------------
NUWA_DIR="${NUWA_DIR:-/app/nuwa}"
NUWA_REPO="git@gitlab.yunshan.net:yunshan/deepflow-group/nuwa.git"

if [ "${SKIP_NUWA:-0}" = "1" ]; then
  echo "SKIP_NUWA=1，跳过 nuwa 拉取（当前容器不需要）"
elif [ -d "${NUWA_DIR}/.git" ]; then
  echo "nuwa 缓存已存在：${NUWA_DIR}，跳过克隆"
else
  echo "正在拉取 nuwa 项目（浅克隆）→ ${NUWA_DIR} ..."
  mkdir -p "$(dirname "${NUWA_DIR}")"
  # 同一节点可能有多个 Pod 同时启动，用文件锁串行化，避免并发克隆互相踩踏
  (
    flock -w 900 9 || { echo "等待 nuwa 克隆锁超时"; exit 1; }
    if [ -d "${NUWA_DIR}/.git" ]; then
      echo "nuwa 已由其他进程拉取完成，跳过"
      exit 0
    fi
    rm -rf "${NUWA_DIR}"
    clone_ok=0
    for attempt in 1 2 3; do
      if git clone --depth 1 --single-branch "${NUWA_REPO}" "${NUWA_DIR}"; then
        clone_ok=1
        break
      fi
      echo "nuwa 第 ${attempt}/3 次拉取失败，10 秒后重试..."
      rm -rf "${NUWA_DIR}"
      sleep 10
    done
    if [ "${clone_ok}" -ne 1 ]; then
      echo "nuwa 拉取失败！请检查网络或 SSH 配置。"
      exit 1
    fi
    echo "nuwa 项目拉取成功（浅克隆）"
  ) 9>"$(dirname "${NUWA_DIR}")/.nuwa-clone.lock"
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