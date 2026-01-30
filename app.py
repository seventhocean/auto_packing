import subprocess
import threading
import time
import os
import glob
import re
import json
import shutil
from flask import Flask, request, Response, jsonify, send_file, abort
import traceback
from urllib.parse import quote, unquote

# 初始化Flask应用
#app = Flask(__name__, static_folder='static', static_url_path='/static')
app = Flask(__name__, static_folder='.', static_url_path='')
build_status = {}  # 存储构建任务状态（SSE实时更新用）
build_semaphore = threading.Semaphore(3)  # 限制最大并发任务数为3

# -------------------------- 基础配置（与项目结构对齐） --------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(BASE_DIR, 'bin')   # 脚本/可执行文件
IMAGE_TAR_ROOT = os.path.join(BASE_DIR, 'images')       # 镜像临时存储根目录（按任务分目录）
UPGRADE_PACKAGE_DIR = os.path.join(BASE_DIR, 'upgrade_packages')  # 最终升级包存储目录（TAR.GZ）
LATEST_LIST_DIR = os.path.join(BASE_DIR, 'latest_image_list')  # 镜像列表目录
PATCH_LIST_PATH = os.path.join(LATEST_LIST_DIR, 'patch_image_tag_list.txt')  # 镜像列表文件
PULL_SCRIPT_PATH = os.path.join(BIN_DIR, 'pull_save.sh') # 镜像拉取脚本
LOG_DIR = os.path.join(BASE_DIR, 'logs')        # 日志目录
OSS_FILE = os.path.join(BIN_DIR, 'ossutil')  # ossutil
OSS_CONFIG = os.path.join(BIN_DIR, '.ossutilconfig')
TRASH_DIR = os.path.join(BASE_DIR, '.trash') # 项目临时回收站
OSS_VERSIONS_TMP_FILE = os.path.join(TRASH_DIR, 'oss_patch_version.txt')
PATCH_LIST_SCRIPT_PATH = os.path.join(BIN_DIR, 'get_patch_image_tag_list.sh') # 生成patch_list.txt的脚本路径
UPGRADE_SCRIPT_PATH = os.path.join(BIN_DIR, "deepflow_patch_upgrade.sh")

for dir_path in [IMAGE_TAR_ROOT, UPGRADE_PACKAGE_DIR, LATEST_LIST_DIR, LOG_DIR]:
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)
        write_log(f"自动创建目录：{dir_path}")

#print(OSS_FILE)
# -------------------------- 工具函数（文件列表处理） --------------------------
def write_log(content, level="INFO", task_id=None):
    """写日志（含时间戳，同时输出到文件和控制台）
    新增task_id参数，用于将日志关联到特定任务并推送到前端
    """
    log_file = os.path.join(LOG_DIR, 'app.log')
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    log_line = f"[{timestamp}] [{level}] {content}\n"
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(log_line)
    print(log_line.strip())
    
    # 如果有任务ID，将日志添加到任务状态中，供前端展示
    if task_id and task_id in build_status:
        if 'logs' not in build_status[task_id]:
            build_status[task_id]['logs'] = []
            
        build_status[task_id]['logs'].append({
            'timestamp': timestamp,
            'level': level,
            'content': content
        })
        
        if len(build_status[task_id]['logs']) > 100:
            build_status[task_id]['logs'] = build_status[task_id]['logs'][-100:]

