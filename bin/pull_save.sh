#!/bin/bash
# 启用严格错误检查（避免隐藏错误）
set -euo pipefail

# ------------------------------ 基础配置（与项目对齐） ------------------------------
# 颜色变量（日志美化）
GREEN='\033[1;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
WHITE='\033[1;37m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'        # 恢复默认

# 项目基础目录（与app.py保持一致）
#BASE_DIR="/home/auto_packing_no_delete"
BASE_DIR=$(cd "$(dirname "$0")" && pwd)
# 默认镜像列表文件（由oss脚本生成）
DEFAULT_IMAGE_LIST="$BASE_DIR/../latest_image_list/patch_image_tag_list.txt"
# 日志文件（统一存储到项目logs目录）
LOG_FILE="$BASE_DIR/../logs/app.log"

# 容器工具（默认nerdctl，支持通过--cmd切换为docker）
# 用数组保存，避免 "nerdctl -n k8s.io" 被当成一个可执行文件名
#container_cmd=(docker)
container_cmd=(nerdctl -n k8s.io)

# 镜像获取方式：
#   archive = 老流程，容器工具 pull + save（依赖节点 containerd.sock）
#   skopeo  = 直接用 skopeo 导出 docker-archive，不依赖节点 containerd
copy_tool="${COPY_TOOL:-archive}"
# 仓库凭据（repo_login 与 skopeo 共用）
REGISTRY_HOST="hub.deepflow.yunshan.net"
REGISTRY_USER="acrpush@yunshan"
REGISTRY_PASSWORD="35lRrgBcLhF"
# skopeo 对内网仓库的 TLS 校验，遇到自签证书可设 SKOPEO_TLS_VERIFY=false
skopeo_tls_verify="${SKOPEO_TLS_VERIFY:-true}"
# 镜像仓库前缀（默认DeepFlow仓库）
repo="hub.deepflow.yunshan.net/dev/"
# 镜像保存目录（默认空，需通过--dir指定）
save_dir=""


# ------------------------------ 工具函数 ------------------------------
##日志函数（带时间戳，同时输出到文件和控制台）
log() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    local log_content="[$timestamp] $1"
    # 写入日志文件
    echo -e "$log_content" >> "$LOG_FILE"
    # 打印到控制台
    echo -e "$log_content"
}

##显示帮助文档
show_help() {
    echo -e "\n${WHITE}=== DeepFlow 镜像拉取脚本 ===${NC}"
    echo -e "${WHITE}使用说明:${NC}"
    echo -e "  ${CYAN}$0 [选项] [镜像名称1 镜像名称2 ...]${NC}\n"
    
    echo -e "${WHITE}选项说明:${NC}"
    echo -e "  ${GREEN}-h, --help     ${NC}显示此帮助信息"
    echo -e "  ${GREEN}-c, --cmd      ${NC}指定容器工具（${BOLD}docker${NC}/${BOLD}nerdctl${NC}，默认nerdctl）"
    echo -e "  ${GREEN}-d, --dir      ${NC}指定镜像保存目录（必填，例如：$BASE_DIR/image_tar）"
    echo -e "  ${GREEN}-f, --file     ${NC}指定镜像列表文件（默认：$DEFAULT_IMAGE_LIST）\n"
    
    echo -e "${WHITE}使用示例:${NC}"
    echo -e "  ${BOLD}1. 自动化流程调用（从默认列表拉取到image_tar）:${NC}"
    echo -e "     ${CYAN}$0 -d $BASE_DIR/../image_tar${NC}\n"
    
    echo -e "  ${BOLD}2. 手动指定列表文件拉取:${NC}"
    echo -e "     ${CYAN}$0 -d ./my_dir -f ./my_image_list.txt${NC}\n"
    
    echo -e "  ${BOLD}3. 手动拉取单个镜像（自动添加仓库前缀）:${NC}"
    echo -e "     ${CYAN}$0 -d ./my_dir deepflow-server:v6.6.5550${NC}\n"
    
    echo -e "  ${BOLD}4. 使用nerdctl拉取多个镜像:${NC}"
    echo -e "     ${CYAN}$0 -c nerdctl -d ./my_dir deepflow-server:v6.6.5550 pcap:v6.6.220${NC}\n"
    
    echo -e "  ${BOLD}5. 拉取完整镜像地址（不自动添加前缀）:${NC}"
    echo -e "     ${CYAN}$0 -d ./my_dir hub.deepflow.yunshan.net/dev/deepflow-server:feature-scp-66${NC}\n"
}

##镜像仓库登录（使用预设账号密码）
repo_login() {
    log "${YELLOW}开始登录镜像仓库：hub.deepflow.yunshan.net${NC}"
    # 检查容器工具是否存在（只取命令名）
    local _cmd_bin="${container_cmd[0]}"
    if ! command -v "$_cmd_bin" &> /dev/null; then
        log "${RED}错误：容器工具 $_cmd_bin 未安装或未配置到环境变量${NC}"
        exit 1
    fi

    # 执行登录（密码通过管道传递，避免明文暴露），网络抖动时重试 3 次
    local login_ok=0
    for attempt in 1 2 3; do
        if echo "${REGISTRY_PASSWORD}" | "${container_cmd[@]}" login --username="${REGISTRY_USER}" --password-stdin "${REGISTRY_HOST}"; then
            login_ok=1
            break
        fi
        log "${YELLOW}仓库登录失败（第 ${attempt}/3 次），3 秒后重试${NC}"
        sleep 3
    done
    if [ "$login_ok" -ne 1 ]; then
        log "${RED}错误：仓库登录失败！请检查账号密码或网络连接${NC}"
        exit 1
    fi
    log "${GREEN}仓库登录成功${NC}"
}

