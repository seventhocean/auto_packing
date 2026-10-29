#!/usr/bin/env bash
# 构建并发布 auto_packing 镜像。
#
# 每次发布使用唯一 tag，避免 K8s 的 imagePullPolicy: IfNotPresent
# 复用节点上的 :latest 缓存，导致“明明构建了却没更新”的问题。
#
# 用法：
#   REGISTRY=registry.example.com ./bin/redeploy.sh   # 构建、推送并更新
#   NERDCTL_IMPORT=1 ./bin/redeploy.sh         # 仅允许 app/worker 都固定在本机的单节点部署
#
# 可选环境变量：
#   IMAGE_REPO      镜像仓库/名称，默认 auto_packing
#   IMAGE_TAG       镜像 tag，默认 时间戳-git短hash
#   REGISTRY        镜像仓库地址（为空则不推送）
#   NERDCTL_IMPORT  设为 1 时用 nerdctl 导入本机 containerd
#   IMPORT_NODE     本机 Kubernetes node 名称，默认 hostname；仅单节点导入校验使用
#   PYTHON          Python 3 路径，需要安装 requirements.txt 中的 PyYAML
#   SYNC_CONFIG     设为 0 跳过 ConfigMap 同步（默认 1）
#   UPDATE_MANIFEST 设为 0 跳过 YAML 镜像 tag 同步（默认 1）
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
PYTHON="${PYTHON:-python3}"

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
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "错误：未找到 Python 3，无法同步配置和部署清单" >&2
    exit 1
fi

# 未推送仓库时，只能在所有业务 Pod 都固定到导入节点的情况下发布。
# 检查实时 Deployment，避免仓库清单与实际调度不一致时漏分发镜像。
if [ -z "$REGISTRY" ]; then
    if [ "$NERDCTL_IMPORT" != "1" ]; then
        echo "错误：多节点发布必须设置 REGISTRY；单节点发布需启用 NERDCTL_IMPORT=1" >&2
        exit 1
    fi
    IMPORT_NODE="${IMPORT_NODE:-$(hostname)}"
    if ! "$KUBECTL" -n "$NAMESPACE" get deployment/maintenance-app deployment/maintenance-worker -o json | \
        "$PYTHON" -c '
import json, sys
data = json.load(sys.stdin)
items = data.get("items", [data])
expected = {"maintenance-app", "maintenance-worker"}
seen = set()
for item in items:
    name = item.get("metadata", {}).get("name")
    if name not in expected:
        continue
    seen.add(name)
    selected = item["spec"]["template"]["spec"].get("nodeSelector", {}).get("kubernetes.io/hostname")
    if selected != sys.argv[1]:
        sys.exit("拒绝本机导入：app/worker 必须都固定到导入节点；多节点请使用 REGISTRY")
if seen != expected:
    sys.exit("无法确认 app/worker 调度位置，拒绝本机导入")
' "$IMPORT_NODE"; then
        exit 1
    fi
fi

"$PYTHON" "${BASE_DIR}/bin/sync_manifest.py"

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

if [ "${SYNC_CONFIG:-1}" = "1" ] && [ -f "${BASE_DIR}/config.yaml" ]; then
    echo "==> 同步 ConfigMap：maintenance-app-config"
    "$KUBECTL" -n "$NAMESPACE" create configmap maintenance-app-config \
        --from-file=config.yaml="${BASE_DIR}/config.yaml" \
        --dry-run=client -o yaml | "$KUBECTL" apply -f -
fi

if [ "${UPDATE_MANIFEST:-1}" = "1" ]; then
    echo "==> 更新 apply/maintenance-app-deployment.yaml 中的镜像 tag"
    "$PYTHON" "${BASE_DIR}/bin/sync_manifest.py" --image "$DEPLOY_IMAGE"
    echo "提示：YAML 已更新为 ${DEPLOY_IMAGE}，请记得提交并推送。"
fi

# set image 不会应用仓库里的 strategy；显式切换，确保执行协议升级不混跑。
"$KUBECTL" -n "$NAMESPACE" patch deployment/maintenance-worker --type merge \
    -p '{"spec":{"strategy":{"type":"Recreate","rollingUpdate":null}}}'

echo "==> 更新 Deployment 到 ${DEPLOY_IMAGE}"
"$KUBECTL" -n "$NAMESPACE" set image deployment/maintenance-app maintenance-app="$DEPLOY_IMAGE"
"$KUBECTL" -n "$NAMESPACE" set image deployment/maintenance-worker maintenance-worker="$DEPLOY_IMAGE"

echo "==> 等待滚动更新完成"
"$KUBECTL" -n "$NAMESPACE" rollout status deployment/maintenance-app --timeout=300s
"$KUBECTL" -n "$NAMESPACE" rollout status deployment/maintenance-worker --timeout=300s

echo "==> 完成：${DEPLOY_IMAGE}"
