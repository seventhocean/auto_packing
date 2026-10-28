# -*- coding: utf-8 -*-
import subprocess
import time
import os
import glob
import re
import json
import shutil
import signal
import threading
import uuid
import calendar
from flask import Flask, request, Response, jsonify, send_file, abort, render_template, redirect # type: ignore
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
PATCH_LIST_SCRIPT_PATH = os.path.join(BIN_DIR, 'get_patch_image_tag_list.sh') # 生成patch_list.txt的脚本路径
UPGRADE_SCRIPT_PATH = os.path.join(BIN_DIR, "deepflow_patch_upgrade.sh")
APP_CONFIG_PATH = os.environ.get('APP_CONFIG_PATH', os.path.join(BASE_DIR, 'config.yaml'))

# -------------------------- 发布线（channel） --------------------------
# 一条 channel = 「大版本 + 子版本」的一条发布序列，同时决定四件事：
#   1) OSS 上补丁包的前缀   2) nuwa 里 make.sh 的目录
#   3) grep 镜像用的 tag 前缀   4) 升级包命名
# 新增版本线只需在 _CHANNEL_SEED 里加一行，构建流程无需改动。
_CHANNEL_SEED = [
    ("6.4.9", "6.4", "6.4.9"),
    ("6.5.9", "6.5", "6.5.9"),
    ("6.6.9", "6.6", "6.6.9"),
    ("7.0.9", "7.0", "7.0.9"),
    ("7.1.9", "7.1", "7.1.9"),
]
DEFAULT_CHANNEL = "6.6.9"

CHANNELS = {}
for _key, _series, _sub in _CHANNEL_SEED:
    # 包名里的版本标识：6.6.9 -> 669，7.1.9 -> 719。
    # 6.6.9 必须保持 "669"，否则续不上 upgrade_packages/ 里已有的历史包。
    _code = _sub.replace('.', '')
    CHANNELS[_key] = {
        "key": _key,
        "series": _series,
        "sub": _sub,
        "tag_prefix": "v{}".format(_series),
        "oss_prefix": "oss://df-patch-no-delete/patch/{}/{}/".format(_series, _sub),
        "nuwa_dir": "nuwa/{}/{}".format(_series, _sub),
        "pkg_code": _code,
        "package_template": "deepflow_patch_v{}_{{current}}_{{target}}.tar.gz".format(_code),
    }
del _key, _series, _sub, _code

# 旧代码/旧接口遗留：默认版本线的包名模板
PACKAGE_NAME_TEMPLATE = CHANNELS[DEFAULT_CHANNEL]["package_template"]
# 默认版本线的 OSS 版本缓存文件（脚本单独运行时仍读这个路径）
OSS_VERSIONS_TMP_FILE = os.path.join(TRASH_DIR, 'oss_patch_version.txt')


def resolve_channel(channel_key):
    """把入参归一化为合法的 channel key；未知版本线返回 None。"""
    if not channel_key:
        return DEFAULT_CHANNEL
    key = str(channel_key).strip()
    # 兼容旧前端/旧接口只传 series 的写法
    if key in ('v6', 'v6.6', '6.6', '669', 'v669'):
        return DEFAULT_CHANNEL
    if key in CHANNELS:
        return key
    # 允许只传大版本号（如 7.1），只要它能唯一匹配到一条发布线
    matched = [k for k, ch in CHANNELS.items() if ch["series"] == key]
    return matched[0] if len(matched) == 1 else None


def _oss_versions_tmp_path(channel_key):
    """每个版本线独立的 OSS 版本缓存文件，避免并发构建互相覆盖。"""
    return os.path.join(TRASH_DIR, 'oss_patch_version_{}.txt'.format(CHANNELS[channel_key]["pkg_code"]))

# 构建超时配置（秒）。拉镜像是最容易卡住的步骤，必须有上限，否则任务会永久占用槽位。
PULL_TIMEOUT_SECONDS = int(os.environ.get('PULL_TIMEOUT_SECONDS', 2 * 3600))
ARCHIVE_TIMEOUT_SECONDS = int(os.environ.get('ARCHIVE_TIMEOUT_SECONDS', 1800))
OSS_QUERY_TIMEOUT_SECONDS = int(os.environ.get('OSS_QUERY_TIMEOUT_SECONDS', 120))
# 槽位锁的兜底 TTL：即使 worker 异常退出也不会永久占住并发额度
SLOT_TTL_SECONDS = int(os.environ.get('SLOT_TTL_SECONDS', 6 * 3600))
# 版本号会参与目录名/包名拼接，必须限制字符集，防止路径穿越
_VERSION_VALUE_RE = re.compile(r'^[0-9A-Za-z][0-9A-Za-z._-]*$')

# /versions 页面加载允许使用短 TTL 缓存；构建任务仍强制刷新，保证镜像清单最新
OSS_VERSIONS_CACHE_TTL_SECONDS = int(os.environ.get('OSS_VERSIONS_CACHE_TTL_SECONDS', 300))
# 按版本线缓存：{channel_key: {"versions": [...], "expires_at": float}}
_OSS_VERSIONS_CACHE = {}


