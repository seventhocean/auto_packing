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
from urllib.parse import quote
from wsgiref.util import FileWrapper  # 用于流式传输

# 初始化Flask应用
app = Flask(__name__, static_folder='.', static_url_path='')
build_status = {}  # 存储构建任务状态（SSE实时更新用）

# -------------------------- 基础配置（与项目结构对齐） --------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_TAR_DIR = os.path.join(BASE_DIR, 'image_tar')       # 镜像/升级包目录
LATEST_LIST_DIR = os.path.join(BASE_DIR, 'latest_image_list')  # 镜像列表目录
PATCH_LIST_PATH = os.path.join(LATEST_LIST_DIR, 'patch_image_tag_list.txt')  # 镜像列表文件
PULL_SCRIPT_PATH = os.path.join(BASE_DIR, 'pull_save.sh') # 镜像拉取脚本
LOG_DIR = os.path.join(BASE_DIR, 'logs')                  # 日志目录

# 确保目录存在（首次运行自动创建）
for dir_path in [IMAGE_TAR_DIR, LATEST_LIST_DIR, LOG_DIR]:
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)


# -------------------------- 工具函数 --------------------------
def write_log(content, level="INFO"):
    """写日志（含时间戳，同时输出到文件和控制台）"""
    log_file = os.path.join(LOG_DIR, 'app.log')
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    log_line = f"[{timestamp}] [{level}] {content}\n"
    # 写入文件
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(log_line)
    # 打印到控制台
    print(log_line.strip())


