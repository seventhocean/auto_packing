#!/bin/bash
# 启用严格错误检查：命令失败、未捕获变量、管道失败时立即退出
set -euo pipefail

# ------------------------------ 用户可配置变量 ------------------------------
# 容器命令，支持 docker 或 nerdctl -nk8s.io
container_cmd="docker"
# 升级后的版本号，按需修改
upgrade_version="DeepFlow-V6.6.9-73"
# 脚本名称（用于帮助信息）
script_name="deepflow_patch_upgrade.sh"
# 路径配置
image_dir="." # 镜像文件存放目录
patch_image_list="patch_image_tag_list.txt" # 镜像标签列表文件
values_yaml="/usr/local/deepflow/templates/values.yaml" # Helm values 文件路径
values_custom="/usr/local/deepflow/templates/values-custom.yaml" # 自定义 values 文件
values_trash="/usr/local/deepflow/templates/.trash" # 自定义 values 文件
source_registry="hub.deepflow.yunshan.net/dev" # 原始镜像仓库
target_registry="hubmgt-uat.paic.com.cn/deepflow"     # 目标镜像仓库
# 定义需要升级的组件列表
components=(
  #openebs
  #df-analyze
  #df-web-ai
  redis
  rabbitmq
  mysql
  mntnct
  opensource
  front-end
  rear-end
  df-help
  df-web-core
  df-web-metrics-explore
  querier-js
  talker
  acl-controller
  warrant
  postman
  log-cleaner
  deepflow
  alarm
  manager
  report
  statistics
  pcap
  grafana
  check
)

# ------------------------------ 全局状态跟踪 ------------------------------
current_step=0                        # 当前操作步骤（用于回滚跟踪）
# ------------------------------ 函数定义 ------------------------------
# 显示使用帮助信息
usage() {
    echo -e "\n\033[1;36m█▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀█\033[0m"
    echo -e "\033[1;36m█             \033[1;37mDeepFlow 升级脚本帮助信息             \033[1;36m█\033[0m"
    echo -e "\033[1;36m█▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄█\033[0m"
    
    echo -e "\n\033[1;33m🛠️ 基本用法:\033[0m"
    echo -e "  \033[1;32m./${script_name} \033[1;35m[选项]\033[0m"
    
    echo -e "\n\033[1;33m📌 可用选项:\033[0m"
    echo -e "  \033[1;35m-v, --version\033[0m  显示脚本版本信息"
    echo -e "  \033[1;35m-h, --help\033[0m     显示脚本帮助信息"
    echo -e "  \033[1;35m-p, --push\033[0m     仅导入并推送镜像到仓库"
    echo -e "  \033[1;35m-u, --upgrade\033[0m  执行完整升级流程（备份、替换、升级）"
    echo -e "  \033[1;35m-c, --check\033[0m    检查升级后的版本是否一致"
    echo -e "  \033[1;35m-r, --rollback\033[0m 回滚到升级前的状态"
    
    echo -e "\n\033[1;33m🔧 更新前准备工作:\033[0m"
    echo -e "  • 修改脚本头部变量 \033[1;36mupgrade_version=\"DeepFlow-V6.4.9-xx\"\033[0m 为实际版本号"
    echo -e "  • 确保脚本和 \033[1;36mpatch_image_tag_list.txt 以及镜像文件包\033[0m 在同一目录"
    
    echo -e "\n\033[1;33m⚙️  可配置变量 (修改脚本头部):\033[0m"
    echo -e "  \033[1;36mcontainer_cmd\033[0m    容器命令 (默认: ${container_cmd})"
    echo -e "  \033[1;36mimage_dir\033[0m        镜像目录 (默认: ${image_dir})"
    echo -e "  \033[1;36msource_registry\033[0m  源仓库地址 (默认: ${source_registry})"
    echo -e "  \033[1;36mtarget_registry\033[0m  目标仓库 (默认: ${target_registry})"
    echo -e "  \033[1;36mpatch_image_list\033[0m 镜像列表文件 (默认: ${patch_image_list})"
    echo -e "  \033[1;36mvalues_yaml\033[0m      Values文件路径 (默认: ${values_yaml})"
    echo -e "  \033[1;36mvalues_custom\033[0m    自定义Values路径 (默认: ${values_custom})"
    
    echo -e "\n\033[1;32m💡 提示: 执行前请仔细阅读上述说明并确认配置正确！\033[0m"
}

