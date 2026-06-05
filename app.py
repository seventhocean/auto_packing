# -*- coding: utf-8 -*-
import subprocess
import time
import os
import glob
import re
import json
import shutil
import uuid
from flask import Flask, request, Response, jsonify, send_file, abort, render_template # type: ignore
import traceback
from urllib.parse import quote, unquote
import yaml # type: ignore
import redis # type: ignore

# 初始化Flask应用
app = Flask(__name__, static_folder='static', static_url_path='/static')

# -------------------------- 基础配置（与项目结构对齐） --------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(BASE_DIR, 'bin')   # 脚本/可执行文件
IMAGE_TAR_ROOT = os.path.join(BASE_DIR, 'images')       # 镜像临时存储根目录（按任务分目录）
UPGRADE_PACKAGE_DIR = os.path.join(BASE_DIR, 'upgrade_packages')  # 最终升级包存储目录（TAR.GZ）
LATEST_LIST_DIR = os.path.join(BASE_DIR, 'latest_image_list')  # 镜像列表目录
PULL_SCRIPT_PATH = os.path.join(BIN_DIR, 'pull_save.sh') # 镜像拉取脚本
LOG_DIR = os.path.join(BASE_DIR, 'logs')        # 日志目录
OSS_FILE = os.path.join(BIN_DIR, 'ossutil')  # ossutil
OSS_CONFIG = os.path.join(BIN_DIR, '.ossutilconfig')
TRASH_DIR = os.path.join(BASE_DIR, '.trash') # 项目临时回收站
OSS_VERSIONS_TMP_FILE = os.path.join(TRASH_DIR, 'oss_patch_version.txt')
PATCH_LIST_SCRIPT_PATH = os.path.join(BIN_DIR, 'get_patch_image_tag_list.sh') # 生成patch_list.txt的脚本路径
UPGRADE_SCRIPT_PATH = os.path.join(BIN_DIR, "deepflow_patch_upgrade.sh")
APP_CONFIG_PATH = os.environ.get('APP_CONFIG_PATH', os.path.join(BASE_DIR, 'config.yaml'))

DEFAULT_CONFIG = {
    "redis": {
        "host": "redis.deepflow.svc.cluster.local",
        "port": 6379,
        "db": 0,
        "password": "",
        "key_prefix": "auto_packing",
        "success_ttl_seconds": 7200,
        "failure_ttl_seconds": 3600,
        "download_ttl_seconds": 90,
        "max_logs": 100,
        "max_concurrent_tasks": 3
    }
}

for dir_path in [IMAGE_TAR_ROOT, UPGRADE_PACKAGE_DIR, LATEST_LIST_DIR, LOG_DIR]:
    os.makedirs(dir_path, exist_ok=True)
       # write_log(f"自动创建目录：{dir_path}")

def load_app_config():
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(APP_CONFIG_PATH):
        with open(APP_CONFIG_PATH, 'r', encoding='utf-8') as f:
            loaded = yaml.safe_load(f) or {}
        if isinstance(loaded.get('redis'), dict):
            config['redis'].update(loaded['redis'])

    redis_password = os.environ.get('REDIS_PASSWORD')
    if redis_password is not None:
        config['redis']['password'] = redis_password

    config['redis']['port'] = int(config['redis']['port'])
    config['redis']['db'] = int(config['redis']['db'])
    config['redis']['success_ttl_seconds'] = int(config['redis']['success_ttl_seconds'])
    config['redis']['failure_ttl_seconds'] = int(config['redis']['failure_ttl_seconds'])
    config['redis']['download_ttl_seconds'] = int(config['redis']['download_ttl_seconds'])
    config['redis']['max_logs'] = int(config['redis']['max_logs'])
    config['redis']['max_concurrent_tasks'] = int(config['redis']['max_concurrent_tasks'])
    return config


APP_CONFIG = load_app_config()
REDIS_CONFIG = APP_CONFIG['redis']
redis_client = redis.Redis(
    host=REDIS_CONFIG['host'],
    port=REDIS_CONFIG['port'],
    db=REDIS_CONFIG['db'],
    password=REDIS_CONFIG['password'] or None,
    decode_responses=True,
    # Worker uses blocking Redis list operations for queue polling.
    # Keep socket timeout comfortably above the BRPOPLPUSH timeout to avoid
    # treating an idle queue as a network read timeout.
    socket_timeout=30,
    socket_connect_timeout=5,
    health_check_interval=15,
    retry_on_timeout=True
)
redis_client.ping()


def task_meta_key(task_id):
    return f"{REDIS_CONFIG['key_prefix']}:task:{task_id}:meta"


