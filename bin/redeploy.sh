#!/usr/bin/env bash
# 构建并发布 auto_packing 镜像。
#
# 每次发布使用唯一 tag，避免 K8s 的 imagePullPolicy: IfNotPresent
# 复用节点上的 :latest 缓存，导致“明明构建了却没更新”的问题。
#
# 用法：
#   ./bin/redeploy.sh                          # 构建并更新（镜像需已存在于节点）
#   REGISTRY=registry.example.com ./bin/redeploy.sh   # 构建、推送并更新
#   NERDCTL_IMPORT=1 ./bin/redeploy.sh         # 构建后导入本机 k8s.io containerd
#
# 可选环境变量：
#   IMAGE_REPO      镜像仓库/名称，默认 auto_packing
#   IMAGE_TAG       镜像 tag，默认 时间戳-git短hash
#   REGISTRY        镜像仓库地址（为空则不推送）
#   NERDCTL_IMPORT  设为 1 时用 nerdctl 导入本机 containerd
#   NAMESPACE       K8s 命名空间，默认 getpatch
#   KUBECTL         kubectl 路径，默认 kubectl
set -euo pipefail

BASE_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$BASE_DIR"

IMAGE_REPO="${IMAGE_REPO:-auto_packing}"
NAMESPACE="${NAMESPACE:-getpatch}"
KUBECTL="${KUBECTL:-kubectl}"
REGISTRY="${REGISTRY:-}"
NERDCTL_IMPORT="${NERDCTL_IMPORT:-0}"
NERDCTL="${NERDCTL:-${BASE_DIR}/bin/nerdctl}"

if [ -z "${IMAGE_TAG:-}" ]; then
    IMAGE_TAG="$(date +%Y%m%d-%H%M%S)-$(git rev-parse --short HEAD 2>/dev/null || echo local)"
fi

LOCAL_IMAGE="${IMAGE_REPO}:${IMAGE_TAG}"
DEPLOY_IMAGE="$LOCAL_IMAGE"

if ! command -v docker >/dev/null 2>&1; then
    echo "错误：未找到 docker，无法构建镜像" >&2
    exit 1
fi
if ! command -v "$KUBECTL" >/dev/null 2>&1; then
    echo "错误：未找到 $KUBECTL，无法更新 Deployment" >&2
    exit 1
fi

echo "==> 构建镜像：${LOCAL_IMAGE}"
docker build -f dockerfile -t "$LOCAL_IMAGE" .

if [ -n "$REGISTRY" ]; then
    DEPLOY_IMAGE="${REGISTRY%/}/${IMAGE_REPO}:${IMAGE_TAG}"
    echo "==> 推送镜像：${DEPLOY_IMAGE}"
    docker tag "$LOCAL_IMAGE" "$DEPLOY_IMAGE"
    docker push "$DEPLOY_IMAGE"
fi

if [ "$NERDCTL_IMPORT" = "1" ]; then
    if [ ! -x "$NERDCTL" ]; then
        echo "错误：未找到可执行的 nerdctl：${NERDCTL}" >&2
        exit 1
    fi
    TMP_TAR="$(mktemp "/tmp/auto_packing_image.XXXXXX.tar")"
    trap 'rm -f "$TMP_TAR"' EXIT
    echo "==> 导出并导入本机 containerd：${LOCAL_IMAGE}"
    docker save "$LOCAL_IMAGE" -o "$TMP_TAR"
    "$NERDCTL" -n k8s.io load -i "$TMP_TAR"
fi

if [ -z "$REGISTRY" ] && [ "$NERDCTL_IMPORT" != "1" ]; then
    echo "提示：未配置 REGISTRY 且未启用 NERDCTL_IMPORT，"
    echo "      请确认 ${LOCAL_IMAGE} 已经存在于集群节点上。"
fi

echo "==> 更新 Deployment 到 ${DEPLOY_IMAGE}"
"$KUBECTL" -n "$NAMESPACE" set image deployment/maintenance-app maintenance-app="$DEPLOY_IMAGE"
"$KUBECTL" -n "$NAMESPACE" set image deployment/maintenance-worker maintenance-worker="$DEPLOY_IMAGE"

echo "==> 等待滚动更新完成"
"$KUBECTL" -n "$NAMESPACE" rollout status deployment/maintenance-app --timeout=300s
"$KUBECTL" -n "$NAMESPACE" rollout status deployment/maintenance-worker --timeout=300s

echo "==> 完成：${DEPLOY_IMAGE}"