##由完整镜像名推导 tar 文件名（deepflow-server:v6.6.5550 -> deepflow-server_v6.6.5550.tar）
image_tar_name() {
    local full_image_name="$1"
    local image_name=$(echo "$full_image_name" | awk -F':' '{print $1}' | awk -F'/' '{print $NF}' | sed 's/[^a-zA-Z0-9_-]//g')
    local image_tag=$(echo "$full_image_name" | awk -F':' '{print $2}' | sed 's/[^a-zA-Z0-9._-]//g')
    echo "${image_name}_${image_tag}.tar"
}

##用 skopeo 直接导出 docker-archive（不经过节点 containerd，产物可直接 docker load）
skopeo_copy_single() {
    local full_image_name="$1"
    local save_file="$2"
    local tls_args=()
    if [ "${skopeo_tls_verify}" != "true" ]; then
        tls_args=(--src-tls-verify=false)
    fi

    if [ -f "$save_file" ]; then
        log "${YELLOW}警告：$save_file 已存在（可能名称冲突），跳过${NC}"
        return 0
    fi

    log "${GREEN}开始导出镜像（skopeo）：$full_image_name${NC}"
    local ok=0
    for attempt in 1 2 3; do
        local tmp_file="${save_file}.part"
        rm -f "$tmp_file"
        if skopeo copy \
              --override-os linux --override-arch amd64 \
              --src-creds "${REGISTRY_USER}:${REGISTRY_PASSWORD}" \
              "${tls_args[@]}" \
              "docker://${full_image_name}" "docker-archive:${tmp_file}:${full_image_name}"; then
            mv -f "$tmp_file" "$save_file"
            ok=1
            break
        fi
        rm -f "$tmp_file"
        log "${YELLOW}导出失败（第 ${attempt}/3 次），5 秒后重试${NC}"
        sleep 5
    done
    if [ "$ok" -ne 1 ]; then
        log "${RED}错误：镜像导出失败：$full_image_name${NC}"
        exit 1
    fi
    log "${GREEN}镜像保存成功：$save_file${NC}"
}

##拉取并保存单个镜像
pull_and_save_single() {
    local full_image_name="$1"
    local save_file="$save_dir/$(image_tar_name "$full_image_name")"

    # skopeo 模式：一步导出，不依赖节点 containerd
    if [ "$copy_tool" = "skopeo" ]; then
        if ! command -v skopeo &> /dev/null; then
            log "${RED}错误：COPY_TOOL=skopeo 但容器内没有 skopeo${NC}"
            exit 1
        fi
        skopeo_copy_single "$full_image_name" "$save_file"
        return 0
    fi

    # 1. 拉取镜像（网络抖动时重试 3 次）
    log "${GREEN}开始拉取镜像：$full_image_name${NC}"
    local pull_ok=0
    for attempt in 1 2 3; do
        if "${container_cmd[@]}" pull "$full_image_name"; then
            pull_ok=1
            break
        fi
        log "${YELLOW}拉取失败（第 ${attempt}/3 次），5 秒后重试${NC}"
        sleep 5
    done
    if [ "$pull_ok" -ne 1 ]; then
        log "${RED}错误：拉取镜像失败：$full_image_name${NC}"
        exit 1
    fi
    log "${GREEN}镜像拉取成功：$full_image_name${NC}"

    # 2. 保存镜像为tar文件
    if [ -f "$save_file" ]; then
        log "${YELLOW}警告：$save_file 已存在（可能名称冲突），跳过${NC}"
        return 0
    fi
    log "${GREEN}开始保存镜像到：$save_file${NC}"
    local save_ok=0
    for attempt in 1 2 3; do
        if "${container_cmd[@]}" save -o "$save_file" "$full_image_name"; then
            save_ok=1
            break
        fi
        log "${YELLOW}保存失败（第 ${attempt}/3 次），清理后重试${NC}"
        [ -f "$save_file" ] && rm -f "$save_file"
        sleep 3
    done
    if [ "$save_ok" -ne 1 ]; then
        log "${RED}错误：保存镜像失败：$full_image_name${NC}"
        if [ -f "$save_file" ]; then
            rm -f "$save_file"
            log "${YELLOW}已清理无效文件：$save_file${NC}"
        fi
        exit 1
    fi
    log "${GREEN}镜像保存成功：$save_file${NC}"
}