def task_logs_key(task_id):
    return f"{REDIS_CONFIG['key_prefix']}:task:{task_id}:logs"


def task_slot_key(task_id):
    return f"{REDIS_CONFIG['key_prefix']}:task:{task_id}:slot"


def active_builds_key():
    return f"{REDIS_CONFIG['key_prefix']}:builds:active"


def build_queue_key():
    return f"{REDIS_CONFIG['key_prefix']}:build:queue"


def build_processing_key(worker_id):
    return f"{REDIS_CONFIG['key_prefix']}:build:processing:{worker_id}"


def worker_heartbeat_key(worker_id):
    return f"{REDIS_CONFIG['key_prefix']}:worker:{worker_id}:heartbeat"


def get_task_status(task_id, include_logs=True):
    status_json = redis_client.get(task_meta_key(task_id))
    if not status_json:
        return None

    status = json.loads(status_json)
    if include_logs:
        logs = redis_client.lrange(task_logs_key(task_id), 0, -1)
        status['logs'] = [json.loads(item) for item in logs]
    return status


def set_task_status(task_id, status, ttl_seconds=None):
    status_to_store = dict(status)
    status_to_store.pop('logs', None)
    redis_client.set(task_meta_key(task_id), json.dumps(status_to_store, ensure_ascii=False))
    if ttl_seconds:
        redis_client.expire(task_meta_key(task_id), ttl_seconds)
        redis_client.expire(task_logs_key(task_id), ttl_seconds)


def update_task_status(task_id, updates, ttl_seconds=None):
    current_status = get_task_status(task_id, include_logs=False)
    if not current_status:
        return None

    current_status.update(updates)
    current_status['updated_at'] = int(time.time())
    set_task_status(task_id, current_status, ttl_seconds=ttl_seconds)
    return current_status


def append_task_log(task_id, timestamp, level, content):
    if not redis_client.exists(task_meta_key(task_id)):
        return

    log_entry = json.dumps({
        'timestamp': timestamp,
        'level': level,
        'content': content
    }, ensure_ascii=False)
    redis_client.rpush(task_logs_key(task_id), log_entry)
    redis_client.ltrim(task_logs_key(task_id), -REDIS_CONFIG['max_logs'], -1)


def initialize_task_status(task_id, message):
    now = int(time.time())
    set_task_status(task_id, {
        "status": "queued",
        "percent": 0,
        "message": message,
        "complete": False,
        "error": False,
        "created_at": now,
        "updated_at": now,
        "download_expire_at": None
    })
    redis_client.delete(task_logs_key(task_id))


def schedule_task_cleanup(task_id, delay_seconds):
    redis_client.expire(task_meta_key(task_id), delay_seconds)
    redis_client.expire(task_logs_key(task_id), delay_seconds)


def try_acquire_build_slot(task_id):
    current_builds = redis_client.incr(active_builds_key())
    if current_builds > REDIS_CONFIG['max_concurrent_tasks']:
        redis_client.decr(active_builds_key())
        return False

    redis_client.set(task_slot_key(task_id), "1")
    return True


def release_build_slot(task_id):
    if redis_client.delete(task_slot_key(task_id)):
        current_value = redis_client.decr(active_builds_key())
        if current_value < 0:
            redis_client.set(active_builds_key(), 0)


def reconcile_build_counter():
    """修复因 worker 崩溃导致的 builds:active 计数泄漏。
    扫描所有现有的 task slot key，用实际数量同步计数器。
    """
    slot_pattern = f"{REDIS_CONFIG['key_prefix']}:task:*:slot"
    actual_count = 0
    for _ in redis_client.scan_iter(match=slot_pattern):
        actual_count += 1
    redis_client.set(active_builds_key(), actual_count)
    write_log(f"builds:active 计数器已同步为 {actual_count}", level="INFO")


def enqueue_build_task(payload):
    redis_client.lpush(build_queue_key(), json.dumps(payload, ensure_ascii=False))


# -------------------------- 工具函数（文件列表处理） --------------------------
# ANSI 转义码过滤：nerdctl 输出含进度条颜色码，前端不需要
_ANSI_RE = re.compile(r'\x1B\[[0-9;]*[a-zA-Z]')
# pull_save.sh / get_patch_image_tag_list.sh 输出自带 [YYYY-MM-DD HH:MM:SS] 前缀，去掉避免双重时间戳
_SHELL_TS_RE = re.compile(r'^\[\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\]\s*')

def _strip_ansi(text):
    return _ANSI_RE.sub('', text)

