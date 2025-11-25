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
BASE_DIR="/home/auto_packing_no_delete"
# 默认镜像列表文件（由oss脚本生成）
DEFAULT_IMAGE_LIST="$BASE_DIR/latest_image_list/patch_image_tag_list.txt"
# 日志文件（统一存储到项目logs目录）
LOG_FILE="$BASE_DIR/logs/pull_save.log"

# 容器工具（默认nerdctl，支持通过--cmd切换为docker）
#container_cmd="docker"
container_cmd="nerdctl"
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
    echo -e "  ${GREEN}-c, --cmd      ${NC}指定容器工具（${BOLD}docker${NC}/${BOLD}nerdctl${NC}，默认docker）"
    echo -e "  ${GREEN}-d, --dir      ${NC}指定镜像保存目录（必填，例如：$BASE_DIR/image_tar）"
    echo -e "  ${GREEN}-f, --file     ${NC}指定镜像列表文件（默认：$DEFAULT_IMAGE_LIST）\n"
    
    echo -e "${WHITE}使用示例:${NC}"
    echo -e "  ${BOLD}1. 自动化流程调用（从默认列表拉取到image_tar）:${NC}"
    echo -e "     ${CYAN}$0 -d $BASE_DIR/image_tar${NC}\n"
    
    echo -e "  ${BOLD}2. 手动指定列表文件拉取:${NC}"
    echo -e "     ${CYAN}$0 -d ./my_dir -f ./my_image_list.txt${NC}\n"
    
    echo -e "  ${BOLD}3. 手动拉取单个镜像（自动添加仓库前缀）:${NC}"
    echo -e "     ${CYAN}$0 -d ./my_dir deepflow-server:v6.6.5550${NC}\n"
    
    echo -e "  ${BOLD}4. 使用nerdctl拉取多个镜像:${NC}"
    echo -e "     ${CYAN}$0 -c nerdctl -d ./my_dir deepflow-server:v6.6.5550 pcap:v6.6.220${NC}\n"
    
    echo -e "  ${BOLD}5. 拉取完整镜像地址（不自动添加前缀）:${NC}"
    echo -e "     ${CYAN}$0 -d ./my_dir hub.deepflow.yunshan.net/dev/deepflow-server:feature-scp-66${NC}\n"
    exit 0
}

##镜像仓库登录（使用预设账号密码）
repo_login() {
    log "${YELLOW}开始登录镜像仓库：hub.deepflow.yunshan.net${NC}"
    # 检查容器工具是否存在
    if ! command -v "$container_cmd" &> /dev/null; then
        log "${RED}错误：容器工具 $container_cmd 未安装或未配置到环境变量${NC}"
        exit 1
    fi

    # 执行登录（密码通过管道传递，避免明文暴露）
    if ! echo "35lRrgBcLhF" | "$container_cmd" login --username=acrpush@yunshan --password-stdin hub.deepflow.yunshan.net; then
        log "${RED}错误：仓库登录失败！请检查账号密码或网络连接${NC}"
        exit 1
    fi
    log "${GREEN}仓库登录成功${NC}"
}

##拉取并保存单个镜像
pull_and_save_single() {
    local full_image_name="$1"
    
    # 1. 拉取镜像
    log "${GREEN}开始拉取镜像：$full_image_name${NC}"
    if ! "$container_cmd" pull "$full_image_name"; then
        log "${RED}错误：拉取镜像失败：$full_image_name${NC}"
        exit 1
    fi
    log "${GREEN}镜像拉取成功：$full_image_name${NC}"

    # 2. 提取镜像名和标签（处理特殊字符，避免文件名异常）
    # 示例：hub.deepflow.yunshan.net/dev/deepflow-server:v6.6.5550 → 提取为 deepflow-server_v6.6.5550.tar
    local image_name=$(echo "$full_image_name" | awk -F':' '{print $1}' | awk -F'/' '{print $NF}' | sed 's/[^a-zA-Z0-9_-]//g')
    local image_tag=$(echo "$full_image_name" | awk -F':' '{print $2}' | sed 's/[^a-zA-Z0-9._-]//g')
    local save_file="$save_dir/${image_name}_${image_tag}.tar"

    # 3. 保存镜像为tar文件
    log "${GREEN}开始保存镜像到：$save_file${NC}"
    if ! "$container_cmd" save -o "$save_file" "$full_image_name"; then
        log "${RED}错误：保存镜像失败：$full_image_name${NC}"
        # 清理失败的空文件
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
            image_tag=$(echo "$line" | grep -oP '_tag:[[:space:]]*\Kv?[0-9.]+' | xargs)
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
            ;;
        -c|--cmd)
            # 指定容器工具（docker/nerdctl）
            container_cmd="$2"
            log "${YELLOW}已指定容器工具：$container_cmd${NC}"
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

# 登录仓库（所有拉取流程的前置操作）
repo_login

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