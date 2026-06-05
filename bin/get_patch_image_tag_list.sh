#!/usr/bin/env bash
# 功能: 支持获取两个 Patch 版本的镜像版本差异
# 要求: 当前脚本执行环境支持在 nuwa 项目目录内执行 git pull 更新项目内容
# 启用严格错误检查（避免隐藏错误）
set -euo pipefail

# 获取脚本所在目录
CURRENT_DIRECTORY=$(cd "$(dirname "$0")" && pwd)
# nuwa 仓库中的大版本/小版本路径（可通过环境变量覆盖）
NUWA_MAJOR="${NUWA_MAJOR:-6.6}"
NUWA_MINOR="${NUWA_MINOR:-6.6.9}"
# grep 匹配的镜像 tag 前缀（可通过环境变量覆盖）
IMAGE_TAG_PREFIX="${IMAGE_TAG_PREFIX:-v6.6}"
# 生成的镜像列表文件
DEFAULT_PATCH_IMAGE_TAG_LIST_FILE="${CURRENT_DIRECTORY}/../latest_image_list/patch_image_tag_list.txt"
# 临时回收站
#TRASH="${CURRENT_DIRECTORY}/../.trash"
# patch 列表（以 OSS 为准）
#OSS_PATCH_LIST="${TRASH}/oss_patch_version.txt"

# 临时回收站
TRASH="${CURRENT_DIRECTORY}/../.trash"

# 如果 .trash 目录不存在，则创建它
if [ ! -d "${TRASH}" ]; then
  mkdir -p "${TRASH}"
fi

# patch 列表（以 OSS 为准）
OSS_PATCH_LIST="${TRASH}/oss_patch_version.txt"

# 如果 oss_patch_version.txt 文件不存在，则创建空文件
if [ ! -f "${OSS_PATCH_LIST}" ]; then
  touch "${OSS_PATCH_LIST}"
fi

# 设置输出日志的颜色及等级
NORMAL_COL="\033[0m"
RED_COL="\033[1;31m"
GREEN_COL="\033[1;32m"
YELLOW_COL="\033[1;33m"
BLUE_COL="\033[1;36m"
# 时间格式
timestamp=$(date '+%Y-%m-%d %H:%M:%S')
# 日志路径
LOG_FILE="$CURRENT_DIRECTORY/../logs/app.log"

debuglog(){ echo -e "[${timestamp}] [DEBUG] [get_patch_image_tag_list.sh] $1 ${NORMAL_COL} ${NORMAL_COL}" >> ${LOG_FILE}; }
infolog(){ echo -e "[${timestamp}] [INFO] [get_patch_image_tag_list.sh] $1 ${GREEN_COL} ${NORMAL_COL}"  >> ${LOG_FILE}; }
warnlog(){ echo -e "[${timestamp}] [WARN] [get_patch_image_tag_list.sh] $1 ${YELLOW_COL} ${NORMAL_COL}" >> ${LOG_FILE}; }
errorlog(){ echo -e "[${timestamp}] [ERROR] [get_patch_image_tag_list.sh] $1 ${RED_COL} ${NORMAL_COL}" >> ${LOG_FILE}; }
usagelog(){ echo -e "[${timestamp}] [get_patch_image_tag_list.sh] $1 ${BLUE_COL} ${NORMAL_COL}" >> ${LOG_FILE}; }


# usage
# 使用方法
function usage() {
  debuglog  "【Usage】"
  usagelog  "  bash $0 [当前补丁版本] [目标补丁版本] [输出文件路径(可选)]"
  debuglog  "【Example】"
  usagelog  "  bash $0 01-20250430-26798-BUG 13-20250528-4661-BUG /tmp/task_patch_image_tag_list.txt"
  debuglog  "【Tips】"
  usagelog  "  获取帮助信息: bash $0 [-h|--help]"   
}