def _clean_shell_line(line):
    """过滤 ANSI 码 + 去掉脚本自带的时间戳前缀"""
    line = _strip_ansi(line).strip()
    return _SHELL_TS_RE.sub('', line)

def _run_and_stream(task_id, cmd, task_log_path, log_prefix=""):
    """用 Popen 逐行读取子脚本输出，过滤 ANSI 码，实时写入 Redis 日志。

    策略：
    1. 关键事件（登录、开始/完成、错误）立即写入 Redis
    2. 所有输出完整写入本地文件（排查问题时查看）
    """
    import time as _time
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1  # 行缓冲
    )
    last_logged = ""

    for line in iter(proc.stdout.readline, ''):
        # 完整清理：去 ANSI 码 + 去脚本自带时间戳
        clean = _clean_shell_line(line)
        if not clean:
            continue

        # 所有输出完整写入本地文件
        with open(task_log_path, 'a', encoding='utf-8') as f:
            f.write(clean + '\n')

        lower = clean.lower()

        # 判断是否为关键事件（立即推送）
        is_key = any(kw in lower for kw in [
            '开始登录', '登录成功', 'login',
            '开始拉取镜像：', '镜像拉取成功：',
            '开始保存镜像', '镜像保存成功', '保存成功',
            '错误：', 'error:', 'warning:', '警告：',
            '所有镜像处理完成', '===== 所有镜像处理完成',
            'failed', 'fatal'
        ])

        if is_key and clean != last_logged:
            write_log(clean, task_id=task_id)
            last_logged = clean

    proc.wait()
    return proc.returncode


def write_log(content, level="INFO", task_id=None):
    """写日志（含时间戳，同时输出到文件和控制台）"""
    log_file = os.path.join(LOG_DIR, 'app.log')
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    log_line = f"[{timestamp}] [{level}] {content}\n"
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(log_line)
    print(log_line.strip())

    if task_id:
        append_task_log(task_id, timestamp, level, content)