def get_oss_versions():
    """从OSS获取x86_64架构的补丁版本列表（供前端下拉框）
    仅筛选x86_64架构的.tar.gz文件，前端显示格式：62-20250815-4924-BUG_x86_64
    """
    try:
        # 调用ossutil列出目标目录下的文件
        result = subprocess.run(
            ["ossutil", "ls", "-d", "oss://df-patch-no-delete/patch/6.6/6.6.9/"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            check=True
        )
        
        versions = []
        seen_versions = set()
        
        for line in result.stdout.split('\n'):
            line = line.strip()
            # 筛选条件：包含.tar.gz且包含x86_64，同时排除目录行
            if line and ".tar.gz" in line and "x86_64" in line and not line.endswith('/'):
                # 提取文件名（如"62-20250815-4924-BUG_x86_64.tar.gz"）
                file_name = line.split('/')[-1]
                # 去掉后缀.tar.gz，得到前端显示的版本字符串
                display_version = file_name[:-len(".tar.gz")]
                
                if display_version not in seen_versions:
                    seen_versions.add(display_version)
                    # 提取日期部分用于排序
                    date_match = re.search(r'(\d{8})', display_version)
                    date_str = date_match.group(1) if date_match else ""
                    
                    versions.append({
                        'value': display_version,
                        'display': display_version,
                        'date': date_str
                    })
        
        # 按日期升序排序
        versions.sort(key=lambda x: x['date'])
        write_log(f"从OSS获取x86_64版本成功，共{len(versions)}个有效版本")
        return versions
    
    except subprocess.CalledProcessError as e:
        error_msg = f"OSS命令执行失败：{e.stderr.strip()}"
        write_log(error_msg, level="ERROR")
        return []
    except Exception as e:
        error_msg = f"OSS版本获取失败：{str(e)}"
        write_log(error_msg, level="ERROR")
        return []


def run_build_task(task_id, current_version, target_version):
    """核心构建任务：拉取镜像→打包升级包"""
    try:
        # 1. 初始化任务状态
        build_status[task_id] = {
            "status": "progress",
            "percent": 0,
            "message": "初始化构建任务，检查依赖"
        }
        write_log(f"任务[{task_id}]启动：{current_version} → {target_version}")
        time.sleep(1)

        # 2. 检查核心依赖
        if not os.path.exists(PATCH_LIST_PATH):
            raise Exception(f"镜像列表文件缺失：{PATCH_LIST_PATH}（请检查OSS同步脚本）")
        if not os.path.exists(PULL_SCRIPT_PATH):
            raise Exception(f"拉取脚本缺失：{PULL_SCRIPT_PATH}")
        
        build_status[task_id] = {
            "status": "progress",
            "percent": 20,
            "message": "依赖检查通过，开始拉取镜像"
        }
        time.sleep(1)

        # 3. 调用pull_save.sh拉取镜像
        pull_result = subprocess.run(
            ["/bin/bash", PULL_SCRIPT_PATH, "-d", IMAGE_TAR_DIR],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        write_log(f"镜像拉取输出：\n{pull_result.stdout}")
        build_status[task_id] = {
            "status": "progress",
            "percent": 70,
            "message": "镜像拉取完成，开始打包升级包"
        }
        time.sleep(2)

        # 4. 打包升级包（含镜像.tar + 镜像列表）
        os.chdir(IMAGE_TAR_DIR)
        tar_files = glob.glob("*.tar")  # 获取所有镜像文件
        if not tar_files:
            raise Exception(f"镜像目录{IMAGE_TAR_DIR}无镜像文件（拉取失败）")
        
        # 临时复制镜像列表到打包目录
        temp_patch_list = os.path.join(IMAGE_TAR_DIR, 'patch_image_tag_list.txt')
        shutil.copy2(PATCH_LIST_PATH, temp_patch_list)
        tar_files.append(temp_patch_list)

        # 生成升级包文件名
        upgrade_package = f"upgrade_{current_version}_to_{target_version}_{task_id}.zip"
        upgrade_path = os.path.join(IMAGE_TAR_DIR, upgrade_package)

        # 执行打包（-j：不保留目录结构）
        zip_result = subprocess.run(
            ["zip", "-j", upgrade_path] + tar_files,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        write_log(f"打包输出：\n{zip_result.stdout}")

        # 清理临时文件
        os.remove(temp_patch_list)

        # 5. 构建完成
        build_status[task_id] = {
            "status": "complete",
            "percent": 100,
            "message": f"构建成功！含{len(tar_files)-1}个镜像+1个列表文件",
            "complete": True,
            "download_url": f"/download/{task_id}",
            "package_path": upgrade_path,
            "package_name": upgrade_package
        }
        write_log(f"任务[{task_id}]完成，升级包：{upgrade_package}")

    except Exception as e:
        # 构建失败处理
        error_msg = str(e)
        build_status[task_id] = {
            "status": "error",
            "percent": 0,
            "message": f"构建失败：{error_msg}",
            "complete": True,
            "error": True
        }
        write_log(f"任务[{task_id}]失败：{error_msg}", level="ERROR")


# -------------------------- Flask路由 --------------------------
@app.route('/')
def index():
    """前端页面入口"""
    return send_file('index.html')


@app.route('/versions')
def versions():
    """获取版本列表接口（前端下拉框用）"""
    versions = get_oss_versions()
    return jsonify({'success': True, 'versions': versions})


@app.route('/build')
def build():
    """构建接口（SSE实时返回进度）"""
    # 获取前端参数
    current = request.args.get('current')
    target = request.args.get('target')
    if not current or not target:
        return jsonify({'success': False, 'message': "请选择当前版本和目标版本"}), 400
    
    # 生成任务ID（时间戳）
    task_id = f"task_{int(time.time())}"
    # 启动构建线程
    build_thread = threading.Thread(
        target=run_build_task,
        args=(task_id, current, target),
        daemon=True
    )
    build_thread.start()

    # SSE生成器：实时推送状态
    def sse_generator():
        last_percent = -1
        while True:
            if task_id not in build_status:
                time.sleep(0.3)
                continue
            
            status = build_status[task_id]
            # 状态变化或任务结束时推送
            if status["percent"] != last_percent or status["status"] in ["complete", "error"]:
                last_percent = status["percent"]
                yield f"data: {json.dumps(status)}\n\n"
            
            # 任务结束，关闭连接
            if status.get("complete"):
                yield "event: close\ndata: 任务结束\n\n"
                break
            time.sleep(1)

    # 返回SSE响应
    return Response(
        sse_generator(),
        mimetype='text/event-stream',
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )


@app.route('/download/<task_id>')
def download(task_id):
    global build_status
    # 配置：设置每次读取的块大小（10MB，平衡性能和内存占用）
    CHUNK_SIZE = 100 * 1024 * 1024  # 10MB
    # 安全目录：限制只能下载此目录内的文件
    SAFE_DIR = "/home/auto_packing_no_delete/image_tar"

    try:
        # 1. 检查任务状态
        if task_id not in build_status or build_status[task_id]['status'] != 'complete':
            msg = f"任务{task_id}不存在或未完成"
            write_log(f"下载失败：{msg}", "ERROR")
            return msg, 404

        status = build_status[task_id]
        package_path = status.get('package_path')
        package_name = status.get('package_name', f"upgrade_{task_id}.zip")

        # 2. 安全校验
        if not package_path:
            return "升级包路径未配置", 500
            
        # 转换为绝对路径并检查是否在安全目录内
        abs_path = os.path.abspath(package_path)
        if not abs_path.startswith(os.path.abspath(SAFE_DIR)):
            write_log(f"非法下载请求：{abs_path}", "ERROR")
            abort(403)  # 禁止访问目录外文件

        # 3. 文件存在性和权限检查
        if not os.path.exists(abs_path):
            return f"文件不存在：{package_name}", 404
            
        if not os.access(abs_path, os.R_OK):
            write_log(f"无读取权限：{abs_path}", "ERROR")
            return "服务器无权限读取文件", 500

        # 4. 获取文件大小（用于进度显示和续传）
        file_size = os.path.getsize(abs_path)

        # 5. 处理续传请求（支持断点续传）
        range_header = request.headers.get('Range', None)
        start = 0
        end = file_size - 1

        if range_header:
            # 解析Range请求头（格式：bytes=start-end）
            range_str = range_header.split('=')[1]
            if '-' in range_str:
                start_str, end_str = range_str.split('-')
                start = int(start_str) if start_str else 0
                end = int(end_str) if end_str else file_size - 1

            # 验证范围有效性
            if start < 0 or end >= file_size or start > end:
                return "无效的请求范围", 416  # 416 表示范围不合法

        # 6. 文件名编码（支持中文和特殊字符）
        encoded_name = quote(package_name, safe='')

        # 7. 构建响应头
        headers = {
            'Content-Type': 'application/zip',
            'Content-Disposition': f"attachment; filename=\"{encoded_name}\"; filename*=UTF-8''{encoded_name}",
            'Accept-Ranges': 'bytes',  # 声明支持断点续传
            'Content-Length': str(end - start + 1),  # 本次传输的大小
        }

        # 8. 处理部分内容响应（续传）
        status_code = 206 if range_header else 200
        if range_header:
            headers['Content-Range'] = f"bytes {start}-{end}/{file_size}"

        # 9. 流式传输文件（核心优化：避免一次性加载大文件到内存）
        def file_stream():
            with open(abs_path, 'rb') as f:
                f.seek(start)  # 定位到起始位置（支持续传）
                while True:
                    # 计算本次读取的实际块大小（最后一块可能小于CHUNK_SIZE）
                    read_size = min(CHUNK_SIZE, end - f.tell() + 1)
                    if read_size <= 0:
                        break
                    chunk = f.read(read_size)
                    if not chunk:
                        break
                    yield chunk

        write_log(f"开始下载任务{task_id}：{package_name}（大小：{file_size/1024/1024:.2f}MB）", "INFO")
        return Response(file_stream(), headers=headers, status=status_code)

    except Exception as e:
        error_msg = f"下载异常：{str(e)}"
        write_log(f"{error_msg}\n堆栈：{traceback.format_exc()}", "ERROR")
        return f"下载失败：{str(e)}", 500
    


# -------------------------- 启动服务 --------------------------
if __name__ == '__main__':
    write_log("="*50)
    write_log("DeepFlow升级包构建服务启动")
    write_log(f"服务地址：http://0.0.0.0:8000")
    write_log(f"基础目录：{BASE_DIR}")
    write_log("="*50)
    app.run(host='0.0.0.0', port=8000, debug=False)  # 生产环境关闭debug