##从列表文件拉取镜像（支持两种格式：name: tag / name_tag: tag）
pull_from_file() {
    local image_list_path="$1"
    
    # 检查列表文件是否存在
    if [ ! -f "$image_list_path" ]; then
        log "${RED}错误：镜像列表文件不存在：$image_list_path${NC}"
        exit 1
    fi
    log "${YELLOW}开始从列表文件拉取镜像：$image_list_path${NC}"
    log "${YELLOW}镜像保存目录：$save_dir${NC}"

    # 逐行处理列表文件
    while IFS= read -r line || [[ -n "$line" ]]; do
        # 跳过空行和注释（支持开头带空格的注释）
        if [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]]; then
            continue
        fi

        # 解析镜像名和标签（兼容两种格式）
        if [[ "$line" =~ _tag:[[:space:]]* ]]; then
            # 格式1：name_tag: vx.x.x → 提取为 name: vx.x.x
            image_name=$(echo "$line" | sed 's/_tag:[[:space:]]*v[0-9].*//' | xargs)
            # 用 sed 替代 grep -oP，兼容非 GNU 环境
            image_tag=$(echo "$line" | sed -n 's/.*_tag:[[:space:]]*\(v\?[0-9][0-9.]*\).*/\1/p' | xargs)
        else
            # 格式2：name: vx.x.x → 直接分割
            image_name=$(echo "$line" | awk -F':' '{print $1}' | xargs)
            image_tag=$(echo "$line" | awk -F':' '{print $2}' | xargs)
        fi

        # 校验解析结果（跳过格式错误的行）
        if [[ -z "$image_name" || -z "$image_tag" ]]; then
            log "${YELLOW}警告：跳过无效行（格式错误）：$line${NC}"
            continue
        fi

        # 构建完整镜像名（去掉 _tag 后缀，添加仓库前缀）
        clean_image_name=$(echo "$image_name" | sed 's/_tag$//')
        full_image_name="${repo}${clean_image_name}:${image_tag}"

        # 调用单镜像处理函数
        pull_and_save_single "$full_image_name"
    done < "$image_list_path"
}


# ------------------------------ 主逻辑（参数解析+流程控制） ------------------------------
# 初始化默认参数
local_image_list="$DEFAULT_IMAGE_LIST"  # 默认使用自动化流程的镜像列表
POSITIONAL_ARGS=()                     # 存储命令行传递的镜像名

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            show_help
            exit 0
            ;;
        -c|--cmd)
            # 指定容器工具（docker/nerdctl），允许带参数，例如 "nerdctl -n k8s.io"
            IFS=' ' read -r -a container_cmd <<< "$2"
            log "${YELLOW}已指定容器工具：${container_cmd[*]}${NC}"
            shift 2
            ;;
        -d|--dir)
            # 指定镜像保存目录（必填）
            save_dir="$2"
            # 检查目录是否存在，不存在则创建
            if [ ! -d "$save_dir" ]; then
                log "${YELLOW}保存目录不存在，自动创建：$save_dir${NC}"
                mkdir -p "$save_dir"
            fi
            shift 2
            ;;
        -f|--file)
            # 指定自定义镜像列表文件
            local_image_list="$2"
            log "${YELLOW}已指定自定义镜像列表：$local_image_list${NC}"
            shift 2
            ;;
        -*)
            # 未知选项
            log "${RED}错误：未知选项：$1${NC}"
            show_help
            exit 1
            ;;
        *)
            # 非选项参数：视为镜像名
            POSITIONAL_ARGS+=("$1")
            shift
            ;;
    esac
done

# 恢复 positional 参数（后续处理指定的镜像名）
set -- "${POSITIONAL_ARGS[@]+"${POSITIONAL_ARGS[@]}"}"

# 前置校验（必传参数检查）
if [ -z "$save_dir" ]; then
    log "${RED}错误：必须通过 -d/--dir 指定镜像保存目录${NC}"
    show_help
    exit 1
fi

# 登录仓库（archive 模式才需要；skopeo 用 --src-creds 直接带凭据）
if [ "$copy_tool" = "skopeo" ]; then
    log "${YELLOW}COPY_TOOL=skopeo：跳过容器工具登录，直接导出 docker-archive${NC}"
else
    repo_login
fi

# 分支逻辑：按不同模式执行拉取
if [ $# -eq 0 ]; then
    # 模式1：无额外参数 → 从列表文件拉取（默认或指定-f的文件）
    pull_from_file "$local_image_list"
else
    # 模式2：有额外参数 → 按指定镜像拉取（自动补仓库前缀）
    log "${YELLOW}开始处理指定镜像列表：$*${NC}"
    for image in "$@"; do
        # 若镜像名不含仓库地址，自动添加默认前缀
        if [[ ! "$image" =~ ^hub\.deepflow\.yunshan\.net ]]; then
            full_image="${repo}${image}"
            log "${YELLOW}自动添加仓库前缀：$image → $full_image${NC}"
        else
            full_image="$image"
        fi
        # 调用单镜像处理函数
        pull_and_save_single "$full_image"
    done
fi

# 流程结束
log "${GREEN}===== 所有镜像处理完成！=====${NC}"
log "${WHITE}镜像保存目录：${CYAN}$save_dir${NC}"
log "${WHITE}日志文件路径：${CYAN}$LOG_FILE${NC}"