def update_progress(task_id, new_percent, message, level="INFO"):
    """更新任务进度。直接跳到目标百分比，不做逐 1% 的中间写入。"""
    task_status = get_task_status(task_id, include_logs=False)
    if not task_status:
        return

    current_percent = task_status.get('percent', 0)
    if new_percent > current_percent:
        task_status.update({
            "status": "progress",
            "percent": new_percent,
            "message": message
        })
        set_task_status(task_id, task_status)

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
                display_version = re.sub(r'_x86_64$', '', version_with_arch)
                
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
        # 防御性处理：glob 和 getmtime 之间文件可能被删除
        def _safe_mtime(pkg):
            try:
                return os.path.getmtime(os.path.join(UPGRADE_PACKAGE_DIR, pkg["name"]))
            except FileNotFoundError:
                return 0
        packages.sort(key=_safe_mtime, reverse=True)
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
    try:
        # 创建任务专属目录（用版本+任务ID命名，确保唯一性，避免文件冲突）
        task_dir_name = f"{current_version}_to_{target_version}_{task_id}"
        task_image_dir = os.path.join(IMAGE_TAR_ROOT, task_dir_name)
        task_patch_list_path = os.path.join(task_image_dir, "patch_image_tag_list.txt")
        # 确保任务目录存在（不存在则创建，存在则清空历史残留）
        if os.path.exists(task_image_dir):
            shutil.rmtree(task_image_dir)
        os.makedirs(task_image_dir, exist_ok=True)

        # 为每个任务生成独立的镜像列表文件路径，避免并发竞争
        task_patch_list_path = os.path.join(LATEST_LIST_DIR, f"patch_image_tag_list_{task_id}.txt")

        # 创建任务专属日志文件（记录脚本详细输出，避免实时刷屏）
        task_log_path = os.path.join(LOG_DIR, f"task_{task_id}.log")

        try:
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

            # 在 app / worker 分离部署后，worker Pod 不能依赖 app Pod 本地生成的
            # .trash/oss_patch_version.txt。这里在 worker 执行任务前主动刷新一份
            # 当前 Pod 本地的 OSS 版本缓存，供 shell 脚本读取。
            update_progress(task_id, 12, "刷新OSS补丁版本缓存")
            oss_versions = get_oss_versions()
            if not oss_versions:
                raise Exception("OSS版本缓存刷新失败，无法生成镜像列表")
            write_log(f"OSS版本缓存刷新完成，共{len(oss_versions)}个版本", task_id=task_id)

            # 执行get_patch_image_tag_list.sh，生成镜像拉取清单
            update_progress(task_id, 15, f"执行镜像列表生成脚本，参数：{current_version} {target_version}")
            write_log(f"开始生成镜像列表，当前版本: {current_version}, 目标版本: {target_version}", task_id=task_id)
            
            # 调用Shell脚本，传递当前版本和目标版本参数
            # 通过环境变量传入任务专属的输出路径，避免并发竞争
            script_env = os.environ.copy()
            script_env["PATCH_IMAGE_TAG_LIST_FILE"] = task_patch_list_path
            with open(task_log_path, 'a', encoding='utf-8') as f:
                script_result = subprocess.run(
                    ["/bin/bash", PATCH_LIST_SCRIPT_PATH, current_version, target_version, task_patch_list_path],
                    check=True,
                    stdout=f,  # 捕获标准输出
                    stderr=subprocess.STDOUT, # 标准错误合并到标准输出
                    universal_newlines=True,
                    timeout=600  # git pull + nuwa grep, 10 分钟上限
                )
            
            update_progress(task_id, 25, "验证镜像列表生成结果")
            # 验证脚本执行结果：必须生成patch_image_tag_list.txt
            if not os.path.exists(task_patch_list_path):
                raise Exception(f"镜像列表脚本执行失败：未生成任务镜像列表文件 {task_patch_list_path}")
            
            # 读取并记录脚本输出日志
            with open(task_log_path, 'r', encoding='utf-8') as f:
                script_log = f.read()
            write_log(f"镜像列表生成成功，脚本输出已记录", task_id=task_id)
            
            update_progress(task_id, 30, "镜像拉取清单生成完成，准备拉取镜像文件")
            # 读取镜像列表文件，获取要拉取的镜像数量
            with open(task_patch_list_path, 'r', encoding='utf-8') as f:
                image_count = len([line for line in f if line.strip()])
            write_log(f"发现 {image_count} 个需要拉取的镜像", task_id=task_id)

            # 执行pull_save.sh，拉取镜像到任务专属目录
            update_progress(task_id, 35, f"开始拉取 {image_count} 个镜像文件")
            write_log(f"开始拉取镜像，存储路径：{task_image_dir}", task_id=task_id)

            # 调用拉取脚本，逐行流式输出到 Redis（带 ANSI 过滤）
            rc = _run_and_stream(
                task_id,
                ["/bin/bash", PULL_SCRIPT_PATH, "-d", task_image_dir, "-f", task_patch_list_path],
                task_log_path
            )
            if rc != 0:
                raise Exception(f"镜像拉取脚本执行失败，退出码 {rc}")

            # 验证镜像拉取结果：任务目录必须有.tar文件
            tar_files = glob.glob(os.path.join(task_image_dir, "*.tar"))
            if not tar_files:
                raise Exception(f"镜像拉取失败：任务目录{task_image_dir}无任何.tar镜像文件")
            
            update_progress(task_id, 65, f"镜像拉取完成，共拉取 {len(tar_files)} 个")
            write_log(f"镜像拉取成功！共拉取{len(tar_files)}个镜像", task_id=task_id)

            # 准备打包
            update_progress(task_id, 70, "准备打包升级包")

            if not os.path.exists(UPGRADE_SCRIPT_PATH) or not os.path.isfile(UPGRADE_SCRIPT_PATH):
                raise Exception(f"升级脚本不存在：{UPGRADE_SCRIPT_PATH}")

            # 复制镜像列表到任务目录的标准文件名，确保 upgrade 脚本能找到
            standard_list_path = os.path.join(task_image_dir, "patch_image_tag_list.txt")
            shutil.copy2(task_patch_list_path, standard_list_path)

            files_to_pack = tar_files + [standard_list_path, UPGRADE_SCRIPT_PATH]
            write_log(f"准备打包 {len(files_to_pack)} 个文件", task_id=task_id)

            # 打包TAR.GZ升级包（仅包含当前任务的镜像+镜像列表）
            update_progress(task_id, 75, "开始打包TAR.GZ升级包")
            current_patch_version = current_version.split('-')[0]
            target_patch_version = target_version.split('-')[0]
            upgrade_package = f"deepflow_patch_v669_{current_patch_version}_{target_patch_version}.tar.gz"
            upgrade_path = os.path.join(UPGRADE_PACKAGE_DIR, upgrade_package)

            # 包名去掉 .tar.gz 后缀作为解包后的目录名
            extract_dir = upgrade_package.rsplit('.tar.gz', 1)[0]

            # 执行TAR.GZ打包（两步 transform：先去路径前缀，再加目录前缀）
            with open(task_log_path, 'a', encoding='utf-8') as f:
                tar_result = subprocess.run(
                    [
                        "tar", "-czf", upgrade_path,
                        "--transform", r"s|.*/||",             # 第一步：去掉文件路径前缀
                        "--transform", f"s|.*|{extract_dir}/&|",  # 第二步：加上目录前缀
                        *files_to_pack
                    ],
                    check=True,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                    timeout=1800  # tar 大文件压缩，30 分钟上限
                )

            if not os.path.exists(upgrade_path) or os.path.getsize(upgrade_path) == 0:
                raise Exception(f"打包失败：升级包{upgrade_path}不存在或为空文件")
            
            update_progress(task_id, 90, "验证升级包完整性")
            # 验证包大小
            package_size = os.path.getsize(upgrade_path)
            write_log(f"升级包大小：{package_size/1024/1024:.2f}MB", task_id=task_id)

            # 清理临时文件
            update_progress(task_id, 95, "清理临时文件")
            write_log(f"临时文件清理完成", task_id=task_id)

            # 任务完成：更新状态并记录日志
            update_progress(task_id, 100, f"构建成功！生成TAR.GZ升级包（{len(tar_files)}个镜像+2个文件）")
            update_task_status(task_id, {
                "status": "complete",
                "percent": 100,
                "message": f"构建成功！生成TAR.GZ升级包（{len(tar_files)}个镜像+2个文件）",
                "complete": True,
                "error": False,
                "download_url": f"/download/{task_id}",
                "package_path": upgrade_path,
                "package_name": upgrade_package,
                "package_format": "tar.gz",  # 标记包格式，便于前端显示
                "package_size_mb": round(os.path.getsize(upgrade_path)/1024/1024, 2),  # 包大小（MB）
                "download_expire_at": int(time.time()) + REDIS_CONFIG['download_ttl_seconds']
            })
            write_log(f"任务[{task_id}]完全结束：{current_version}→{target_version}升级包构建完成，请在90秒内下载升级包", task_id=task_id)
            schedule_task_cleanup(task_id, REDIS_CONFIG['success_ttl_seconds'])

        except Exception as e:
            # 任务失败：捕获异常并更新状态
            error_msg = str(e)
            update_task_status(task_id, {
                "status": "error",
                "message": f"构建失败：{error_msg}",
                "complete": True,
                "error": True
            })
            # _run_and_stream 已经实时写入 Redis 日志，这里只写错误信息即可
            write_log(f"任务失败：{error_msg}", level="ERROR", task_id=task_id)
            # 打印异常堆栈，便于问题排查
            traceback.print_exc()
            schedule_task_cleanup(task_id, REDIS_CONFIG['failure_ttl_seconds'])

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
            
            # 清理任务专属镜像列表文件
            if os.path.exists(task_patch_list_path):
                try:
                    os.remove(task_patch_list_path)
                    write_log(f"任务专属镜像列表文件已清理：{task_patch_list_path}", task_id=task_id)
                except Exception as patch_e:
                    write_log(f"任务专属镜像列表清理失败：{str(patch_e)}", level="WARNING", task_id=task_id)

            # 清理任务日志文件
            if os.path.exists(task_log_path):
                try:
                    os.remove(task_log_path)
                    write_log(f"任务临时日志文件已清理", task_id=task_id)
                except Exception as log_e:
                    write_log(f"任务临时日志清理失败：{str(log_e)}", level="WARNING", task_id=task_id)
    finally:
        release_build_slot(task_id)