def update_progress(task_id, new_percent, message, level="INFO"):
    """更新任务进度的辅助函数，确保进度平滑增加"""
    if task_id not in build_status:
        return
        
    current_percent = build_status[task_id].get('percent', 0)
    if new_percent > current_percent:
        steps = new_percent - current_percent
        for i in range(steps):
            percent = current_percent + i + 1
            build_status[task_id].update({
                "status": "progress",
                "percent": percent,
                "message": message if i == steps - 1 else build_status[task_id].get('message', '')
            })
            if steps > 5 and (i % (steps // 5) == 0 or i == steps - 1):
                progress_msg = f"处理中...({percent}%)"
                write_log(progress_msg, level, task_id)   
            time.sleep(0.1)

def get_oss_versions():
    """从OSS获取x86_64架构的补丁版本列表（供前端下拉框用）
    按文件名前缀序号（01~16）排序，前端直接沿用此顺序
    """
    try:
        result = subprocess.run(
            [OSS_FILE,"-c", OSS_CONFIG, "ls", "-d", "oss://df-patch-no-delete/patch/6.6/6.6.9/"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            check=True
        )
        
        versions = []
        seen_versions = set()
        
        for line in result.stdout.split('\n'):
            line = line.strip()
            if line and ".tar.gz" in line and "x86_64" in line and not line.endswith('/'):
                # 提取完整文件名（如"01-20250430-26798-BUG_x86_64.tar.gz"）
                file_name = line.split('/')[-1]
                #seq_match = re.search(r'^(\d{3})-', file_name)
                seq_match = re.search(r'^(\d+)-', file_name)   #支持3位数以上patch
                seq_num = int(seq_match.group(1)) if seq_match else 99  # 异常序号放最后

                # 处理前端显示的版本名（去掉.tar.gz和_x86_64）
                version_with_arch = file_name[:-len(".tar.gz")]
                display_version = version_with_arch.rstrip('_x86_64')
                
                if display_version not in seen_versions:
                    seen_versions.add(display_version)
                    date_match = re.search(r'(\d{8})', display_version)
                    versions.append({
                        'value': display_version,
                        'display': display_version,
                        'seq_num': seq_num,
                        'date': date_match.group(1) if date_match else ""  # 仅存储，不排序
                    })
        
        # 后端严格按序号升序排序，前端直接用此顺序
        versions.sort(key=lambda x: x['seq_num'])
        write_log(f"OSS版本按序号排序完成，共{len(versions)}个版本")
        
        # 保留 OSS 获取的 PATCH 版本到临时文本，方便其他脚本使用
        with open(OSS_VERSIONS_TMP_FILE, 'w', encoding='utf-8') as f:
            for ver in versions:
                f.write(f"{ver['value']}\n")
        
        write_log(f"OSS 获取的 PATCH 版本已经成功保存到临时文本：{OSS_VERSIONS_TMP_FILE}", level="INFO")
        return versions
    
    except Exception as e:
        error_msg = f"OSS版本获取失败：{str(e)}"
        write_log(error_msg, level="ERROR")
        return []

def get_existing_packages():
    """新增：获取UPGRADE_PACKAGE_DIR目录下的所有tar.gz升级包列表
    返回格式：[{name: 文件名, size: 文件大小(字节)}, ...]
    """
    try:
        # 筛选目录下所有.tar.gz文件
        package_pattern = os.path.join(UPGRADE_PACKAGE_DIR, "*.tar.gz")
        package_files = glob.glob(package_pattern)
        
        packages = []
        for file_path in package_files:
            if os.path.isfile(file_path):
                file_name = os.path.basename(file_path)
                file_size = os.path.getsize(file_path)
                packages.append({
                    "name": file_name,
                    "size": file_size
                })
        
        # 按文件修改时间降序排序（最新生成的包排在前面）
        packages.sort(key=lambda x: os.path.getmtime(os.path.join(UPGRADE_PACKAGE_DIR, x["name"])), reverse=True)
        write_log(f"获取已有升级包成功，共{len(packages)}个有效包")
        return packages
    
    except Exception as e:
        error_msg = f"获取已有升级包列表失败：{str(e)}"
        write_log(error_msg, level="ERROR")
        return []


def validate_package_filename(filename):
    """新增：校验文件名合法性（防止目录遍历攻击）
    规则：1. 仅允许.tar.gz后缀；2. 不包含../等路径字符；3. 文件必须在UPGRADE_PACKAGE_DIR目录内
    """
    # 禁止空文件名
    if not filename:
        return False, "文件名不能为空"
    
    # 仅允许.tar.gz后缀
    if not filename.endswith(".tar.gz"):
        return False, "仅支持.tar.gz格式的升级包"
    
    # 禁止包含路径分隔符（防止目录遍历，如../../etc/passwd.tar.gz）
    if "/" in filename or "\\" in filename:
        return False, "文件名不允许包含路径分隔符"
    
    # 计算绝对路径，确保在安全目录内
    safe_dir_abs = os.path.abspath(UPGRADE_PACKAGE_DIR)
    file_path_abs = os.path.abspath(os.path.join(UPGRADE_PACKAGE_DIR, filename))
    if not file_path_abs.startswith(safe_dir_abs):
        return False, "文件不在允许的下载目录内"
    
    # 确认文件存在且是普通文件
    if not os.path.exists(file_path_abs) or not os.path.isfile(file_path_abs):
        return False, "文件不存在或不是有效文件"
    
    return True, file_path_abs


def run_build_task(task_id, current_version, target_version):
    """核心构建任务：执行构建list脚本→拉取镜像→打包TAR.GZ格式升级包"""
    with build_semaphore:  # 控制并发任务数量
        # 创建任务专属目录（用版本+任务ID命名，确保唯一性，避免文件冲突）
        task_dir_name = f"{current_version}_to_{target_version}_{task_id}"
        task_image_dir = os.path.join(IMAGE_TAR_ROOT, task_dir_name)
        # 确保任务目录存在（不存在则创建，存在则清空历史残留）
        if os.path.exists(task_image_dir):
            shutil.rmtree(task_image_dir)
        os.makedirs(task_image_dir, exist_ok=True)

        # 创建任务专属日志文件（记录脚本详细输出，避免实时刷屏）
        task_log_path = os.path.join(LOG_DIR, f"task_{task_id}.log")

        try:
            # 初始化任务状态（SSE实时推送用），新增logs字段存储前端日志
            build_status[task_id] = {
                "status": "progress",
                "percent": 0,
                "message": "初始化构建任务，检查核心依赖",
                "logs": []  # 用于存储前端展示的日志
            }
            write_log(f"任务[{task_id}]启动：版本升级 {current_version} → {target_version}", task_id=task_id)
            time.sleep(1)  # 预留SSE状态推送时间，避免前端接收延迟

            # 检查核心依赖脚本（缺失则直接终止任务）
            dependencies = [
                (PATCH_LIST_SCRIPT_PATH, "生成patch_list的Shell脚本"),
                (PULL_SCRIPT_PATH, "镜像拉取的Shell脚本")
            ]
            
            update_progress(task_id, 5, "检查核心依赖脚本")
            for dep_path, dep_desc in dependencies:
                if not os.path.exists(dep_path):
                    raise Exception(f"核心依赖缺失：{dep_desc}路径不存在 → {dep_path}")
                write_log(f"验证依赖：{dep_desc}存在", task_id=task_id)
                time.sleep(0.5)
            
            update_progress(task_id, 10, "所有依赖检查通过，开始生成镜像列表")
            write_log(f"任务[{task_id}]依赖检查通过：所有Shell脚本均存在", task_id=task_id)

            # 执行get_patch_image_tag_list.sh，生成镜像拉取清单
            update_progress(task_id, 15, f"执行镜像列表生成脚本，参数：{current_version} {target_version}")
            write_log(f"开始生成镜像列表，当前版本: {current_version}, 目标版本: {target_version}", task_id=task_id)
            
            # 调用Shell脚本，传递当前版本和目标版本参数
            with open(task_log_path, 'a', encoding='utf-8') as f:
                script_result = subprocess.run(
                    ["/bin/bash", PATCH_LIST_SCRIPT_PATH, current_version, target_version],
                    check=True,
                    stdout=f,  # 捕获标准输出
                    stderr=subprocess.STDOUT, # 标准错误合并到标准输出
                    universal_newlines=True
                )
            
            update_progress(task_id, 25, "验证镜像列表生成结果")
            # 验证脚本执行结果：必须生成patch_image_tag_list.txt
            if not os.path.exists(PATCH_LIST_PATH):
                raise Exception(f"镜像列表脚本执行失败：未在{LATEST_LIST_DIR}生成patch_image_tag_list.txt")
            
            # 读取并记录脚本输出日志
            with open(task_log_path, 'r', encoding='utf-8') as f:
                script_log = f.read()
            write_log(f"镜像列表生成成功，脚本输出已记录", task_id=task_id)
            
            update_progress(task_id, 30, "镜像拉取清单生成完成，准备拉取镜像文件")
            # 读取镜像列表文件，获取要拉取的镜像数量
            with open(PATCH_LIST_PATH, 'r', encoding='utf-8') as f:
                image_count = len([line for line in f if line.strip()])
            write_log(f"发现 {image_count} 个需要拉取的镜像", task_id=task_id)

            # 执行pull_save.sh，拉取镜像到任务专属目录
            update_progress(task_id, 35, f"开始拉取 {image_count} 个镜像文件")
            write_log(f"开始拉取镜像，存储路径：{task_image_dir}", task_id=task_id)
            
            # 调用拉取脚本，指定任务专属目录（确保镜像仅属于当前任务）
            with open(task_log_path, 'a', encoding='utf-8') as f:
                pull_result = subprocess.run(
                    ["/bin/bash", PULL_SCRIPT_PATH, "-d", task_image_dir],
                    check=True,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True
                )
            
            # 验证镜像拉取结果：任务目录必须有.tar文件
            tar_files = glob.glob(os.path.join(task_image_dir, "*.tar"))
            if not tar_files:
                raise Exception(f"镜像拉取失败：任务目录{task_image_dir}无任何.tar镜像文件")
            
            update_progress(task_id, 65, f"镜像拉取完成，共拉取 {len(tar_files)} 个")
            write_log(f"镜像拉取成功！共拉取{len(tar_files)}个镜像", task_id=task_id)

            # 准备打包
            update_progress(task_id, 70, "准备打包升级包，复制镜像列表文件")
            temp_patch_list = os.path.join(task_image_dir, "patch_image_tag_list.txt")
            shutil.copy2(PATCH_LIST_PATH, temp_patch_list)
            
            if not os.path.exists(UPGRADE_SCRIPT_PATH) or not os.path.isfile(UPGRADE_SCRIPT_PATH):
                raise Exception(f"升级脚本不存在：{UPGRADE_SCRIPT_PATH}")
            files_to_pack = tar_files + [temp_patch_list, UPGRADE_SCRIPT_PATH]
            write_log(f"准备打包 {len(files_to_pack)} 个文件", task_id=task_id)

            # 打包TAR.GZ升级包（仅包含当前任务的镜像+镜像列表）
            update_progress(task_id, 75, "开始打包TAR.GZ升级包")
            current_patch_version = current_version.split('-')[0]
            target_patch_version = target_version.split('-')[0]
            upgrade_package = f"deepflow_patch_v669_{current_patch_version}_{target_patch_version}.tar.gz"
            upgrade_path = os.path.join(UPGRADE_PACKAGE_DIR, upgrade_package)
            
            # 执行TAR.GZ打包（--transform确保解包后无嵌套目录）
            with open(task_log_path, 'a', encoding='utf-8') as f:
                tar_result = subprocess.run(
                    [
                        "tar", "-czf", upgrade_path,  # 核心参数：创建gzip压缩包
                        "--transform", "s/.*\///",    # 关键：仅保留文件名，删除路径前缀
                        *files_to_pack                # 待打包的所有文件
                    ],
                    check=True,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True
                )

            if not os.path.exists(upgrade_path) or os.path.getsize(upgrade_path) == 0:
                raise Exception(f"打包失败：升级包{upgrade_path}不存在或为空文件")
            
            update_progress(task_id, 90, "验证升级包完整性")
            # 验证包大小
            package_size = os.path.getsize(upgrade_path)
            write_log(f"升级包大小：{package_size/1024/1024:.2f}MB", task_id=task_id)

            # 清理临时文件
            update_progress(task_id, 95, "清理临时文件")
            os.remove(temp_patch_list)
            write_log(f"临时文件清理完成", task_id=task_id)

            # 任务完成：更新状态并记录日志
            update_progress(task_id, 100, f"构建成功！生成TAR.GZ升级包（{len(tar_files)}个镜像+2个文件）")
            build_status[task_id].update({
                "status": "complete",
                "percent": 100,
                "message": f"构建成功！生成TAR.GZ升级包（{len(tar_files)}个镜像+2个文件）",
                "complete": True,
                "error": False,
                "download_url": f"/download/{task_id}",
                "package_path": upgrade_path,
                "package_name": upgrade_package,
                "package_format": "tar.gz",  # 标记包格式，便于前端显示
                "package_size_mb": round(os.path.getsize(upgrade_path)/1024/1024, 2)  # 包大小（MB）
            })
            write_log(f"任务[{task_id}]完全结束：{current_version}→{target_version}升级包构建完成，请在90秒内下载升级包", task_id=task_id)
            
            # 成功后延迟90秒删除状态，给前端足够时间获取最终状态
            time.sleep(90)
            if task_id in build_status:
                del build_status[task_id]

        except Exception as e:
            # 任务失败：捕获异常并更新状态
            error_msg = str(e)
            build_status[task_id].update({
                "status": "error",
                "message": f"构建失败：{error_msg}",
                "complete": True,
                "error": True
            })
            write_log(f"任务失败：{error_msg}", level="ERROR", task_id=task_id)
            # 打印异常堆栈，便于问题排查
            traceback.print_exc()
            
            # 失败后延迟5秒删除状态，给前端足够时间接收错误状态
            time.sleep(50)
            if task_id in build_status:
                del build_status[task_id]

        finally:
            # 最终清理：无论成功/失败，都删除任务专属镜像目录（节省磁盘空间）
            if os.path.exists(task_image_dir):
                try:
                    shutil.rmtree(task_image_dir)
                    write_log(f"任务临时目录清理完成：{task_image_dir}", task_id=task_id)
                except Exception as clean_e:
                    # 清理失败不影响任务结果，但需记录日志（避免磁盘空间泄漏）
                    clean_error_msg = f"任务临时目录清理失败：{str(clean_e)}"
                    write_log(clean_error_msg, level="WARNING", task_id=task_id)
            
            # 清理任务日志文件
            if os.path.exists(task_log_path):
                try:
                    os.remove(task_log_path)
                    write_log(f"任务临时日志文件已清理", task_id=task_id)
                except Exception as log_e:
                    write_log(f"任务临时日志清理失败：{str(log_e)}", level="WARNING", task_id=task_id)
                    


# -------------------------- Flask路由 --------------------------
@app.route('/')
def index():
    """前端页面入口（返回静态HTML）"""
    return send_file('index.html')

# -------------------------- 获取已有升级包列表 --------------------------
@app.route('/existing-packages', methods=['GET'])
def api_existing_packages():
    """
    前端调用：获取UPGRADE_PACKAGE_DIR目录下所有已生成的tar.gz升级包
    """
    try:
        # 调用工具函数获取文件列表
        packages = get_existing_packages()
        return jsonify({
            "success": True,
            "files": packages,
            "message": f"共获取到{len(packages)}个已生成的升级包"
        })
    except Exception as e:
        error_msg = f"获取已有升级包列表失败：{str(e)}"
        write_log(error_msg, level="ERROR")
        return jsonify({
            "success": False,
            "files": [],
            "message": error_msg
        }), 500


# -------------------------- 下载已有升级包 --------------------------
@app.route('/download-existing/<filename>', methods=['GET'])
def api_download_existing(filename):
    """
    前端调用：直接下载已生成的升级包（通过文件名定位）
    """
    # 解码URL编码的文件名
    try:
        filename = unquote(filename)
    except Exception as e:
        error_msg = f"文件名解码失败：{str(e)}"
        write_log(error_msg, level="ERROR")
        abort(400, description=error_msg)

    # 执行安全校验
    valid, result = validate_package_filename(filename)
    if not valid:
        error_msg = f"文件下载校验失败：{result}"
        write_log(error_msg, level="WARNING")
        abort(403, description=error_msg)

    # 校验通过，执行下载
    file_path = result
    try:
        response = send_file(
            file_path,
            as_attachment=True,
            attachment_filename=filename,
            mimetype='application/gzip'
        )
        response.headers['Accept-Ranges'] = 'bytes'
        write_log(f"已有升级包下载成功：文件名={filename}，路径={file_path}")
        return response
    except Exception as e:
        error_msg = f"文件下载失败：{str(e)}"
        write_log(error_msg, level="ERROR")
        abort(500, description=error_msg)


# -------------------------- 获取版本列表（兼容前端） --------------------------
@app.route('/versions', methods=['GET'])
def api_get_versions():
    """原有接口：从OSS获取版本列表，供前端下拉框选择"""
    versions = get_oss_versions()
    if versions:
        return jsonify({
            "success": True,
            "versions": versions,
            "message": f"成功获取{len(versions)}个版本"
        })
    else:
        return jsonify({
            "success": False,
            "versions": [],
            "message": "未获取到有效版本（可能是OSS连接失败或无x86_64版本）"
        }), 500


# -------------------------- 查询任务状态 --------------------------
@app.route('/task-status/<task_id>', methods=['GET'])
def api_task_status(task_id):
    """查询指定任务的当前状态，包含日志信息"""
    if task_id in build_status:
        return jsonify({
            "success": True,
            "status": build_status[task_id]
        })
    else:
        return jsonify({
            "success": False,
            "message": "任务不存在或已过期"
        }), 404


# -------------------------- 构建任务（兼容前端SSE） --------------------------
@app.route('/build', methods=['GET'])
def api_build():
    """原有接口：启动构建任务，通过SSE实时推送进度和日志"""
    # 检查并发任务数
    if build_semaphore._value == 0:
        return jsonify({
            "success": False,
            "message": "当前构建任务已达上限，请稍后再试"
        }), 429  # 429 Too Many Requests
    
    # 获取前端传递的版本参数
    current_version = request.args.get('current')
    target_version = request.args.get('target')
    
    # 参数校验
    if not current_version or not target_version:
        return jsonify({"success": False, "message": "缺少必要参数：current或target版本不能为空"}), 400
    if current_version == target_version:
        return jsonify({"success": False, "message": "当前版本与目标版本不能相同"}), 400

    # 生成唯一任务ID
    timestamp = int(time.time())
    current_abbr = current_version[:8] if len(current_version)>=8 else current_version
    target_abbr = target_version[:8] if len(target_version)>=8 else target_version
    task_id = f"build_{current_abbr}_to_{target_abbr}_{timestamp}"

    # 启动异步构建任务
    build_thread = threading.Thread(
        target=run_build_task,
        args=(task_id, current_version, target_version),
        daemon=True
    )
    build_thread.start()

    # 建立SSE连接，实时推送进度和日志
    def generate_sse():
        # 初始状态推送
        retry_count = 0
        initial_status = None
        while retry_count < 2 and not initial_status:
            initial_status = build_status.get(task_id, {
                'status': 'progress',
                'percent': 0,
                'message': '任务初始化中...',
                'logs': []
            })
            if not initial_status:
                time.sleep(0.5)
                retry_count += 1
        yield f"data: {json.dumps(initial_status)}\n\n"
        
        # 循环推送状态
        while True:
            if task_id not in build_status:
                # 重试3次，若仍不存在则判定过期
                retry = 0
                while retry < 3:
                    if task_id in build_status:
                        break
                    time.sleep(0.5)
                    retry += 1
                if task_id not in build_status:
                    yield f"data: {json.dumps({'status': 'error', 'percent': 0, 'message': '任务状态已过期，请刷新页面重试', 'logs': []})}\n\n"
                    break
            
            task_status = build_status[task_id]
            yield f"data: {json.dumps(task_status)}\n\n"
            
            # 任务完成，终止循环
            if task_status.get('complete', False):
                break
            
            # 控制推送频率
            time.sleep(1)  # 提高推送频率，使进度更新更及时

    # 设置SSE响应头
    return Response(generate_sse(), mimetype='text/event-stream')


# -------------------------- 下载新构建的包（兼容） --------------------------
@app.route('/download/<task_id>', methods=['GET'])
def api_download(task_id):
    """原有接口：下载新构建的升级包（通过任务ID定位）"""
    if task_id not in build_status:
        error_msg = f"任务{task_id}不存在或已过期（请重新构建）"
        write_log(error_msg, level="WARNING")
        return jsonify({"success": False, "message": error_msg}), 404

    task_status = build_status[task_id]
    if task_status.get('status') != 'complete' or not task_status.get('package_path'):
        error_msg = f"任务{task_id}未构建完成，无法下载"
        write_log(error_msg, level="WARNING")
        return jsonify({"success": False, "message": error_msg}), 400

    # 校验包路径合法性
    package_path = task_status['package_path']
    if not os.path.exists(package_path) or not package_path.endswith('.tar.gz'):
        error_msg = f"任务{task_id}的升级包无效（路径不存在或格式错误）"
        write_log(error_msg, level="ERROR")
        return jsonify({"success": False, "message": error_msg}), 500

    # 执行下载
    try:
        response = send_file(
            package_path,
            as_attachment=True,
            attachment_filename=task_status['package_name'],
            mimetype='application/gzip'
        )
        response.headers['Accept-Ranges'] = 'bytes'
        write_log(f"新构建包下载成功：任务ID={task_id}，文件名={task_status['package_name']}")
        return response
    except Exception as e:
        error_msg = f"新构建包下载失败：{str(e)}"
        write_log(error_msg, level="ERROR")
        return jsonify({"success": False, "message": error_msg}), 500


# -------------------------- 应用启动入口 --------------------------
if __name__ == '__main__':
    write_log("DeepFlow升级包构建服务启动成功！")
    write_log(f"服务配置：升级包存储目录={UPGRADE_PACKAGE_DIR}，日志目录={LOG_DIR}")
    app.run(host='0.0.0.0', port=8000, threaded=True, debug=True)