# 检查传参数量
if [ $# -lt 2 ] || [ $# -gt 3 ]; then
    usage
    exit 0
fi

# 获取传参传入的当前补丁版本和目标补丁版本
CURRENT_PATCH_VERSION="$1"
TARGET_PATCH_VERSION="$2"
PATCH_IMAGE_TAG_LIST_FILE="${3:-$DEFAULT_PATCH_IMAGE_TAG_LIST_FILE}"
PATCH_IMAGE_TAG_LIST_DIR=$(dirname "${PATCH_IMAGE_TAG_LIST_FILE}")
mkdir -p "${PATCH_IMAGE_TAG_LIST_DIR}"

TMP_PATCH_IMAGE_TAG_LIST_FILE=$(mktemp "${TRASH}/patch_image_tag_list.XXXXXX")
cleanup() {
    rm -f "${TMP_PATCH_IMAGE_TAG_LIST_FILE}"
}
trap cleanup EXIT

# 拉取最新项目内容
function pull_nuwa_project() {
    infolog "开始 git pull "
    cd ${CURRENT_DIRECTORY}/../nuwa/ && git pull --ff-only>> $LOG_FILE
    if [ $? -eq 0 ];then
       infolog "https://gitlab.yunshan.net/yunshan/deepflow-group/nuwa.git 拉取完成"
       return 0
    else
       errorlog "https://gitlab.yunshan.net/yunshan/deepflow-group/nuwa.git 拉取失败，请检查！！！"
       return 1
    fi
} 

# 用于获取相关升级镜像并生成 patch_image_tag_list.txt，支持传参传入指定的大版本和小版本
function get_patch_image_tag_list() {
    # 获取对应版本全量的 Patch 列表
    ALL_PATCH_LIST=($(cat ${OSS_PATCH_LIST}))

    # 验证版本列表非空
    if [ ${#ALL_PATCH_LIST[@]} -eq 0 ]; then
        errorlog "OSS 版本列表文件为空或不存在：${OSS_PATCH_LIST}"
        exit 1
    fi

    # 获取起始版本的行号
    CURRENT_LINE=$(printf "%s\n" "${ALL_PATCH_LIST[@]}" | grep -n "^${CURRENT_PATCH_VERSION}$" | cut -d: -f1 || true)
    if [ -z "$CURRENT_LINE" ]; then
        errorlog "当前版本 '${CURRENT_PATCH_VERSION}' 在版本列表中未找到，请检查版本名是否正确"
        exit 1
    fi
    CURRENT_PATCH_VERSION_INDEX=$(( CURRENT_LINE - 1 ))

    # 获取目标版本的行号
    TARGET_LINE=$(printf "%s\n" "${ALL_PATCH_LIST[@]}" | grep -n "^${TARGET_PATCH_VERSION}$" | cut -d: -f1 || true)
    if [ -z "$TARGET_LINE" ]; then
        errorlog "目标版本 '${TARGET_PATCH_VERSION}' 在版本列表中未找到，请检查版本名是否正确"
        exit 1
    fi
    TARGET_PATCH_VERSION_INDEX=$(( TARGET_LINE - 1 ))

    # 验证目标版本必须在当前版本之后
    if [ $TARGET_PATCH_VERSION_INDEX -le $CURRENT_PATCH_VERSION_INDEX ]; then
        errorlog "目标版本序号(${TARGET_PATCH_VERSION_INDEX})必须大于当前版本序号(${CURRENT_PATCH_VERSION_INDEX})"
        exit 1
    fi

    echo "CURRENT_PATCH_VERSION_INDEX: $CURRENT_PATCH_VERSION_INDEX"
    echo "TARGET_PATCH_VERSION_INDEX: $TARGET_PATCH_VERSION_INDEX"
    # 获取需要更新的 Patch 版本数
    DIFF_PATCH_INDEX=$(( ${TARGET_PATCH_VERSION_INDEX} - ${CURRENT_PATCH_VERSION_INDEX} ))
    # 获取需要更新的 Patch 列表
    DIFF_PATCH_LIST=(${ALL_PATCH_LIST[@]:$(( $CURRENT_PATCH_VERSION_INDEX + 1 )):$DIFF_PATCH_INDEX})
    infolog "获取需要更新的 Patch 列表"
    for i in ${DIFF_PATCH_LIST[@]};do echo $i >> $LOG_FILE; done
    # 生成需要更新 Patch 的镜像列表集
    rm -f ${PATCH_IMAGE_TAG_LIST_FILE}
    for patch in ${DIFF_PATCH_LIST[@]}; do
        make_file="${CURRENT_DIRECTORY}/../nuwa/$1/$2/$patch/make.sh"
        if [ -f "$make_file" ]; then
            grep ":${IMAGE_TAG_PREFIX}" "$make_file" >> ${TMP_PATCH_IMAGE_TAG_LIST_FILE} 2>/dev/null || true
        fi
    done
    # 过滤需要更新的镜像列表
    cat ${TMP_PATCH_IMAGE_TAG_LIST_FILE} | sed 's/^[[:space:]]*//' | sort -V | tac | awk -F: '!seen[$1]++' | tac | sed -E 's/^([^:]+):(.+)$/\1_tag: \2/' >> ${PATCH_IMAGE_TAG_LIST_FILE}
}

# 主函数
function main() {
    pull_nuwa_project || {
        errorlog "nuwa 仓库拉取失败，终止镜像列表生成"
        exit 1
    }
    infolog "开始生成镜像列表"
    get_patch_image_tag_list "${NUWA_MAJOR}" "${NUWA_MINOR}"
    if [ $? -eq 0 ];then
       infolog "生成镜像列表完成"
    else
       errorlog "生成镜像列表失败，请检查！！！"
       exit 1
    fi
}

main