def run_build_task_v7(task_id, images):
    """V7构建任务：根据镜像列表生成patchlist→拉取镜像→打包TAR.GZ格式升级包"""
    try:
        task_dir_name = f"v7_{task_id}"
        task_image_dir = os.path.join(IMAGE_TAR_ROOT, task_dir_name)
        task_patch_list_path = os.path.join(task_image_dir, "patch_image_tag_list.txt")
        if os.path.exists(task_image_dir):
            shutil.rmtree(task_image_dir)
        os.makedirs(task_image_dir, exist_ok=True)

        task_log_path = os.path.join(LOG_DIR, f"task_{task_id}.log")

        try:
            write_log(f"任务[{task_id}]启动：V7镜像构建，数量={len(images)}", task_id=task_id)
            time.sleep(1)

            dependencies = [
                (PULL_SCRIPT_PATH, "镜像拉取的Shell脚本")
            ]

            update_progress(task_id, 5, "检查核心依赖脚本")
            for dep_path, dep_desc in dependencies:
                if not os.path.exists(dep_path):
                    raise Exception(f"核心依赖缺失：{dep_desc}路径不存在 → {dep_path}")
                write_log(f"验证依赖：{dep_desc}存在", task_id=task_id)
                time.sleep(0.5)

            update_progress(task_id, 12, "生成V7镜像列表文件")
            with open(task_patch_list_path, 'w', encoding='utf-8') as f:
                for image in images:
                    f.write(f"{image}\n")
            write_log(f"V7镜像列表写入完成：{task_patch_list_path}", task_id=task_id)

            update_progress(task_id, 25, "镜像拉取清单生成完成，准备拉取镜像文件")
            write_log(f"开始拉取镜像，存储路径：{task_image_dir}", task_id=task_id)

            # 调用拉取脚本，逐行流式输出到 Redis（带 ANSI 过滤）
            rc = _run_and_stream(
                task_id,
                ["/bin/bash", PULL_SCRIPT_PATH, "-d", task_image_dir, "-f", task_patch_list_path],
                task_log_path
            )
            if rc != 0:
                raise Exception(f"镜像拉取脚本执行失败，退出码 {rc}")

            tar_files = glob.glob(os.path.join(task_image_dir, "*.tar"))
            if not tar_files:
                raise Exception(f"镜像拉取失败：任务目录{task_image_dir}无任何.tar镜像文件")

            update_progress(task_id, 60, f"镜像拉取完成，共拉取 {len(tar_files)} 个")
            write_log(f"镜像拉取成功！共拉取{len(tar_files)}个镜像", task_id=task_id)

            update_progress(task_id, 70, "准备打包升级包")

            if not os.path.exists(UPGRADE_SCRIPT_PATH) or not os.path.isfile(UPGRADE_SCRIPT_PATH):
                raise Exception(f"升级脚本不存在：{UPGRADE_SCRIPT_PATH}")
            files_to_pack = tar_files + [task_patch_list_path, UPGRADE_SCRIPT_PATH]

            update_progress(task_id, 75, "开始打包TAR.GZ升级包")
            upgrade_package = f"deepflow_patch_v7_{task_id}.tar.gz"
            upgrade_path = os.path.join(UPGRADE_PACKAGE_DIR, upgrade_package)

            extract_dir = upgrade_package.rsplit('.tar.gz', 1)[0]

            with open(task_log_path, 'a', encoding='utf-8') as f:
                subprocess.run(
                    [
                        "tar", "-czf", upgrade_path,
                        "--transform", r"s|.*/||",
                        "--transform", f"s|.*|{extract_dir}/&|",
                        *files_to_pack
                    ],
                    check=True,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                    timeout=1800  # tar 大文件压缩，30 分钟上限
                )

            if not os.path.exists(upgrade_path) or os.path.getsize(upgrade_path) == 0:
                raise Exception(f"打包失败：升级包{upgrade_path}不存在或为空文件")

            update_progress(task_id, 90, "验证升级包完整性")
            package_size = os.path.getsize(upgrade_path)
            write_log(f"升级包大小：{package_size/1024/1024:.2f}MB", task_id=task_id)

            update_progress(task_id, 95, "清理临时文件")
            write_log(f"临时文件清理完成", task_id=task_id)

            update_progress(task_id, 100, f"构建成功！生成TAR.GZ升级包（{len(tar_files)}个镜像+2个文件）")
            update_task_status(task_id, {
                "status": "complete",
                "percent": 100,
                "message": f"构建成功！生成TAR.GZ升级包（{len(tar_files)}个镜像+2个文件）",
                "complete": True,
                "error": False,
                "download_url": f"/download/{task_id}",
                "package_path": upgrade_path,
                "package_name": upgrade_package,
                "package_format": "tar.gz",
                "package_size_mb": round(os.path.getsize(upgrade_path)/1024/1024, 2),
                "download_expire_at": int(time.time()) + REDIS_CONFIG['download_ttl_seconds']
            })
            write_log(f"任务[{task_id}]完全结束：V7升级包构建完成，请在90秒内下载升级包", task_id=task_id)

            schedule_task_cleanup(task_id, REDIS_CONFIG['success_ttl_seconds'])

        except Exception as e:
            error_msg = str(e)
            update_task_status(task_id, {
                "status": "error",
                "message": f"构建失败：{error_msg}",
                "complete": True,
                "error": True
            })
            # _run_and_stream 已经实时写入 Redis 日志，这里只写错误信息即可
            write_log(f"任务失败：{error_msg}", level="ERROR", task_id=task_id)
            traceback.print_exc()

            schedule_task_cleanup(task_id, REDIS_CONFIG['failure_ttl_seconds'])

        finally:
            if os.path.exists(task_image_dir):
                try:
                    shutil.rmtree(task_image_dir)
                    write_log(f"任务临时目录清理完成：{task_image_dir}", task_id=task_id)
                except Exception as clean_e:
                    clean_error_msg = f"任务临时目录清理失败：{str(clean_e)}"
                    write_log(clean_error_msg, level="WARNING", task_id=task_id)

            if os.path.exists(task_log_path):
                try:
                    os.remove(task_log_path)
                    write_log(f"任务临时日志文件已清理", task_id=task_id)
                except Exception as log_e:
                    write_log(f"任务临时日志清理失败：{str(log_e)}", level="WARNING", task_id=task_id)
    finally:
        release_build_slot(task_id)