# 导入所有 .tar 镜像文件
import_images() {
  echo -e "\n\033[1;34m[步骤 1/$current_step] 导入镜像文件...\033[0m"
  cd "$image_dir" || { echo "目录不存在: $image_dir"; exit 1; }
  
  for img_tar in *.tar; do
    [ -f "$img_tar" ] || continue  # 跳过未找到的情况
    echo "正在导入: $img_tar"
    if ! $container_cmd load -i "$img_tar"; then
      echo -e "\033[1;31m错误: 导入 $img_tar 失败\033[0m"
      exit 1
    fi
  done
  echo -e "\033[1;32m✓ 所有镜像导入成功\033[0m"
  sleep 2  # 添加2秒停顿
}

# 推送镜像到目标仓库
push_images() {
  echo -e "\n\033[1;34m[步骤 2/$current_step] 推送镜像到仓库...\033[0m"
  [ -f "$patch_image_list" ] || { echo "缺少文件: $patch_image_list"; exit 1; }

  while IFS=: read -r image_part tag; do
    # 移除可能的回车符和空格
    image_part=$(echo "$image_part" | tr -d '\r' | xargs)
    tag=$(echo "$tag" | tr -d '\r' | xargs)
    
    # 删除 _tag 后缀（如果存在）
    image=${image_part%_tag}
    
    source_img="$source_registry/$image:$tag"
    target_img="$target_registry/$image:$tag"

    echo "处理镜像: $image:$tag"
    $container_cmd tag "$source_img" "$target_img" || { echo "标记失败: $image"; exit 1; }
    $container_cmd push "$target_img" || { echo "推送失败: $image"; exit 1; }
  done < "$patch_image_list"
  echo -e "\033[1;32m✓ 所有镜像推送成功\033[0m"
  sleep 2  # 添加2秒停顿
}

# 备份配置文件
backup_files() {
  echo -e "\n\033[1;34m[步骤 3/$current_step] 备份配置文件...\033[0m"
  local timestamp=$(date +%Y%m%d%H%M%S)
  cp -v "$values_yaml" "${values_yaml}.bak.${timestamp}" || exit 1
  cp -v "$values_custom" "${values_custom}.bak.${timestamp}" || exit 1
  sleep 2  # 添加2秒停顿
}

# 替换 values.yaml 中的镜像标签
update_image_tags() {
  echo -e "\n\033[1;34m[步骤 4/$current_step] 更新镜像标签...\033[0m"
  [ -f "$patch_image_list" ] || { echo "缺少文件: $patch_image_list"; exit 1; }

  while IFS=: read -r image tag; do
    image=$(echo "$image" | tr -d '\r' | xargs)
    tag=$(echo "$tag" | tr -d '\r' | xargs)
    
    echo "更新: $image → $tag"
    sed -i "s|\(${image}: \).*|\1${tag}|" "$values_yaml" || exit 1
  done < "$patch_image_list"
  echo -e "\033[1;32m✓ 标签更新完成\033[0m"
  sleep 2  # 添加2秒停顿
}

# 更新自定义版本号
update_custom_version() {
  echo -e "\n\033[1;34m[步骤 5/$current_step] 更新版本号...\033[0m"
  sed -i "s/deepflowVersion: .*/deepflowVersion: ${upgrade_version}/" "$values_custom" || exit 1
  echo -e "\033[1;32m✓ 版本号更新为 $upgrade_version\033[0m"
  sleep 2  # 添加2秒停顿
}

# 执行组件升级
upgrade_components() {
  echo -e "\n\033[1;34m[步骤 6/$current_step] 升级组件...\033[0m"
  local deploy_tool="/usr/local/deepflow/bin/deepflow-deploy"
  [ -x "$deploy_tool" ] || { echo "找不到可执行文件: $deploy_tool"; exit 1; }

  # 获取需要升级的组件目录
  # 逻辑为排除不需要升级的组件，现已改为固定那几个组件进行升级
  #local components=$(ls -d /usr/local/deepflow/templates/*/ | xargs -n1 basename | grep -vE 
  #"grafana-agent|opensource|kube-state-metrics|genesis|pcap-rest|influxdb|openebs|baseline")
  
  # 升级每个组件
  for component in "${components[@]}"; do
    echo "升级: $component"
    $deploy_tool -uo "$component" || { echo "升级失败: $component"; exit 1; }
  done
  
  echo -e "\033[1;32m✓ 所有组件升级完成\033[0m"
  sleep 2
}