class BuildCancelled(Exception):
    """用户主动取消构建。"""

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
        "max_logs": 2000,
        "max_concurrent_tasks": 1
    },
    # 升级包保留策略：只增不删会把盘吃满，每次构建成功后按这个策略回收。
    # 默认保守（数量/容量都留足余量），可用 config.yaml 的 package_retention 覆盖。
    "retention": {
        "enabled": True,
        "max_count": 40,        # 最多保留多少个包
        "max_total_gb": 60,     # 所有包总大小上限
        "min_keep": 3,          # 无论超限与否，至少保留最近的 N 个
        "min_age_minutes": 30,  # 这么新的包一律不删（防止删掉正在下载/刚交付的包）
        "dry_run": False
    },
    # 升级包存储后端：local=只落本地盘；oss=构建成功后上传 OSS，下载走签名 URL。
    # 换成 oss 后 app 不再强依赖本机磁盘，是多副本/多节点的前置条件。
    "package_storage": {
        "backend": "local",
        "oss_bucket": "publicshare-unsafe",
        "oss_prefix": "tmp/auto_packing/packages/",
        "presign_seconds": 900,          # 签名 URL 有效期
        "oss_timeout_seconds": 120,      # 列举/签名超时
        "upload_timeout_seconds": 3600,  # 上传超时（大包可能几 GB）
        # 上传成功后删除本地副本：worker 用 emptyDir 运行时必须开，
        # 否则单个 Pod 长期存活会累积几十 GB；下载会自动走 OSS 签名 URL
        "delete_local_after_upload": False
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
        if isinstance(loaded.get('package_retention'), dict):
            config['retention'].update(loaded['package_retention'])
        if isinstance(loaded.get('package_storage'), dict):
            config['package_storage'].update(loaded['package_storage'])

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

    retention = config['retention']
    retention['enabled'] = bool(retention['enabled'])
    retention['dry_run'] = bool(retention['dry_run'])
    retention['max_count'] = int(retention['max_count'])
    retention['max_total_gb'] = float(retention['max_total_gb'])
    retention['min_keep'] = int(retention['min_keep'])
    retention['min_age_minutes'] = int(retention['min_age_minutes'])

    storage = config['package_storage']
    storage['backend'] = str(storage['backend']).strip().lower()
    if storage['backend'] not in ('local', 'oss'):
        storage['backend'] = 'local'
    storage['oss_bucket'] = str(storage['oss_bucket']).strip()
    storage['oss_prefix'] = str(storage['oss_prefix']).strip()
    storage['presign_seconds'] = int(storage['presign_seconds'])
    storage['oss_timeout_seconds'] = int(storage['oss_timeout_seconds'])
    storage['upload_timeout_seconds'] = int(storage['upload_timeout_seconds'])
    storage['delete_local_after_upload'] = bool(storage.get('delete_local_after_upload', False))
    return config


APP_CONFIG = load_app_config()
REDIS_CONFIG = APP_CONFIG['redis']
RETENTION_CONFIG = APP_CONFIG['retention']
STORAGE_CONFIG = APP_CONFIG['package_storage']
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


def task_log_seq_key(task_id):
    return f"{REDIS_CONFIG['key_prefix']}:task:{task_id}:log_seq"


def task_slot_key(task_id):
    return f"{REDIS_CONFIG['key_prefix']}:task:{task_id}:slot"


def task_cancel_key(task_id):
    return f"{REDIS_CONFIG['key_prefix']}:task:{task_id}:cancel"


def active_builds_key():
    return f"{REDIS_CONFIG['key_prefix']}:builds:active"


def build_queue_key():
    return f"{REDIS_CONFIG['key_prefix']}:build:queue"


def build_processing_key(worker_id):
    return f"{REDIS_CONFIG['key_prefix']}:build:processing:{worker_id}"


def worker_heartbeat_key(worker_id):
    return f"{REDIS_CONFIG['key_prefix']}:worker:{worker_id}:heartbeat"


def get_task_status(task_id, include_logs=True, since_seq=0):
    status_json = redis_client.get(task_meta_key(task_id))
    if not status_json:
        return None

    status = json.loads(status_json)
    if include_logs:
        logs = redis_client.lrange(task_logs_key(task_id), 0, -1)
        entries = [json.loads(item) for item in logs]
        # 只返回游标之后的新日志，避免 SSE 每秒重复推送全量日志
        if since_seq:
            entries = [entry for entry in entries if entry.get('seq', 0) > since_seq]
        status['logs'] = entries

        last_seq = redis_client.get(task_log_seq_key(task_id))
        status['log_seq'] = int(last_seq) if last_seq else (entries[-1].get('seq', 0) if entries else 0)
    return status


def _parse_since_seq():
    """解析 SSE/状态接口的日志游标，非法值按 0 处理。"""
    try:
        return max(0, int(request.args.get('since', 0)))
    except (TypeError, ValueError):
        return 0


def _advance_log_cursor(cursor, task_status):
    try:
        return max(cursor, int(task_status.get('log_seq') or 0))
    except (TypeError, ValueError):
        return cursor


def is_build_cancelled(task_id):
    """取消标记是尽力而为的：Redis 抖动时不应让构建流程崩溃。"""
    try:
        return bool(redis_client.exists(task_cancel_key(task_id)))
    except Exception:
        return False


def _raise_if_cancelled(task_id):
    if is_build_cancelled(task_id):
        raise BuildCancelled("构建已被用户取消")


def set_task_status(task_id, status, ttl_seconds=None):
    status_to_store = dict(status)
    status_to_store.pop('logs', None)
    redis_client.set(task_meta_key(task_id), json.dumps(status_to_store, ensure_ascii=False))
    if ttl_seconds:
        redis_client.expire(task_meta_key(task_id), ttl_seconds)
        redis_client.expire(task_logs_key(task_id), ttl_seconds)
        redis_client.expire(task_log_seq_key(task_id), ttl_seconds)


def update_task_status(task_id, updates, ttl_seconds=None):
    current_status = get_task_status(task_id, include_logs=False)
    if not current_status:
        return None

    current_status.update(updates)
    current_status['updated_at'] = int(time.time())
    set_task_status(task_id, current_status, ttl_seconds=ttl_seconds)
    return current_status


def mark_task_cancelled(task_id):
    return update_task_status(task_id, {
        "status": "error",
        "message": "构建已取消",
        "complete": True,
        "error": True,
        "cancelled": True
    }, ttl_seconds=REDIS_CONFIG['failure_ttl_seconds'])


def append_task_log(task_id, timestamp, level, content):
    if not redis_client.exists(task_meta_key(task_id)):
        return

    # seq 单调递增：日志被 ltrim 后，客户端仍可用它做稳定的增量游标
    seq = redis_client.incr(task_log_seq_key(task_id))
    log_entry = json.dumps({
        'seq': seq,
        'timestamp': timestamp,
        'level': level,
        'content': content
    }, ensure_ascii=False)
    redis_client.rpush(task_logs_key(task_id), log_entry)
    redis_client.ltrim(task_logs_key(task_id), -REDIS_CONFIG['max_logs'], -1)


def initialize_task_status(task_id, message, task_type=None, extra=None):
    now = int(time.time())
    status = {
        "task_id": task_id,
        "status": "queued",
        "percent": 0,
        "message": message,
        "complete": False,
        "error": False,
        "created_at": now,
        "updated_at": now,
        "download_expire_at": None
    }
    if task_type:
        status["task_type"] = task_type
    if isinstance(extra, dict):
        status.update(extra)
    set_task_status(task_id, status)
    redis_client.delete(task_logs_key(task_id))
    redis_client.delete(task_log_seq_key(task_id))
    redis_client.delete(task_cancel_key(task_id))


def schedule_task_cleanup(task_id, delay_seconds):
    redis_client.expire(task_meta_key(task_id), delay_seconds)
    redis_client.expire(task_logs_key(task_id), delay_seconds)
    redis_client.expire(task_log_seq_key(task_id), delay_seconds)


def _count_active_slots():
    """统计当前真实持有的构建槽位数量。以 slot key 为准，避免计数漂移。"""
    slot_pattern = f"{REDIS_CONFIG['key_prefix']}:task:*:slot"
    return sum(1 for _ in redis_client.scan_iter(match=slot_pattern))


def try_acquire_build_slot(task_id):
    """抢占构建槽位。

    采用「先写 task slot，再统计 slot 总数」的方式，slot key 本身就是锁：
    - 同一任务重复获取（如失活回收后重跑）直接视为已持有，避免重复计数；
    - 不再依赖独立的 builds:active 做准入判断，异常退出不会造成永久泄漏；
    - slot 带 TTL 兜底，极端情况下也能自动释放。
    """
    slot_key = task_slot_key(task_id)
    if not redis_client.set(slot_key, "1", nx=True, ex=SLOT_TTL_SECONDS):
        return True

    active_count = _count_active_slots()
    if active_count > REDIS_CONFIG['max_concurrent_tasks']:
        redis_client.delete(slot_key)
        return False

    # 仅用于观测/展示，不再作为准入依据
    redis_client.set(active_builds_key(), active_count)
    return True


def release_build_slot(task_id):
    if redis_client.delete(task_slot_key(task_id)):
        redis_client.set(active_builds_key(), _count_active_slots())


def reconcile_build_counter():
    """用实际存在的 slot key 同步 builds:active 观测值。

    准入已经改为基于 slot key，本函数只负责让 builds:active 尽量准确。
    """
    actual_count = _count_active_slots()
    redis_client.set(active_builds_key(), actual_count)
    write_log(f"builds:active 计数器已同步为 {actual_count}", level="INFO")


def enqueue_build_task(payload):
    redis_client.lpush(build_queue_key(), json.dumps(payload, ensure_ascii=False))


def _validate_v6_versions(current_version, target_version):
    """校验 V6 版本参数，返回错误信息；通过时返回 None。"""
    if not current_version or not target_version:
        return "缺少必要参数：current或target版本不能为空"
    # 版本号会参与目录/包名拼接，必须限制字符集（防路径穿越）
    if not _is_safe_version_value(current_version) or not _is_safe_version_value(target_version):
        return "版本参数包含非法字符"
    if current_version == target_version:
        return "当前版本与目标版本不能相同"
    return None


def _create_build_task(task_id, task_type, payload, extra=None):
    """初始化任务元数据并入队；入队失败时尽力标记任务错误。"""
    initialize_task_status(task_id, "任务已创建，等待 worker 调度", task_type=task_type, extra=extra)
    try:
        enqueue_build_task(payload)
    except Exception as e:
        update_task_status(task_id, {
            "status": "error",
            "message": f"任务入队失败：{str(e)}",
            "complete": True,
            "error": True
        }, ttl_seconds=REDIS_CONFIG['failure_ttl_seconds'])
        raise
    return task_id


def _create_v6_build_task(current_version, target_version, channel_key=DEFAULT_CHANNEL):
    """固定版本（按 patch 差异）构建任务。task_type 仍写 v6，保证旧前端/旧任务兼容。"""
    channel = CHANNELS[channel_key]
    timestamp = int(time.time() * 1000)
    current_abbr = current_version[:8] if len(current_version) >= 8 else current_version
    target_abbr = target_version[:8] if len(target_version) >= 8 else target_version
    task_id = f"build_{channel['pkg_code']}_{current_abbr}_to_{target_abbr}_{timestamp}_{uuid.uuid4().hex[:8]}"
    return _create_build_task(task_id, "v6", {
        "task_id": task_id,
        "task_type": "v6",
        "mode": "patch",
        "channel": channel_key,
        "current_version": current_version,
        "target_version": target_version
    }, extra={
        "mode": "patch",
        "channel": channel_key,
        "current_version": current_version,
        "target_version": target_version
    })


def _create_v7_build_task(images):
    """自定义镜像清单构建任务。"""
    timestamp = int(time.time() * 1000)
    task_id = f"build_custom_{timestamp}_{uuid.uuid4().hex[:8]}"
    return _create_build_task(task_id, "v7", {
        "task_id": task_id,
        "task_type": "v7",
        "mode": "custom",
        "images": images
    }, extra={
        "mode": "custom",
        "image_count": len(images),
        # 存一份原始清单，任务结束后"重试"才能回填（overview 不返回该字段，避免轮询变重）
        "images": images
    })


def _is_worker_online(max_age_seconds=30):
    """通过 worker 心跳判断是否有可用的构建进程。"""
    now = int(time.time())
    try:
        for key in redis_client.scan_iter(match=worker_heartbeat_key('*')):
            raw = redis_client.get(key)
            try:
                last_seen = int(raw)
            except (TypeError, ValueError):
                continue
            if now - last_seen <= max_age_seconds:
                return True
    except Exception:
        return False
    return False


def get_tasks_overview(recent_limit=5):
    """汇总进行中/最近完成的任务，供任务中心展示。只读取元数据，不读日志。"""
    active, recent = [], []
    prefix = f"{REDIS_CONFIG['key_prefix']}:task:"
    try:
        keys = list(redis_client.scan_iter(match=f"{prefix}*:meta"))
    except Exception:
        keys = []

    for key in keys:
        raw = redis_client.get(key)
        if not raw:
            continue
        try:
            status = json.loads(raw)
        except (TypeError, ValueError):
            continue
        task_id = status.get("task_id")
        if not task_id:
            continue
        item = {
            "task_id": task_id,
            "task_type": status.get("task_type"),
            "mode": status.get("mode"),
            "channel": status.get("channel"),
            "current_version": status.get("current_version"),
            "target_version": status.get("target_version"),
            "image_count": status.get("image_count"),
            "status": status.get("status"),
            "percent": status.get("percent", 0),
            "message": status.get("message", ""),
            "created_at": status.get("created_at"),
            "updated_at": status.get("updated_at"),
            "complete": bool(status.get("complete")),
            "error": bool(status.get("error")),
            "cancelled": bool(status.get("cancelled")),
            "package_name": status.get("package_name"),
            "download_url": status.get("download_url"),
        }
        if item["complete"]:
            recent.append(item)
        else:
            active.append(item)

    active.sort(key=lambda x: x.get("created_at") or 0)
    recent.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    return active, recent[:recent_limit]


def get_system_status():
    """单节点运行状态：worker 心跳、队列、槽位和升级包磁盘余量。"""
    try:
        queue_length = redis_client.llen(build_queue_key())
    except Exception:
        queue_length = 0
    try:
        disk_free, disk_total = 0, 0
        usage = shutil.disk_usage(UPGRADE_PACKAGE_DIR)
        disk_free, disk_total = usage.free, usage.total
    except Exception:
        disk_free, disk_total = 0, 0
    return {
        "worker_online": _is_worker_online(),
        "queue_length": queue_length,
        "active_slots": _count_active_slots(),
        "max_concurrent_tasks": REDIS_CONFIG['max_concurrent_tasks'],
        "disk_free": disk_free,
        "disk_total": disk_total,
        "server_time": int(time.time()),
    }


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

def _run_and_stream(task_id, cmd, task_log_path, log_prefix="", timeout=PULL_TIMEOUT_SECONDS):
    """用 Popen 逐行读取子脚本输出，过滤 ANSI 码，实时写入 Redis 日志。

    策略：
    1. 所有输出写入 Redis 日志（按 max_logs 截断），前端可看到完整拉取过程
    2. 所有输出完整写入本地文件（排查问题时查看）
    3. 超时后强杀子进程，避免镜像拉取卡死导致任务永久占用槽位
    4. 每秒检查取消标记，用户取消后终止整个进程组
    """
    _raise_if_cancelled(task_id)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,  # 行缓冲
        start_new_session=True  # 独立进程组，超时可连同子进程一起终止
    )
    timed_out = threading.Event()
    cancelled = threading.Event()
    finished = threading.Event()
    cancel_stop = threading.Event()

    def _kill_group():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass

    def _kill_on_timeout():
        if finished.is_set():
            return
        # 只有主进程仍在运行时才判定超时，避免竞态误报
        if proc.poll() is None:
            timed_out.set()
        # 杀掉整个进程组：只杀 bash 的话，nerdctl 等子进程可能继续持有 stdout，
        # 导致 readline 永远等不到 EOF，超时逻辑失效。
        _kill_group()

    def _watch_cancel():
        while not cancel_stop.wait(1):
            if is_build_cancelled(task_id):
                cancelled.set()
                _kill_group()
                return

    timer = threading.Timer(timeout, _kill_on_timeout)
    timer.daemon = True
    timer.start()
    threading.Thread(target=_watch_cancel, daemon=True).start()

    try:
        for line in iter(proc.stdout.readline, ''):
            # 完整清理：去 ANSI 码 + 去脚本自带时间戳
            clean = _clean_shell_line(line)
            if not clean:
                continue

            # 所有输出完整写入本地文件
            with open(task_log_path, 'a', encoding='utf-8') as f:
                f.write(clean + '\n')

            lower = clean.lower()

            # 全部输出都写入 Redis 日志（最多保留 max_logs 条），便于前端排查问题
            level = "ERROR" if ('error' in lower or 'failed' in lower or '错误' in lower) else "INFO"
            append_task_log(
                task_id,
                time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
                level,
                clean
            )
    finally:
        finished.set()
        cancel_stop.set()
        timer.cancel()
        try:
            proc.stdout.close()
        except OSError:
            pass
        proc.wait()

    if cancelled.is_set():
        error_msg = "构建已被用户取消"
        write_log(error_msg, level="WARNING", task_id=task_id)
        raise BuildCancelled(error_msg)

    if timed_out.is_set():
        error_msg = f"命令执行超时（{timeout}秒），已强制终止：{' '.join(cmd)}"
        write_log(error_msg, level="ERROR", task_id=task_id)
        raise TimeoutError(error_msg)

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