# -------------------------- Flask路由 --------------------------
@app.route('/')
def index():
    """前端页面入口"""
    return render_template('index.html')

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
    series = request.args.get('series', 'v6')
    if series != 'v6':
        return jsonify({
            "success": False,
            "versions": [],
            "message": f"{series} 版本列表尚未接入（占位逻辑）"
        }), 501

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
    task_status = get_task_status(task_id)
    if task_status:
        return jsonify({
            "success": True,
            "status": task_status
        })
    else:
        return jsonify({
            "success": False,
            "message": "任务不存在或已过期"
        }), 404


# -------------------------- 构建任务（兼容前端SSE） --------------------------
@app.route('/build', methods=['POST'])
def api_build_post():
    """V7 专用：通过 POST JSON 提交镜像列表，避免 URL 超长。返回 task_id 供前端 SSE 连接。"""
    data = request.get_json(silent=True) or {}
    series = data.get('series', 'v7')
    if series == 'v7':
        images = data.get('images', [])
        if not isinstance(images, list) or not images:
            return jsonify({"success": False, "message": "缺少必要参数：images 不能为空"}), 400
        images = [img.strip() for img in images if isinstance(img, str) and img.strip()]
        if not images:
            return jsonify({"success": False, "message": "镜像列表为空，请输入有效镜像名称"}), 400
        timestamp = int(time.time() * 1000)
        task_id = f"build_v7_{timestamp}_{uuid.uuid4().hex[:8]}"
        try:
            initialize_task_status(task_id, "任务已创建，等待 worker 调度")
            enqueue_build_task({"task_id": task_id, "task_type": "v7", "images": images})
        except Exception as e:
            update_task_status(task_id, {
                "status": "error", "message": f"任务入队失败：{str(e)}",
                "complete": True, "error": True
            }, ttl_seconds=REDIS_CONFIG['failure_ttl_seconds'])
            return jsonify({"success": False, "message": f"任务入队失败：{str(e)}"}), 500
        return jsonify({"success": True, "task_id": task_id})
    else:
        return jsonify({"success": False, "message": f"不支持的 series: {series}"}), 400