check_image() {
  echo -e "\n\033[1;34m[检查] 检查已升级镜像版本...\033[0m"
  controller_version=()

  # 读取kubectl输出并清理格式
  while IFS= read -r line; do
      cleaned_line=$(echo "$line" | sed -e 's/[[:space:]]*$//')
      controller_version+=("$cleaned_line")
  done < <(kubectl get pod -n deepflow -o jsonpath='{range.items[*]}{.metadata.name}: {range.spec.containers[*]}{.image}{"\n"}{end}{end}' | awk -F'[/:]' '{printf "%s:%s\n", $(NF-1), $NF}' | sort -u)

  # 读取文件并对比版本
  while IFS= read -r line; do
      [[ -z "$line" || "$line" =~ ^# ]] && continue

      # 处理格式并清理特殊字符
      processed_line=$(echo "$line" | sed -E -e 's/_tag:[[:space:]]+/:/' -e 's/\r//g' -e 's/[[:space:]]*$//')

      # 提取镜像名和版本
      image_name=$(echo "$processed_line" | cut -d: -f1)
      file_version=$(echo "$processed_line" | cut -d: -f2)

					
      if [[ ! "$processed_line" =~ ^[A-Za-z0-9_-]+:[A-Za-z0-9._-]+$ ]]; then
          printf "\033[33m【Skip】 \033[0m 请检查格式：%-40s\n" "$processed_line"
          continue
      fi

      # 查找并对比版本
      found=0
      for version in "${controller_version[@]}"; do
          if echo "$version" | grep -Eq "^${image_name}([:-]|$)"; then
              found=1
              # 使用printf固定宽度，实现对齐显示
              if [ "$version" = "$processed_line" ]; then
                  printf "\033[32m【True】 \033[0m 预期版本：%-40s 当前版本：%s\n" "$processed_line" "$version"
              else
                  printf "\033[31m【False】\033[0m 预期版本：%-40s 当前版本：%s\n" "$processed_line" "$version"
              fi

              break
          fi
      done

      # 未找到对应镜像
      if [ $found -eq 0 ]; then
          printf "\033[31m【False】\033[0m 预期版本：%-40s 当前版本：未找到\n" "$processed_line"
      fi
  done < "patch_image_tag_list.txt"
}

# 回滚操作
rollback_changes() {
  echo -e "\n\033[1;33m[回滚] 恢复配置文件...\033[0m"
  # 查找最新的备份文件
  local latest_values=$(ls -t "${values_yaml}".bak.* 2>/dev/null | head -1)
  local latest_custom=$(ls -t "${values_custom}".bak.* 2>/dev/null | head -1)
   
  echo -e "\033[31m"${latest_values}" -> "${values_yaml}"\033[0m"
  echo -e "\033[31m"${latest_custom}" -> "${values_custom}"\033[0m"
  echo ""
  echo -e "\033[31m"【注意】即将替换以上配置完成配置回滚，请再次确认！"\033[0m"
  read -p "输入【yes】 or 【no】，并按回车键: " yesorno
  

  [[ "x${yesorno}" == x"y" ]]  ||  [[ "x${yesorno}" == x"yes" ]]  || exit 1
  
  
  echo -e "\n\033[1;33m[回滚] 开始恢复配置文件...\033[0m"
  [ -d "${values_trash}" ] || mkdir "${values_trash}"

  if [ -n "$latest_values" ]; then
    mv -v "$values_yaml" "${values_trash}" && mv -v "$latest_values" "$values_yaml" || exit 1
  else
    echo -e "\033[31m没有找到可用的备份 values.yaml\033[0m"  
    exit 1
  fi

  if [ -n "$latest_custom" ]; then
    mv -v "$values_custom" "${values_trash}" && mv -v "$latest_custom" "$values_custom" || exit 1
  else
    echo -e "\033[31m没有找到可用的备份 values-custom.yaml\033[0m"  
    exit 1
  fi

  echo -e "\n\033[1;33m[回滚] 重新使用旧配置更新...\033[0m"
}

# ------------------------------ 主逻辑 ------------------------------
main() {
  case "${1:-}" in
    # 处理参数
    -t|--test)
      push_images
      ;;
    -v|--version)
      echo -e "\033[1;33mVersion 1.1.4 for DeepFlow v6.6.9\033[0m"
      exit 0
      ;;
    -h|--help)
      usage
      ;;
    -p|--push)
      current_step=2
      import_images
      push_images
      ;;
    -u|--upgrade)
      current_step=6  # 总步骤数用于进度显示
      backup_files
      update_image_tags
      update_custom_version
      upgrade_components
      ;;
    -c|--check)
      check_image
      ;;
    -r|--rollback)
      rollback_changes    # 回滚配置
      upgrade_components  # 使用旧配置重新部署
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

# 脚本入口
main "$@"