def _run_oss_ls(channel_key=DEFAULT_CHANNEL):
    """执行 ossutil ls，网络抖动时重试 3 次。按版本线取对应 OSS 前缀。"""
    prefix = CHANNELS[channel_key]["oss_prefix"]
    last_error = None
    for attempt in range(3):
        try:
            return subprocess.run(
                [OSS_FILE, "-c", OSS_CONFIG, "ls", "-d", prefix],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                check=True,
                timeout=OSS_QUERY_TIMEOUT_SECONDS
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            last_error = exc
            write_log(
                f"OSS版本获取失败[{channel_key}]（第{attempt + 1}/3次）：{str(exc)}",
                level="WARNING"
            )
            if attempt < 2:
                time.sleep(2)
    raise last_error


def get_oss_versions(channel_key=DEFAULT_CHANNEL, use_cache=False):
    """从OSS获取指定版本线 x86_64 架构的补丁版本列表（供前端下拉框用）
    按文件名前缀序号（01~16）排序，前端直接沿用此顺序

    use_cache=True 时使用 5 分钟进程内缓存（页面加载）；
    构建流程使用默认的 False，确保拿到最新版本列表。
    """
    if channel_key not in CHANNELS:
        write_log(f"未知版本线：{channel_key}", level="ERROR")
        return []

    channel = CHANNELS[channel_key]
    cache = _OSS_VERSIONS_CACHE.setdefault(channel_key, {"versions": [], "expires_at": 0.0})
    now = time.time()
    if use_cache and cache["versions"] and now < cache["expires_at"]:
        return list(cache["versions"])

    try:
        result = _run_oss_ls(channel_key)

        versions = []
        seen_versions = set()

        for line in result.stdout.split('\n'):
            line = line.strip()
            if line and ".tar.gz" in line and "x86_64" in line and not line.endswith('/'):
                # 提取完整文件名（如"01-20250430-26798-BUG_x86_64.tar.gz"）
                file_name = line.split('/')[-1]
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
        write_log(f"OSS[{channel_key}]版本按序号排序完成，共{len(versions)}个版本")

        # 保留 OSS 获取的 PATCH 版本到临时文本，方便 shell 脚本使用。
        # 先写临时文件再原子替换，避免脚本读到写了一半的版本列表。
        tmp_versions_file = _oss_versions_tmp_path(channel_key)
        write_tmp = f"{tmp_versions_file}.{uuid.uuid4().hex[:8]}.tmp"
        with open(write_tmp, 'w', encoding='utf-8') as f:
            for ver in versions:
                f.write(f"{ver['value']}\n")
        os.replace(write_tmp, tmp_versions_file)

        # 默认版本线额外维护一份历史文件名，兼容单独执行的脚本和旧习惯
        if channel_key == DEFAULT_CHANNEL:
            legacy_tmp = f"{OSS_VERSIONS_TMP_FILE}.{uuid.uuid4().hex[:8]}.tmp"
            with open(legacy_tmp, 'w', encoding='utf-8') as f:
                for ver in versions:
                    f.write(f"{ver['value']}\n")
            os.replace(legacy_tmp, OSS_VERSIONS_TMP_FILE)

        cache["versions"] = list(versions)
        cache["expires_at"] = time.time() + OSS_VERSIONS_CACHE_TTL_SECONDS

        write_log(f"OSS[{channel_key}] 获取的 PATCH 版本已经成功保存到临时文本：{tmp_versions_file}", level="INFO")
        return versions

    except subprocess.TimeoutExpired:
        return _oss_versions_fallback(
            f"OSS版本获取超时（{OSS_QUERY_TIMEOUT_SECONDS}秒）",
            use_cache,
            channel_key
        )
    except Exception as e:
        return _oss_versions_fallback(f"OSS版本获取失败：{str(e)}", use_cache, channel_key)


def _oss_versions_fallback(error_msg, use_cache, channel_key=DEFAULT_CHANNEL):
    # 页面加载场景下，OSS 短暂不可用时继续使用上次成功获取的列表
    cache = _OSS_VERSIONS_CACHE.get(channel_key) or {"versions": []}
    if use_cache and cache["versions"]:
        write_log(error_msg + f"[{channel_key}]，返回上次缓存版本", level="WARNING")
        return list(cache["versions"])
    write_log(error_msg, level="ERROR")
    return []

def get_existing_packages():
    """获取本地 UPGRADE_PACKAGE_DIR 下的升级包列表

    返回格式：[{name, size, mtime, source}, ...]，按 mtime 倒序
    """
    try:
        package_pattern = os.path.join(UPGRADE_PACKAGE_DIR, "*.tar.gz")
        package_files = glob.glob(package_pattern)

        packages = []
        for file_path in package_files:
            if os.path.isfile(file_path):
                try:
                    st = os.stat(file_path)
                except OSError:
                    continue
                packages.append({
                    "name": os.path.basename(file_path),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "source": "local",
                })

        packages.sort(key=lambda p: p["mtime"], reverse=True)
        write_log(f"获取已有升级包成功，共{len(packages)}个有效包")
        return packages

    except Exception as e:
        error_msg = f"获取已有升级包列表失败：{str(e)}"
        write_log(error_msg, level="ERROR")
        return []


def _format_gb(num_bytes):
    try:
        return float(num_bytes) / (1024 ** 3)
    except (TypeError, ValueError):
        return 0.0


def _package_entries():
    """列出升级包（按 mtime 从新到旧）。"""
    entries = []
    for path in glob.glob(os.path.join(UPGRADE_PACKAGE_DIR, "*.tar.gz")):
        try:
            st = os.stat(path)
        except OSError:
            continue
        entries.append({
            "name": os.path.basename(path),
            "path": path,
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def cleanup_old_packages(keep_name=None):
    """按保留策略回收升级包，避免只增不删把盘吃满。

    从最旧的开始删，直到同时满足「数量 <= max_count」且「总大小 <= max_total_gb」。
    三重保护：
      1. 最近 min_keep 个永远保留；
      2. 年龄小于 min_age_minutes 的一律不动（可能正在下载或刚交付）；
      3. 本次刚构建的包（keep_name）永远不动。
    """
    cfg = RETENTION_CONFIG
    summary = {
        "enabled": cfg["enabled"],
        "deleted": [],
        "freed_bytes": 0,
        "kept": 0,
        "total_bytes": 0,
        "reason": "",
    }
    if not cfg["enabled"]:
        summary["reason"] = "保留策略未启用"
        return summary

    entries = _package_entries()
    total = sum(e["size"] for e in entries)
    summary["kept"] = len(entries)
    summary["total_bytes"] = total

    limit_bytes = int(cfg["max_total_gb"] * (1024 ** 3))
    min_keep = max(int(cfg["min_keep"]), 1)
    min_age_seconds = int(cfg["min_age_minutes"]) * 60
    now = time.time()

    remaining = len(entries)
    for entry in reversed(entries):          # 最旧的先处理
        if remaining <= min_keep:
            break
        if remaining <= cfg["max_count"] and total <= limit_bytes:
            break
        if keep_name and entry["name"] == keep_name:
            continue
        if now - entry["mtime"] < min_age_seconds:
            continue

        if cfg["dry_run"]:
            summary["deleted"].append(entry["name"] + "（dry-run，未删除）")
            total -= entry["size"]
            remaining -= 1
            continue

        try:
            os.remove(entry["path"])
        except OSError as exc:
            write_log(f"升级包清理失败：{entry['name']}（{exc}）", level="WARNING")
            continue

        # 本地删掉后同步清 OSS 上的同名对象，避免"本地没了远端还占着"
        if oss_enabled():
            delete_oss_package(entry["name"])

        summary["deleted"].append(entry["name"])
        summary["freed_bytes"] += entry["size"]
        total -= entry["size"]
        remaining -= 1

    summary["kept"] = remaining
    summary["total_bytes"] = max(total, 0)

    if summary["deleted"]:
        write_log(
            "升级包清理：删除 {} 个，释放 {:.2f} GB；剩余 {} 个 / {:.2f} GB".format(
                len(summary["deleted"]),
                _format_gb(summary["freed_bytes"]),
                summary["kept"],
                _format_gb(summary["total_bytes"]),
            ),
            level="INFO",
        )
    elif not summary["reason"]:
        summary["reason"] = "未达到清理阈值"
    return summary


# -------------------------- 升级包存储后端（local / OSS） --------------------------
def oss_enabled():
    return STORAGE_CONFIG.get('backend') == 'oss'


def _oss_prefix_normalized():
    return str(STORAGE_CONFIG.get('oss_prefix') or '').strip('/')


def package_oss_uri(package_name):
    """拼出对象完整 URI；package_name 为空时返回前缀 URI（用于列举）。"""
    bucket = STORAGE_CONFIG['oss_bucket']
    prefix = _oss_prefix_normalized()
    if package_name:
        return "oss://{}/{}/{}".format(bucket, prefix, package_name) if prefix else "oss://{}/{}".format(bucket, package_name)
    return "oss://{}/{}/".format(bucket, prefix) if prefix else "oss://{}/".format(bucket)


def _run_ossutil(args, timeout_seconds):
    """执行 ossutil 子进程（复用 entrypoint 生成的 .ossutilconfig）。"""
    return subprocess.run(
        [OSS_FILE, "-c", OSS_CONFIG] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        timeout=timeout_seconds,
    )


def upload_package_to_oss(local_path, package_name, task_id=None):
    """上传升级包到 OSS，成功返回 URI；失败返回 None。

    上传失败不抛异常：本地文件仍在，构建结果不受影响，只在日志里记 ERROR。
    """
    if not oss_enabled():
        return None
    if not os.path.exists(local_path):
        write_log(f"OSS 上传跳过：本地文件不存在 {local_path}", level="WARNING", task_id=task_id)
        return None

    uri = package_oss_uri(package_name)
    started = time.time()
    try:
        result = _run_ossutil(["cp", "-f", local_path, uri], STORAGE_CONFIG['upload_timeout_seconds'])
    except subprocess.TimeoutExpired:
        write_log(f"OSS 上传超时（{STORAGE_CONFIG['upload_timeout_seconds']}s）：{package_name}",
                  level="ERROR", task_id=task_id)
        return None
    except Exception as exc:
        write_log(f"OSS 上传异常：{package_name}（{exc}）", level="ERROR", task_id=task_id)
        return None

    if result.returncode != 0:
        write_log("OSS 上传失败：{}，输出：{}".format(package_name, (result.stdout or '')[-400:]),
                  level="ERROR", task_id=task_id)
        return None

    write_log("OSS 上传完成：{}（{:.1f}s）→ {}".format(package_name, time.time() - started, uri),
              task_id=task_id)
    return uri


# ossutil ls 输出形如：
# 2026-10-28 11:09:56 +0000 UTC            5   Standard   D8E8...   oss://bucket/prefix/x.tar.gz
_OSS_LS_LINE_RE = re.compile(
    r'^(?P<date>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?\s(?P<size>\d+)\s+\S+\s+\S+\s+(?P<uri>oss://\S+)$'
)


def _oss_time_to_epoch(text):
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return 0


def list_oss_packages():
    """列举 OSS 上的升级包；任何失败都返回空列表（不影响本地列表展示）。"""
    if not oss_enabled():
        return []
    base_uri = package_oss_uri("")
    try:
        result = _run_ossutil(["ls", base_uri], STORAGE_CONFIG['oss_timeout_seconds'])
    except subprocess.TimeoutExpired:
        write_log("OSS 列举超时", level="WARNING")
        return []
    except Exception as exc:
        write_log(f"OSS 列举异常：{exc}", level="WARNING")
        return []

    if result.returncode != 0:
        write_log("OSS 列举失败：" + (result.stdout or '')[-300:], level="WARNING")
        return []

    packages = []
    for line in (result.stdout or '').splitlines():
        match = _OSS_LS_LINE_RE.match(line.strip())
        if not match:
            continue
        name = match.group('uri').rsplit('/', 1)[-1]
        if not name.endswith('.tar.gz'):
            continue
        packages.append({
            "name": name,
            "size": int(match.group('size')),
            "mtime": _oss_time_to_epoch(match.group('date')),
            "source": "oss",
            "oss_uri": match.group('uri'),
        })
    return packages


def presign_package_url(package_name, expires_seconds=None):
    """生成 OSS 预签名下载 URL；失败返回 None。"""
    if not oss_enabled() or not package_name:
        return None
    seconds = int(expires_seconds or STORAGE_CONFIG['presign_seconds'])
    uri = package_oss_uri(package_name)
    try:
        result = _run_ossutil(
            ["presign", uri, "--expires-duration", "{}s".format(seconds)],
            STORAGE_CONFIG['oss_timeout_seconds'],
        )
    except subprocess.TimeoutExpired:
        write_log(f"OSS 签名超时：{package_name}", level="WARNING")
        return None
    except Exception as exc:
        write_log(f"OSS 签名异常：{package_name}（{exc}）", level="WARNING")
        return None

    if result.returncode != 0:
        write_log("OSS 签名失败：{}，输出：{}".format(package_name, (result.stdout or '')[-300:]),
                  level="WARNING")
        return None

    for line in (result.stdout or '').splitlines():
        line = line.strip()
        if line.startswith('http'):
            return line
    return None


def delete_oss_package(package_name):
    """删除 OSS 上的包（保留策略用），失败只告警。"""
    if not oss_enabled() or not package_name:
        return False
    uri = package_oss_uri(package_name)
    try:
        result = _run_ossutil(["rm", uri], STORAGE_CONFIG['oss_timeout_seconds'])
    except Exception as exc:
        write_log(f"OSS 删除异常：{package_name}（{exc}）", level="WARNING")
        return False
    if result.returncode != 0:
        write_log("OSS 删除失败：{}，输出：{}".format(package_name, (result.stdout or '')[-300:]),
                  level="WARNING")
        return False
    return True


def _is_safe_version_value(value):
    """版本号会参与目录名/包名拼接，拒绝路径分隔符和 .. 等危险字符。"""
    if not value or '..' in value:
        return False
    return _VERSION_VALUE_RE.match(value) is not None


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
    
    # 计算绝对路径，确保文件确实位于安全目录内
    safe_dir_abs = os.path.abspath(UPGRADE_PACKAGE_DIR)
    file_path_abs = os.path.abspath(os.path.join(UPGRADE_PACKAGE_DIR, filename))
    if os.path.dirname(file_path_abs) != safe_dir_abs:
        return False, "文件不在允许的下载目录内"
    
    # 确认文件存在且是普通文件
    if not os.path.exists(file_path_abs) or not os.path.isfile(file_path_abs):
        return False, "文件不存在或不是有效文件"
    
    return True, file_path_abs


def _create_upgrade_archive(files_to_pack, upgrade_path, task_log_path):
    """先打包到临时文件，再原子替换为目标包。

    这样下载方永远不会读到写了一半的包，也能避免上一次同名包在写入过程中被破坏。
    """
    tmp_path = f"{upgrade_path}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(task_log_path, 'a', encoding='utf-8') as f:
            subprocess.run(
                [
                    "tar", "-czf", tmp_path,
                    "--transform", r"s|.*/||",             # 第一步：去掉文件路径前缀
                    "--transform", f"s|.*|{os.path.basename(upgrade_path).rsplit('.tar.gz', 1)[0]}/&|",  # 第二步：加上目录前缀
                    *files_to_pack
                ],
                check=True,
                stdout=f,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                timeout=ARCHIVE_TIMEOUT_SECONDS
            )

        if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            raise Exception(f"打包失败：升级包{upgrade_path}不存在或为空文件")

        os.replace(tmp_path, upgrade_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _prepare_upgrade_script(channel, target_version, task_image_dir, task_id):
    """把升级脚本复制到任务目录，并把 upgrade_version 里的 patch 号替换为本次构建的实际值。

    返回 (可打包的脚本路径, 填入的版本号或 None)。

    注意：包内脚本必须保持原名 deepflow_patch_upgrade.sh，客户解压后直接执行。
    匹配不到 upgrade_version 时回退到原始脚本，不影响构建。
    """
    target_seq = (target_version or '').split('-')[0]
    if not target_seq.isdigit():
        return UPGRADE_SCRIPT_PATH, None

    try:
        with open(UPGRADE_SCRIPT_PATH, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        return UPGRADE_SCRIPT_PATH, None

    filled = "DeepFlow-V{}-{}".format(channel["sub"], target_seq)
    patched, count = re.subn(
        r'^upgrade_version="[^"]*"',
        'upgrade_version="{}"'.format(filled),
        content,
        count=1,
        flags=re.MULTILINE
    )
    if count == 0:
        return UPGRADE_SCRIPT_PATH, None

    dst_dir = os.path.join(task_image_dir, "upgrade_script")
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, os.path.basename(UPGRADE_SCRIPT_PATH))
    with open(dst, 'w', encoding='utf-8') as f:
        f.write(patched)
    os.chmod(dst, 0o755)
    return dst, filled


def run_build_task(task_id, current_version, target_version, channel_key=DEFAULT_CHANNEL):
    """核心构建任务：执行构建list脚本→拉取镜像→打包TAR.GZ格式升级包"""
    if channel_key not in CHANNELS:
        channel_key = DEFAULT_CHANNEL
    channel = CHANNELS[channel_key]
    try:
        # 创建任务专属目录（用版本+任务ID命名，确保唯一性，避免文件冲突）
        task_dir_name = f"{current_version}_to_{target_version}_{task_id}"
        task_image_dir = os.path.join(IMAGE_TAR_ROOT, task_dir_name)
        # 确保任务目录存在（不存在则创建，存在则清空历史残留）
        if os.path.exists(task_image_dir):
            shutil.rmtree(task_image_dir)
        os.makedirs(task_image_dir, exist_ok=True)

        # 为每个任务生成独立的镜像列表文件路径，避免并发竞争
        task_patch_list_path = os.path.join(LATEST_LIST_DIR, f"patch_image_tag_list_{task_id}.txt")

        # 创建任务专属日志文件（记录脚本详细输出，避免实时刷屏）
        task_log_path = os.path.join(LOG_DIR, f"task_{task_id}.log")

        try:
            write_log(f"任务[{task_id}]启动：版本线 {channel_key}，升级 {current_version} → {target_version}", task_id=task_id)
            time.sleep(1)  # 预留SSE状态推送时间，避免前端接收延迟
            _raise_if_cancelled(task_id)

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
            # 当前 Pod 本地、当前版本线专属的 OSS 版本缓存，供 shell 脚本读取。
            update_progress(task_id, 12, f"刷新 {channel_key} 的 OSS 补丁版本缓存")
            oss_versions = get_oss_versions(channel_key)
            if not oss_versions:
                raise Exception(f"{channel_key} 的 OSS版本缓存刷新失败，无法生成镜像列表")
            write_log(f"OSS版本缓存刷新完成[{channel_key}]，共{len(oss_versions)}个版本", task_id=task_id)
            _raise_if_cancelled(task_id)

            # 执行get_patch_image_tag_list.sh，生成镜像拉取清单
            update_progress(task_id, 15, f"执行镜像列表生成脚本，参数：{current_version} {target_version}")
            write_log(f"开始生成镜像列表，当前版本: {current_version}, 目标版本: {target_version}", task_id=task_id)
            
            # 调用Shell脚本，传递当前版本和目标版本参数
            # 任务专属输出路径通过第3个参数传入，避免并发竞争；
            # 版本线相关参数用环境变量传入，脚本本身早已支持这三个变量。
            script_env = dict(os.environ)
            script_env.update({
                "NUWA_MAJOR": channel["series"],
                "NUWA_MINOR": channel["sub"],
                "IMAGE_TAG_PREFIX": channel["tag_prefix"],
                "OSS_PATCH_LIST_FILE": _oss_versions_tmp_path(channel_key),
            })
            with open(task_log_path, 'a', encoding='utf-8') as f:
                subprocess.run(
                    ["/bin/bash", PATCH_LIST_SCRIPT_PATH, current_version, target_version, task_patch_list_path],
                    check=True,
                    env=script_env,
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
            write_log("镜像列表生成成功，脚本输出已记录", task_id=task_id)
            
            update_progress(task_id, 30, "镜像拉取清单生成完成，准备拉取镜像文件")
            # 读取镜像列表文件，获取要拉取的镜像数量
            with open(task_patch_list_path, 'r', encoding='utf-8') as f:
                image_count = len([line for line in f if line.strip()])
            write_log(f"发现 {image_count} 个需要拉取的镜像", task_id=task_id)

            # 区间内可能全部是"只改基础镜像/只发 agent"的 patch，清单会为空。
            # 直接给出可读的失败原因，避免最后报"任务目录无任何 .tar"这种误导性错误。
            if image_count == 0:
                raise Exception(
                    f"{current_version} → {target_version} 区间内没有需要打包的镜像变更"
                    f"（版本线 {channel_key}）。该区间可能只包含基础镜像或空 patch。"
                )

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
            _raise_if_cancelled(task_id)

            if not os.path.exists(UPGRADE_SCRIPT_PATH) or not os.path.isfile(UPGRADE_SCRIPT_PATH):
                raise Exception(f"升级脚本不存在：{UPGRADE_SCRIPT_PATH}")

            # 复制镜像列表到任务目录的标准文件名，确保 upgrade 脚本能找到
            standard_list_path = os.path.join(task_image_dir, "patch_image_tag_list.txt")
            shutil.copy2(task_patch_list_path, standard_list_path)

            # 把包里那份升级脚本的 upgrade_version 末尾 patch 号替换成本次目标 patch，
            # 省掉"交付前忘了改 -xx"这类事故。脚本其余内容（components/target_registry）
            # 仍按交付方定制处理，不在这里改动。
            packed_upgrade_script, filled_version = _prepare_upgrade_script(
                channel, target_version, task_image_dir, task_id
            )
            if filled_version:
                write_log(
                    f"升级脚本版本号已自动填为 {filled_version}（原脚本为固定值）",
                    task_id=task_id
                )
            if channel_key != DEFAULT_CHANNEL:
                write_log(
                    f"注意：包内升级脚本是 {DEFAULT_CHANNEL} 的定制件（components/target_registry 针对特定客户），"
                    f"交付 {channel_key} 前请确认是否需要按现场调整",
                    level="WARNING",
                    task_id=task_id
                )

            files_to_pack = tar_files + [standard_list_path, packed_upgrade_script]
            write_log(f"准备打包 {len(files_to_pack)} 个文件", task_id=task_id)

            # 打包TAR.GZ升级包（仅包含当前任务的镜像+镜像列表）
            update_progress(task_id, 75, "开始打包TAR.GZ升级包")
            current_patch_version = current_version.split('-')[0]
            target_patch_version = target_version.split('-')[0]
            upgrade_package = channel["package_template"].format(
                current=current_patch_version, target=target_patch_version
            )
            upgrade_path = os.path.join(UPGRADE_PACKAGE_DIR, upgrade_package)

            # 打包TAR.GZ升级包（两步 transform：先去路径前缀，再加目录前缀）
            _create_upgrade_archive(files_to_pack, upgrade_path, task_log_path)
            
            update_progress(task_id, 90, "验证升级包完整性")
            # 验证包大小
            package_size = os.path.getsize(upgrade_path)
            write_log(f"升级包大小：{package_size/1024/1024:.2f}MB", task_id=task_id)

            # 上传到 OSS（仅 backend=oss）：失败不影响构建结果，本地文件仍可下载
            oss_uri = None
            if oss_enabled():
                update_progress(task_id, 93, "上传升级包到 OSS")
                oss_uri = upload_package_to_oss(upgrade_path, upgrade_package, task_id=task_id)
                # 已确认上云后再删本地，保证任何时候都至少有一份可下载
                if oss_uri and STORAGE_CONFIG.get('delete_local_after_upload'):
                    try:
                        os.remove(upgrade_path)
                        write_log(f"本地副本已删除（已上传 OSS）：{upgrade_package}", task_id=task_id)
                    except OSError as exc:
                        write_log(f"删除本地副本失败：{exc}", level="WARNING", task_id=task_id)

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
                "oss_uri": oss_uri,
                "package_size_mb": round(package_size/1024/1024, 2),  # 包大小（MB）
                "download_expire_at": int(time.time()) + REDIS_CONFIG['download_ttl_seconds']
            })
            write_log(f"任务[{task_id}]完全结束：{current_version}→{target_version}升级包构建完成，请在90秒内下载升级包", task_id=task_id)

            # 构建成功后再回收旧包：失败的构建不应该动历史产物
            try:
                cleanup_old_packages(keep_name=upgrade_package)
            except Exception as cleanup_exc:
                write_log(f"升级包清理异常（不影响本次构建）：{cleanup_exc}", level="WARNING")

            schedule_task_cleanup(task_id, REDIS_CONFIG['success_ttl_seconds'])

        except BuildCancelled:
            mark_task_cancelled(task_id)
            write_log(f"任务[{task_id}]已取消", level="WARNING", task_id=task_id)
            schedule_task_cleanup(task_id, REDIS_CONFIG['failure_ttl_seconds'])

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
        redis_client.delete(task_cancel_key(task_id))
        release_build_slot(task_id)

def run_build_task_v7(task_id, images):
    """自定义镜像构建任务：按用户给定镜像清单→拉取镜像→打包TAR.GZ格式升级包"""
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
            _raise_if_cancelled(task_id)

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
            _raise_if_cancelled(task_id)

            if not os.path.exists(UPGRADE_SCRIPT_PATH) or not os.path.isfile(UPGRADE_SCRIPT_PATH):
                raise Exception(f"升级脚本不存在：{UPGRADE_SCRIPT_PATH}")
            files_to_pack = tar_files + [task_patch_list_path, UPGRADE_SCRIPT_PATH]

            update_progress(task_id, 75, "开始打包TAR.GZ升级包")
            upgrade_package = f"deepflow_patch_custom_{task_id}.tar.gz"
            upgrade_path = os.path.join(UPGRADE_PACKAGE_DIR, upgrade_package)

            _create_upgrade_archive(files_to_pack, upgrade_path, task_log_path)

            update_progress(task_id, 90, "验证升级包完整性")
            package_size = os.path.getsize(upgrade_path)
            write_log(f"升级包大小：{package_size/1024/1024:.2f}MB", task_id=task_id)

            # 上传到 OSS（仅 backend=oss）：失败不影响构建结果，本地文件仍可下载
            oss_uri = None
            if oss_enabled():
                update_progress(task_id, 93, "上传升级包到 OSS")
                oss_uri = upload_package_to_oss(upgrade_path, upgrade_package, task_id=task_id)
                # 已确认上云后再删本地，保证任何时候都至少有一份可下载
                if oss_uri and STORAGE_CONFIG.get('delete_local_after_upload'):
                    try:
                        os.remove(upgrade_path)
                        write_log(f"本地副本已删除（已上传 OSS）：{upgrade_package}", task_id=task_id)
                    except OSError as exc:
                        write_log(f"删除本地副本失败：{exc}", level="WARNING", task_id=task_id)

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
                "oss_uri": oss_uri,
                "package_size_mb": round(package_size/1024/1024, 2),
                "download_expire_at": int(time.time()) + REDIS_CONFIG['download_ttl_seconds']
            })
            write_log(f"任务[{task_id}]完全结束：V7升级包构建完成，请在90秒内下载升级包", task_id=task_id)

            # 构建成功后再回收旧包：失败的构建不应该动历史产物
            try:
                cleanup_old_packages(keep_name=upgrade_package)
            except Exception as cleanup_exc:
                write_log(f"升级包清理异常（不影响本次构建）：{cleanup_exc}", level="WARNING")

            schedule_task_cleanup(task_id, REDIS_CONFIG['success_ttl_seconds'])

        except BuildCancelled:
            mark_task_cancelled(task_id)
            write_log(f"任务[{task_id}]已取消", level="WARNING", task_id=task_id)
            schedule_task_cleanup(task_id, REDIS_CONFIG['failure_ttl_seconds'])

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
        redis_client.delete(task_cancel_key(task_id))
        release_build_slot(task_id)


# -------------------------- Flask路由 --------------------------
@app.route('/')
def index():
    """前端页面入口"""
    return render_template('index.html')


@app.route('/health', methods=['GET'])
def health():
    """轻量健康检查，供 K8s 探针使用（不渲染页面、不访问 OSS）。"""
    try:
        redis_client.ping()
        return jsonify({"status": "ok", "redis": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "redis": str(e)}), 503

# -------------------------- 获取已有升级包列表 --------------------------
@app.route('/existing-packages', methods=['GET'])
def api_existing_packages():
    """
    前端调用：获取UPGRADE_PACKAGE_DIR目录下所有已生成的tar.gz升级包
    """
    try:
        # 调用工具函数获取文件列表
        packages = get_existing_packages()

        # OSS 后端：合并远端列表（本地同名的以本地为准，避免重复）
        if oss_enabled():
            try:
                local_names = {p["name"] for p in packages}
                remote = [p for p in list_oss_packages() if p["name"] not in local_names]
                if remote:
                    packages = packages + remote
                    packages.sort(key=lambda p: p.get("mtime") or 0, reverse=True)
            except Exception as exc:
                write_log(f"合并 OSS 升级包列表失败（仅返回本地）：{exc}", level="WARNING")

        return jsonify({
            "success": True,
            "files": packages,
            "storage_backend": STORAGE_CONFIG['backend'],
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


# -------------------------- 手动触发升级包清理 --------------------------
@app.route('/packages/cleanup', methods=['POST'])
def api_cleanup_packages():
    """运维用：立即按保留策略回收升级包（自动清理在每次构建成功后已经跑过一次）。"""
    try:
        summary = cleanup_old_packages()
        return jsonify({"success": True, **summary})
    except Exception as e:
        error_msg = f"升级包清理失败：{str(e)}"
        write_log(error_msg, level="ERROR")
        return jsonify({"success": False, "message": error_msg}), 500


# -------------------------- 任务中心总览 --------------------------
@app.route('/tasks/overview', methods=['GET'])
def api_tasks_overview():
    """任务中心：进行中/最近完成任务 + 单节点运行状态（worker、队列、磁盘）。"""
    try:
        active, recent = get_tasks_overview()
        response = jsonify({
            "success": True,
            "active": active,
            "recent": recent,
            "system": get_system_status()
        })
        response.headers['Cache-Control'] = 'no-store'
        return response
    except Exception as e:
        error_msg = f"获取任务总览失败：{str(e)}"
        write_log(error_msg, level="ERROR")
        return jsonify({
            "success": False,
            "active": [],
            "recent": [],
            "system": {},
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

    # 先做纯文件名校验：OSS 上的包本地可能没有，不能一上来就要求文件存在
    if (not filename or '..' in filename or '/' in filename or '\\' in filename
            or not filename.endswith('.tar.gz')):
        error_msg = f"文件下载校验失败：非法文件名 {filename}"
        write_log(error_msg, level="WARNING")
        abort(403, description=error_msg)

    # OSS 后端：优先用签名 URL 直接跳转（本地没有该文件也能下载）
    if oss_enabled():
        url = presign_package_url(filename)
        if url:
            write_log(f"已有升级包下载（OSS 签名跳转）：文件名={filename}")
            return redirect(url, code=302)

    # 回退本地文件
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
            download_name=filename,
            mimetype='application/gzip'
        )
        response.headers['Accept-Ranges'] = 'bytes'
        write_log(f"已有升级包下载成功：文件名={filename}，路径={file_path}")
        return response
    except Exception as e:
        error_msg = f"文件下载失败：{str(e)}"
        write_log(error_msg, level="ERROR")
        abort(500, description=error_msg)


# -------------------------- 版本线列表 --------------------------
@app.route('/channels', methods=['GET'])
def api_get_channels():
    """返回支持的版本线，供前端渲染下拉。数据来自后端唯一事实来源 CHANNELS。"""
    channels = []
    for key, ch in CHANNELS.items():
        channels.append({
            "key": key,
            "label": key,
            "series": ch["series"],
            "sub": ch["sub"],
            "tag_prefix": ch["tag_prefix"],
            "package_name_template": ch["package_template"],
            "is_default": key == DEFAULT_CHANNEL,
        })
    return jsonify({
        "success": True,
        "channels": channels,
        "default": DEFAULT_CHANNEL,
        "message": f"共{len(channels)}条版本线"
    })


# -------------------------- 获取版本列表（兼容前端） --------------------------
@app.route('/versions', methods=['GET'])
def api_get_versions():
    """从OSS获取指定版本线的补丁版本列表，供前端下拉框选择。

    参数：?channel=7.1.9；不传时回退到默认版本线，`?series=v6` 等旧参数继续兼容。
    """
    raw = request.args.get('channel') or request.args.get('series')
    channel_key = resolve_channel(raw)
    if not channel_key:
        return jsonify({
            "success": False,
            "versions": [],
            "message": f"不支持的版本线：{raw}（可选：{', '.join(CHANNELS)}）"
        }), 400

    versions = get_oss_versions(channel_key, use_cache=True)
    if versions:
        return jsonify({
            "success": True,
            "channel": channel_key,
            "versions": versions,
            "package_name_template": CHANNELS[channel_key]["package_template"],
            "message": f"{channel_key} 成功获取{len(versions)}个版本"
        })
    else:
        return jsonify({
            "success": False,
            "channel": channel_key,
            "versions": [],
            "message": f"{channel_key} 未获取到有效版本（可能是OSS连接失败或无x86_64版本）"
        }), 500


# -------------------------- 查询任务状态 --------------------------
@app.route('/task-status/<task_id>', methods=['GET'])
def api_task_status(task_id):
    """查询指定任务的当前状态，包含日志信息。支持 ?since=<seq> 增量返回日志。"""
    since_seq = _parse_since_seq()
    task_status = get_task_status(task_id, since_seq=since_seq)
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
    """创建构建任务（V6/V7 共用）。

    只负责入队并返回 task_id，不再复用 GET 建任务 + SSE 的混合语义：
    前端拿到 task_id 后统一通过 `GET /build?task_id=...` 订阅进度，
    这样 SSE 重连不会重复创建任务。
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    # mode 是新的语义：patch=固定版本按 patch 差异打包，custom=自定义镜像清单。
    # 兼容旧调用：只传 series（v6/v7）时按旧语义推断。
    series = data.get('series')
    mode = data.get('mode') or ('custom' if series == 'v7' else 'patch')

    try:
        if mode == 'custom':
            images = data.get('images', [])
            if not isinstance(images, list) or not images:
                return jsonify({"success": False, "message": "缺少必要参数：images 不能为空"}), 400
            images = [img.strip() for img in images if isinstance(img, str) and img.strip()]
            if not images:
                return jsonify({"success": False, "message": "镜像列表为空，请输入有效镜像名称"}), 400
            task_id = _create_v7_build_task(images)
        elif mode == 'patch':
            channel_key = resolve_channel(data.get('channel') or series)
            if not channel_key:
                return jsonify({
                    "success": False,
                    "message": f"不支持的版本线：{data.get('channel') or series}"
                }), 400
            current_version = (data.get('current') or '').strip()
            target_version = (data.get('target') or '').strip()
            error = _validate_v6_versions(current_version, target_version)
            if error:
                return jsonify({"success": False, "message": error}), 400
            task_id = _create_v6_build_task(current_version, target_version, channel_key)
        else:
            return jsonify({"success": False, "message": f"不支持的 mode: {mode}"}), 400
    except Exception as e:
        return jsonify({"success": False, "message": f"任务入队失败：{str(e)}"}), 500

    return jsonify({"success": True, "task_id": task_id})


@app.route('/build/<task_id>/cancel', methods=['POST'])
def api_cancel_build(task_id):
    """请求取消一个排队中/执行中的构建任务。"""
    task_status = get_task_status(task_id, include_logs=False)
    if not task_status:
        return jsonify({"success": False, "message": "任务不存在或已过期"}), 404
    if task_status.get('complete'):
        return jsonify({"success": False, "message": "任务已结束，无法取消"}), 409

    redis_client.set(task_cancel_key(task_id), "1", ex=REDIS_CONFIG['failure_ttl_seconds'])
    write_log(f"收到任务[{task_id}]的取消请求", level="WARNING", task_id=task_id)

    # 排队中的任务不必等 worker 取到再取消；执行中的任务由 worker 的取消监听终止
    if task_status.get('status') == 'queued':
        mark_task_cancelled(task_id)

    return jsonify({"success": True, "message": "取消请求已提交"})


@app.route('/build', methods=['GET'])
def api_build():
    """构建入口：V6 用 GET 参数入队；通过 task_id 参数可仅观察已有任务。返回 SSE 流。"""
    task_id = request.args.get('task_id')
    # 必须在视图函数内解析（请求上下文有效）；生成器开始迭代时上下文可能已释放
    initial_since_seq = _parse_since_seq()
    # 仅观察已有任务（不创建新任务）
    if task_id:
        def watch_sse():
            since_seq = initial_since_seq
            try:
                while True:
                    task_status = get_task_status(task_id, since_seq=since_seq)
                    if not task_status:
                        retry = 0
                        while retry < 3:
                            task_status = get_task_status(task_id, since_seq=since_seq)
                            if task_status:
                                break
                            time.sleep(0.5)
                            retry += 1
                        if not task_status:
                            yield f"data: {json.dumps({'status': 'error', 'percent': 0, 'message': '任务状态已过期', 'logs': []})}\n\n"
                            break
                    since_seq = _advance_log_cursor(since_seq, task_status)
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

    # 旧版 GET 入队流程（固定版本 + 兼容旧的自定义镜像 GET 调用）
    series = request.args.get('series', 'v6')
    current_version = request.args.get('current')
    target_version = request.args.get('target')
    v7_images = request.args.get('images', '')

    try:
        if series == 'v7':
            if not v7_images.strip():
                return jsonify({"success": False, "message": "缺少必要参数：images不能为空"}), 400
            images = [img.strip() for img in v7_images.split(',') if img.strip()]
            if not images:
                return jsonify({"success": False, "message": "镜像列表为空，请输入有效镜像名称"}), 400
            task_id = _create_v7_build_task(images)
        else:
            channel_key = resolve_channel(request.args.get('channel') or series)
            if not channel_key:
                return jsonify({
                    "success": False,
                    "message": f"不支持的版本线：{request.args.get('channel') or series}"
                }), 400
            error = _validate_v6_versions(current_version, target_version)
            if error:
                return jsonify({"success": False, "message": error}), 400
            task_id = _create_v6_build_task(current_version, target_version, channel_key)
    except Exception as e:
        return jsonify({"success": False, "message": f"任务入队失败：{str(e)}"}), 500

    # 建立SSE连接，实时推送进度和日志
    def generate_sse():
        since_seq = initial_since_seq
        try:
            # 初始状态推送
            initial_status = get_task_status(task_id, since_seq=since_seq) or {
                'status': 'progress',
                'percent': 0,
                'message': '任务初始化中...',
                'logs': []
            }
            since_seq = _advance_log_cursor(since_seq, initial_status)
            yield f"data: {json.dumps(initial_status)}\n\n"

            # 循环推送状态
            while True:
                task_status = get_task_status(task_id, since_seq=since_seq)
                if not task_status:
                    # 重试3次，若仍不存在则判定过期
                    retry = 0
                    while retry < 3:
                        task_status = get_task_status(task_id, since_seq=since_seq)
                        if task_status:
                            break
                        time.sleep(0.5)
                        retry += 1
                    if not task_status:
                        yield f"data: {json.dumps({'status': 'error', 'percent': 0, 'message': '任务状态已过期，请刷新页面重试', 'logs': []})}\n\n"
                        break

                since_seq = _advance_log_cursor(since_seq, task_status)
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

    # OSS 后端：优先签名 URL 跳转，避免依赖本机文件
    if oss_enabled():
        url = presign_package_url(task_status['package_name'])
        if url:
            write_log(f"新构建包下载（OSS 签名跳转）：任务ID={task_id}，文件名={task_status['package_name']}")
            return redirect(url, code=302)

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
            download_name=task_status['package_name'],
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
    app.run(host='0.0.0.0', port=8000, threaded=True, debug=os.environ.get('FLASK_DEBUG') == '1')