@app.route('/build', methods=['GET'])
def api_build():
    """构建入口：V6 用 GET 参数入队；通过 task_id 参数可仅观察已有任务。返回 SSE 流。"""
    task_id = request.args.get('task_id')
    # 仅观察已有任务（不创建新任务）
    if task_id:
        def watch_sse():
            try:
                while True:
                    task_status = get_task_status(task_id)
                    if not task_status:
                        retry = 0
                        while retry < 3:
                            task_status = get_task_status(task_id)
                            if task_status:
                                break
                            time.sleep(0.5)
                            retry += 1
                        if not task_status:
                            yield f"data: {json.dumps({'status': 'error', 'percent': 0, 'message': '任务状态已过期', 'logs': []})}\n\n"
                            break
                    yield f"data: {json.dumps(task_status)}\n\n"
                    if task_status.get('complete', False):
                        break
                    time.sleep(1)
            except GeneratorExit:
                pass
            except Exception as e:
                write_log(f"SSE 推送异常 (task={task_id}): {str(e)}", level="ERROR")
                yield f"data: {json.dumps({'status': 'error', 'percent': 0, 'message': f'推送异常: {str(e)}', 'logs': []})}\n\n"
        return Response(watch_sse(), mimetype='text/event-stream')

    # 旧版 GET 入队流程（V6 + 兼容旧的 V7 GET 调用）
    series = request.args.get('series', 'v6')
    current_version = request.args.get('current')
    target_version = request.args.get('target')
    v7_images = request.args.get('images', '')

    if series == 'v7':
        if not v7_images.strip():
            return jsonify({"success": False, "message": "缺少必要参数：images不能为空"}), 400
        images = [img.strip() for img in v7_images.split(',') if img.strip()]
        if not images:
            return jsonify({"success": False, "message": "镜像列表为空，请输入有效镜像名称"}), 400
        timestamp = int(time.time() * 1000)
        task_id = f"build_v7_{timestamp}_{uuid.uuid4().hex[:8]}"
        try:
            initialize_task_status(task_id, "任务已创建，等待 worker 调度")
            enqueue_build_task({
                "task_id": task_id,
                "task_type": "v7",
                "images": images
            })
        except Exception as e:
            update_task_status(task_id, {
                "status": "error",
                "message": f"任务入队失败：{str(e)}",
                "complete": True,
                "error": True
            }, ttl_seconds=REDIS_CONFIG['failure_ttl_seconds'])
            raise
    else:
        # 参数校验
        if not current_version or not target_version:
            return jsonify({"success": False, "message": "缺少必要参数：current或target版本不能为空"}), 400
        if current_version == target_version:
            return jsonify({"success": False, "message": "当前版本与目标版本不能相同"}), 400

        # 生成唯一任务ID
        timestamp = int(time.time() * 1000)
        current_abbr = current_version[:8] if len(current_version)>=8 else current_version
        target_abbr = target_version[:8] if len(target_version)>=8 else target_version
        task_id = f"build_{current_abbr}_to_{target_abbr}_{timestamp}_{uuid.uuid4().hex[:8]}"
        try:
            initialize_task_status(task_id, "任务已创建，等待 worker 调度")
            enqueue_build_task({
                "task_id": task_id,
                "task_type": "v6",
                "current_version": current_version,
                "target_version": target_version
            })
        except Exception as e:
            update_task_status(task_id, {
                "status": "error",
                "message": f"任务入队失败：{str(e)}",
                "complete": True,
                "error": True
            }, ttl_seconds=REDIS_CONFIG['failure_ttl_seconds'])
            raise

    # 建立SSE连接，实时推送进度和日志
    def generate_sse():
        try:
            # 初始状态推送
            retry_count = 0
            initial_status = None
            while retry_count < 2 and not initial_status:
                initial_status = get_task_status(task_id) or {
                    'status': 'progress',
                    'percent': 0,
                    'message': '任务初始化中...',
                    'logs': []
                }
                if not initial_status:
                    time.sleep(0.5)
                    retry_count += 1
            yield f"data: {json.dumps(initial_status)}\n\n"

            # 循环推送状态
            while True:
                task_status = get_task_status(task_id)
                if not task_status:
                    # 重试3次，若仍不存在则判定过期
                    retry = 0
                    while retry < 3:
                        task_status = get_task_status(task_id)
                        if task_status:
                            break
                        time.sleep(0.5)
                        retry += 1
                    if not task_status:
                        yield f"data: {json.dumps({'status': 'error', 'percent': 0, 'message': '任务状态已过期，请刷新页面重试', 'logs': []})}\n\n"
                        break

                yield f"data: {json.dumps(task_status)}\n\n"

                # 任务完成，终止循环
                if task_status.get('complete', False):
                    break

                # 控制推送频率
                time.sleep(1)
        except GeneratorExit:
            pass
        except Exception as e:
            write_log(f"SSE 推送异常 (task={task_id}): {str(e)}", level="ERROR")
            yield f"data: {json.dumps({'status': 'error', 'percent': 0, 'message': f'服务端推送异常: {str(e)}', 'logs': []})}\n\n"

    # 设置SSE响应头
    return Response(generate_sse(), mimetype='text/event-stream')


# -------------------------- 下载新构建的包（兼容） --------------------------
@app.route('/download/<task_id>', methods=['GET'])
def api_download(task_id):
    """原有接口：下载新构建的升级包（通过任务ID定位）"""
    task_status = get_task_status(task_id, include_logs=False)
    if not task_status:
        error_msg = f"任务{task_id}不存在或已过期（请重新构建）"
        write_log(error_msg, level="WARNING")
        return jsonify({"success": False, "message": error_msg}), 404

    if task_status.get('status') != 'complete' or not task_status.get('package_path'):
        error_msg = f"任务{task_id}未构建完成，无法下载"
        write_log(error_msg, level="WARNING")
        return jsonify({"success": False, "message": error_msg}), 400

    download_expire_at = task_status.get('download_expire_at')
    if download_expire_at and time.time() > float(download_expire_at):
        error_msg = f"任务{task_id}的下载链接已过期，请重新构建或从已有升级包列表下载"
        write_log(error_msg, level="WARNING")
        return jsonify({"success": False, "message": error_msg}), 410

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
    write_log(
        f"服务配置：升级包存储目录={UPGRADE_PACKAGE_DIR}，日志目录={LOG_DIR}，Redis={REDIS_CONFIG['host']}:{REDIS_CONFIG['port']}/{REDIS_CONFIG['db']}"
    )
    app.run(host='0.0.0.0', port=8000, threaded=True, debug=True)
