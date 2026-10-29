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
from contextlib import contextmanager
import tempfile
from flask import Flask, request, Response, jsonify, send_file, abort, render_template, redirect # type: ignore
import traceback
from urllib.parse import quote, unquote
import yaml # type: ignore
import redis # type: ignore
from image_refs import DEFAULT_REPOSITORY, normalize_image_ref, normalize_images

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
if os.environ.get('REQUIRE_OSS_STORAGE') == '1' and STORAGE_CONFIG['backend'] != 'oss':
    raise ValueError('独立 worker 产物目录要求 package_storage.backend=oss')
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


def build_upload_queue_key(worker_id):
    return f"{REDIS_CONFIG['key_prefix']}:build:upload:{worker_id}"


class TaskLeaseLost(Exception):
    """当前执行尝试已被回收；旧执行者必须停止发布状态和清理共享文件。"""


_execution_local = threading.local()


@contextmanager
def execution_context(task_id, owner_token):
    previous = getattr(_execution_local, 'execution', None)
    _execution_local.execution = (task_id, owner_token)
    try:
        yield
    finally:
        _execution_local.execution = previous


def get_task_owner(task_id):
    execution = getattr(_execution_local, 'execution', None)
    return execution[1] if execution and execution[0] == task_id else None


def assert_task_owned(task_id, owner_token=None):
    owner = owner_token or get_task_owner(task_id)
    if owner and redis_client.get(task_slot_key(task_id)) != owner:
        raise TaskLeaseLost(f"任务[{task_id}]执行权已失效")


# Each script checks the execution token and mutates data in the same Redis turn.
# A recovered worker cannot overwrite the new attempt between a check and write.
_WRITE_TASK_STATUS = """-- write_task_status_v1
if ARGV[1] ~= '' and redis.call('GET', KEYS[1]) ~= ARGV[1] then return -1 end
redis.call('SET', KEYS[2], ARGV[2])
local ttl = tonumber(ARGV[3])
if ttl > 0 then
  for i = 2, #KEYS do redis.call('EXPIRE', KEYS[i], ttl) end
else
  for i = 3, #KEYS do redis.call('PERSIST', KEYS[i]) end
end
return 1
"""

_APPEND_TASK_LOG = """-- append_task_log_v1
if ARGV[1] ~= '' and redis.call('GET', KEYS[1]) ~= ARGV[1] then return -1 end
if redis.call('EXISTS', KEYS[2]) == 0 then return 0 end
local entry = cjson.decode(ARGV[2])
entry.seq = redis.call('INCR', KEYS[3])
redis.call('RPUSH', KEYS[4], cjson.encode(entry))
redis.call('LTRIM', KEYS[4], -tonumber(ARGV[3]), -1)
local ttl = redis.call('TTL', KEYS[2])
if ttl >= 0 then
  ttl = math.max(ttl, 1)
  redis.call('EXPIRE', KEYS[3], ttl)
  redis.call('EXPIRE', KEYS[4], ttl)
else
  redis.call('PERSIST', KEYS[3])
  redis.call('PERSIST', KEYS[4])
end
return entry.seq
"""

_TASK_CLEANUP = """-- task_cleanup_v1
if ARGV[1] ~= '' and redis.call('GET', KEYS[1]) ~= ARGV[1] then return -1 end
for i = 2, #KEYS do redis.call('EXPIRE', KEYS[i], tonumber(ARGV[2])) end
return 1
"""

_COMPARE_DELETE = """-- compare_delete_v1
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""

_CLEAR_CANCEL = """-- clear_task_cancel_v1
if ARGV[1] ~= '' and redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[2])
"""

_ACQUIRE_SLOT = """-- acquire_build_slot_v1
if #KEYS == 3 and not redis.call('LPOS', KEYS[3], ARGV[5]) then return -1 end
local existing = redis.call('GET', KEYS[1])
if existing then
  if existing == ARGV[1] then return 1 end
  return 0
end
local slots = redis.call('KEYS', ARGV[4])
if #slots >= tonumber(ARGV[2]) then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[3]))
redis.call('SET', KEYS[2], #slots + 1)
return 1
"""


def clear_task_cancel(task_id):
    redis_client.eval(_CLEAR_CANCEL, 2, task_slot_key(task_id), task_cancel_key(task_id),
                      get_task_owner(task_id) or '')


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

        # Only acknowledge entries actually returned. A concurrent append after
        # LRANGE must remain visible on the next request, rather than advancing
        # the client cursor to a sequence it has never received.
        status['log_seq'] = max([since_seq] + [entry.get('seq', 0) for entry in entries])
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
    assert_task_owned(task_id)
    if is_build_cancelled(task_id):
        raise BuildCancelled("构建已被用户取消")


def set_task_status(task_id, status, ttl_seconds=None):
    status_to_store = dict(status)
    status_to_store.pop('logs', None)
    result = redis_client.eval(
        _WRITE_TASK_STATUS, 4, task_slot_key(task_id), task_meta_key(task_id),
        task_logs_key(task_id), task_log_seq_key(task_id), get_task_owner(task_id) or '',
        json.dumps(status_to_store, ensure_ascii=False), int(ttl_seconds or 0))
    if result == -1:
        raise TaskLeaseLost(f"任务[{task_id}]执行权已失效")


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
        "cancelled": True,
        "upload_retry_available": False,
        "download_url": None,
        "download_expire_at": None
    }, ttl_seconds=REDIS_CONFIG['failure_ttl_seconds'])


def append_task_log(task_id, timestamp, level, content):
    log_entry = json.dumps({
        'timestamp': timestamp,
        'level': level,
        'content': content
    }, ensure_ascii=False)
    result = redis_client.eval(
        _APPEND_TASK_LOG, 4, task_slot_key(task_id), task_meta_key(task_id),
        task_log_seq_key(task_id), task_logs_key(task_id), get_task_owner(task_id) or '',
        log_entry, REDIS_CONFIG['max_logs'])
    if result == -1:
        raise TaskLeaseLost(f"任务[{task_id}]执行权已失效")


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
    result = redis_client.eval(
        _TASK_CLEANUP, 4, task_slot_key(task_id), task_meta_key(task_id),
        task_logs_key(task_id), task_log_seq_key(task_id), get_task_owner(task_id) or '',
        delay_seconds)
    if result == -1:
        raise TaskLeaseLost(f"任务[{task_id}]执行权已失效")


def _count_active_slots():
    """统计当前真实持有的构建槽位数量。以 slot key 为准，避免计数漂移。"""
    slot_pattern = f"{REDIS_CONFIG['key_prefix']}:task:*:slot"
    return sum(1 for _ in redis_client.scan_iter(match=slot_pattern))


def try_acquire_build_slot(task_id, owner_token=None, processing_key=None, raw_payload=None):
    """原子限流并绑定执行尝试；只有相同 token 的重试视为幂等。"""
    owner = owner_token or get_task_owner(task_id) or 'legacy'
    keys = [task_slot_key(task_id), active_builds_key()]
    if processing_key is not None:
        keys.append(processing_key)
    result = redis_client.eval(
        _ACQUIRE_SLOT, len(keys), *keys, owner,
        REDIS_CONFIG['max_concurrent_tasks'], SLOT_TTL_SECONDS,
        f"{REDIS_CONFIG['key_prefix']}:task:*:slot", raw_payload or '')
    if result == -1:
        raise TaskLeaseLost(f"任务[{task_id}]队列执行权已失效")
    return bool(result)


def release_build_slot(task_id, owner_token=None):
    owner = owner_token or get_task_owner(task_id) or 'legacy'
    if redis_client.eval(_COMPARE_DELETE, 1, task_slot_key(task_id), owner):
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


def _is_worker_online(max_age_seconds=None):
    """通过 worker 心跳判断是否有可用的构建进程。"""
    if max_age_seconds is None:
        max_age_seconds = int(os.environ.get('WORKER_HEARTBEAT_TTL_SECONDS', 60))
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
            "oss_object_name": status.get("oss_object_name"),
            "upload_failed": bool(status.get("upload_failed")),
            "upload_retry_available": bool(status.get("upload_retry_available")),
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

def _kill_process_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_cancellable(task_id, cmd, timeout, stdout=None, env=None):
    """有界执行归档/上传等命令；错误、取消、失去执行权均清理整个进程组。"""
    _raise_if_cancelled(task_id)
    proc = subprocess.Popen(cmd, stdout=stdout if stdout is not None else subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, env=env,
                            start_new_session=True)
    deadline = time.monotonic() + timeout
    output = None
    try:
        while True:
            _raise_if_cancelled(task_id)
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                if stdout is None:
                    output, _ = proc.communicate(timeout=min(0.25, max(0.01, deadline-time.monotonic())))
                else:
                    proc.wait(timeout=min(0.25, max(0.01, deadline-time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                continue
        _raise_if_cancelled(task_id)
        return subprocess.CompletedProcess(cmd, proc.returncode, output)
    except BaseException:
        _kill_process_group(proc)
        proc.wait(timeout=5)
        raise
    finally:
        if proc.stdout is not None:
            proc.stdout.close()


def _run_and_stream(task_id, cmd, task_log_path, log_prefix="", timeout=PULL_TIMEOUT_SECONDS):
    """流式记录脚本输出；所有退出路径都终止或回收整个进程组。"""
    _raise_if_cancelled(task_id)
    owner = get_task_owner(task_id)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, start_new_session=True)
    timed_out = threading.Event()
    finished = threading.Event()
    monitor_error = []

    def monitor():
        deadline = time.monotonic() + timeout
        while not finished.wait(0.2):
            try:
                assert_task_owned(task_id, owner)
                if is_build_cancelled(task_id):
                    raise BuildCancelled("构建已被用户取消")
                if time.monotonic() >= deadline:
                    timed_out.set()
                    _kill_process_group(proc)
                    return
            except Exception as exc:
                monitor_error.append(exc)
                _kill_process_group(proc)
                return

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    try:
        for line in iter(proc.stdout.readline, ''):
            clean = _clean_shell_line(line)
            if not clean:
                continue
            with open(task_log_path, 'a', encoding='utf-8') as f:
                f.write(clean + '\n')
            lower = clean.lower()
            level = "ERROR" if any(x in lower for x in ('error', 'failed', '错误')) else "INFO"
            append_task_log(task_id, time.strftime('%Y-%m-%d %H:%M:%S'), level, clean)
        # 监听器继续保护 wait，直到进程真正退出。
        proc.wait()
    except BaseException:
        _kill_process_group(proc)
        proc.wait(timeout=5)
        raise
    finally:
        finished.set()
        watcher.join(timeout=1)
        proc.stdout.close()
    if monitor_error:
        raise monitor_error[0]
    if timed_out.is_set():
        raise TimeoutError(f"命令执行超时（{timeout}秒），已强制终止进程组")
    _raise_if_cancelled(task_id)
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
        try:
            append_task_log(task_id, timestamp, level, content)
        except TaskLeaseLost:
            pass  # 旧执行者可以写本地诊断，但不能污染新执行者的 Redis 日志

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
    """回收本地与纯 OSS 产物；序列化执行并始终保护最近和新生成的包。"""
    cfg = RETENTION_CONFIG
    summary = {'enabled': cfg['enabled'], 'deleted': [], 'freed_bytes': 0,
               'kept': 0, 'total_bytes': 0, 'reason': ''}
    if not cfg['enabled']:
        summary['reason'] = '保留策略未启用'
        return summary
    token = uuid.uuid4().hex
    lock = f"{REDIS_CONFIG['key_prefix']}:packages:cleanup:lock"
    if not redis_client.set(lock, token, nx=True, ex=120):
        summary['reason'] = '另一个回收正在执行'
        return summary
    try:
        entries = {e['name']: dict(e, remote=False) for e in _package_entries()}
        if oss_enabled():
            # 列举失败时停止，避免以不完整列表计算 min_keep 并误删。
            for remote in list_oss_packages(strict=True):
                local = entries.get(remote['name'], {})
                entries[remote['name']] = dict(remote, path=local.get('path'), remote=True,
                    mtime=max(remote['mtime'], local.get('mtime', 0)))
        entries = sorted(entries.values(), key=lambda e: e['mtime'], reverse=True)
        total, remaining = sum(e['size'] for e in entries), len(entries)
        protected = {e['name'] for e in entries[:max(1, cfg['min_keep'])]}
        protected.add(keep_name)
        now = time.time()
        for entry in reversed(entries):
            # 超过锁租约必须停止，不能与新的回收执行者重叠。
            if redis_client.get(lock) != token:
                summary['reason'] = '回收锁已失效，停止本次回收'
                break
            if remaining <= cfg['max_count'] and total <= int(cfg['max_total_gb'] * 1024**3):
                break
            if entry['name'] in protected or now-entry['mtime'] < cfg['min_age_minutes']*60:
                continue
            if not cfg['dry_run']:
                if entry.get('remote') and not delete_oss_package(entry['name']):
                    continue
                if entry.get('path'):
                    try:
                        os.remove(entry['path'])
                    except OSError as exc:
                        write_log(f"本地升级包回收失败：{exc}", level='WARNING')
                        continue
            summary['deleted'].append(entry['name'] + ('（dry-run，未删除）' if cfg['dry_run'] else ''))
            summary['freed_bytes'] += entry['size']
            total -= entry['size']
            remaining -= 1
        summary['kept'], summary['total_bytes'] = remaining, total
        summary['reason'] = summary['reason'] or ('已完成回收' if summary['deleted'] else '未达到清理阈值或受保留保护')
        # 仅回收已过期、且不再执行的上传失败副本；从不碰运行中的归档。
        pending = os.path.join(UPGRADE_PACKAGE_DIR, '.pending')
        for path in glob.glob(os.path.join(pending, '*', '*', '*.tar.gz')):
            if now - os.path.getmtime(path) < REDIS_CONFIG['failure_ttl_seconds']:
                continue
            task_id = os.path.relpath(path, pending).split(os.sep)[0]
            status = get_task_status(task_id, include_logs=False)
            if not status and not cfg['dry_run']:
                _remove_local_package(path)
        return summary
    finally:
        redis_client.eval(_COMPARE_DELETE, 1, lock, token)


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

    上传失败返回 None，由交付阶段保留本地包并报告可重试失败。
    """
    if not oss_enabled():
        return None
    if not os.path.exists(local_path):
        write_log(f"OSS 上传跳过：本地文件不存在 {local_path}", level="WARNING", task_id=task_id)
        return None

    uri = package_oss_uri(package_name)
    started = time.time()
    try:
        result = _run_cancellable(
            task_id, [OSS_FILE, '-c', OSS_CONFIG, 'cp', '-f', local_path, uri],
            STORAGE_CONFIG['upload_timeout_seconds'])
    except (BuildCancelled, TaskLeaseLost):
        raise
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

    _raise_if_cancelled(task_id)
    try:
        write_log("OSS 上传完成：{}（{:.1f}s）→ {}".format(package_name, time.time() - started, uri),
                  task_id=task_id)
    except Exception as exc:
        print(f'OSS 上传已完成，写入诊断失败：{exc}')
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


def list_oss_packages(strict=False):
    """列举 OSS 上的升级包；任何失败都返回空列表（不影响本地列表展示）。"""
    if not oss_enabled():
        return []
    base_uri = package_oss_uri("")
    try:
        result = _run_ossutil(["ls", base_uri], STORAGE_CONFIG['oss_timeout_seconds'])
    except subprocess.TimeoutExpired:
        if strict:
            raise
        write_log("OSS 列举超时", level="WARNING")
        return []
    except Exception as exc:
        if strict:
            raise
        write_log(f"OSS 列举异常：{exc}", level="WARNING")
        return []

    if result.returncode != 0:
        if strict:
            raise RuntimeError("OSS 列举失败，停止回收")
        write_log("OSS 列举失败：" + (result.stdout or '')[-300:], level="WARNING")
        return []

    packages = []
    for line in (result.stdout or '').splitlines():
        match = _OSS_LS_LINE_RE.match(line.strip())
        if not match:
            if strict and '.tar.gz' in line:
                raise RuntimeError('OSS 列举格式无法识别，停止回收')
            continue
        name = match.group('uri').rsplit('/', 1)[-1]
        if not name.endswith('.tar.gz') or match.group('uri') != package_oss_uri(name):
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


def _create_upgrade_archive(files_to_pack, upgrade_path, task_log_path, task_id=None):
    """用暂存链接兼容 GNU/BSD tar，仍通过临时包和原子替换发布。"""
    tmp_path = f"{upgrade_path}.{uuid.uuid4().hex[:8]}.tmp"
    names = [os.path.basename(path) for path in files_to_pack]
    if len(set(names)) != len(names):
        raise ValueError("归档文件名冲突，拒绝生成不完整升级包")
    try:
        with tempfile.TemporaryDirectory(prefix='packing-archive-') as stage:
            prefix = os.path.basename(upgrade_path).removesuffix('.tar.gz')
            folder = os.path.join(stage, prefix)
            os.mkdir(folder)
            for src, name in zip(files_to_pack, names):
                os.symlink(os.path.abspath(src), os.path.join(folder, name))
            # -h 解引用暂存链接；包内是普通文件，保持原来的顶层目录结构。
            cmd = ['tar', '-czhf', tmp_path, '-C', stage,
                   *[f'{prefix}/{name}' for name in names]]
            with open(task_log_path, 'a', encoding='utf-8') as f:
                result = _run_cancellable(task_id, cmd, ARCHIVE_TIMEOUT_SECONDS, stdout=f,
                                          env=dict(os.environ, COPYFILE_DISABLE='1'))
            result.check_returncode()
        if not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) == 0:
            raise RuntimeError("打包失败：升级包为空或不存在")
        _raise_if_cancelled(task_id)
        os.replace(tmp_path, upgrade_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


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


class UploadFailed(Exception):
    """镜像归档已完成，但尚未成功交付到 OSS。"""


def _verify_image_exports(task_image_dir, list_path):
    with open(list_path, encoding='utf-8') as f:
        expected = list(dict.fromkeys(normalize_image_ref(line, legacy=True)
                        for line in f if line.strip() and not line.lstrip().startswith('#')))
    manifest = os.path.join(task_image_dir, 'image_exports.tsv')
    exported = {}
    with open(manifest, encoding='utf-8') as f:
        for line in f:
            ref, name = line.rstrip('\n').split('\t')
            if ref in exported or os.path.basename(name) != name or not name.endswith('.tar'):
                raise ValueError('镜像导出清单存在重复或非法文件')
            path = os.path.join(task_image_dir, name)
            if not os.path.isfile(path) or os.path.islink(path) or os.path.getsize(path) == 0:
                raise ValueError('镜像导出缺少有效 tar 文件：' + name)
            exported[ref] = path
    if set(exported) != set(expected) or len(set(exported.values())) != len(exported):
        raise ValueError('镜像导出与请求清单不一致，拒绝生成不完整升级包')
    return [exported[ref] for ref in expected]


def _remove_local_package(path):
    os.remove(path)
    pending = os.path.realpath(os.path.join(UPGRADE_PACKAGE_DIR, '.pending')) + os.sep
    if os.path.realpath(path).startswith(pending):
        # 仅删除已空的 attempt/task 目录，保留 .pending 根目录。
        directory = os.path.dirname(path)
        while os.path.realpath(directory).startswith(pending):
            try:
                os.rmdir(directory)
            except OSError:
                break
            directory = os.path.dirname(directory)


def _package_output_path(task_id, package_name, attempt):
    if not oss_enabled():
        return os.path.join(UPGRADE_PACKAGE_DIR, package_name)
    pending = os.path.join(UPGRADE_PACKAGE_DIR, '.pending', task_id, attempt)
    os.makedirs(pending, exist_ok=True)
    return os.path.join(pending, package_name)


def _complete_package(task_id, path, package_name, image_count, object_name=None):
    _raise_if_cancelled(task_id)
    size = os.path.getsize(path)
    owner = get_task_owner(task_id)
    worker_id = owner.rsplit(':', 1)[0] if owner else None
    # 每个任务/尝试使用独立对象，旧任务和同版本对的包不会覆盖本次交付。
    object_name = object_name or (os.path.basename(path).removesuffix('.tar.gz') +
                                 '_' + uuid.uuid4().hex[:12] + '.tar.gz')
    update_task_status(task_id, {
        'package_path': path, 'package_name': package_name, 'oss_object_name': object_name,
        'artifact_worker_id': worker_id, 'image_count': image_count,
        'package_size_mb': round(size / 1024 / 1024, 2),
        'download_url': None, 'download_expire_at': None, 'oss_uri': None,
    })
    uri = None
    if oss_enabled():
        update_progress(task_id, 93, '上传升级包到 OSS')
        for attempt in range(3):
            _raise_if_cancelled(task_id)
            uri = upload_package_to_oss(path, object_name, task_id=task_id)
            if uri:
                break
            if attempt < 2:
                write_log(f'OSS 上传失败，重试 {attempt + 2}/3（保留已完成归档）',
                          level='WARNING', task_id=task_id)
                time.sleep(0.5)
        _raise_if_cancelled(task_id)
        if not uri:
            update_task_status(task_id, {
                'status': 'error', 'complete': True, 'error': True, 'upload_failed': True,
                'upload_retry_available': bool(worker_id),
                'message': '升级包已生成，但 OSS 上传失败；可在原 worker 在线时重试上传',
            }, ttl_seconds=REDIS_CONFIG['failure_ttl_seconds'])
            raise UploadFailed('OSS 上传失败，已保留本地归档')
    _raise_if_cancelled(task_id)
    message = f'构建成功！生成TAR.GZ升级包（{image_count}个镜像+2个文件）'
    update_task_status(task_id, {
        'status': 'complete', 'percent': 100, 'message': message,
        'complete': True, 'error': False, 'cancelled': False,
        'upload_failed': False, 'upload_retry_available': False,
        'download_url': f'/download/{task_id}', 'oss_uri': uri,
        'package_format': 'tar.gz',
        'download_expire_at': int(time.time()) + REDIS_CONFIG['download_ttl_seconds'],
    }, ttl_seconds=REDIS_CONFIG['success_ttl_seconds'])
    # 先发布持久化完成状态，再删除本地副本；Redis 写入失败时仍可重试交付。
    if uri and STORAGE_CONFIG.get('delete_local_after_upload'):
        try:
            _remove_local_package(path)
        except OSError as exc:
            print(f'删除已上传的本地副本失败：{exc}')
    try:
        write_log(message, task_id=task_id)
    except Exception as exc:
        print(f'交付已完成，写入完成日志失败：{exc}')
    try:
        cleanup_old_packages(keep_name=object_name if uri else package_name)
    except Exception as exc:
        print(f'交付已完成，升级包回收失败：{exc}')


def run_upload_retry(task_id):
    status = get_task_status(task_id, include_logs=False)
    path = (status or {}).get('package_path') or ''
    pending_root = os.path.realpath(os.path.join(UPGRADE_PACKAGE_DIR, '.pending')) + os.sep
    if not os.path.realpath(path).startswith(pending_root) or not os.path.isfile(path):
        update_task_status(task_id, {'upload_retry_available': False})
        raise RuntimeError('保留的升级包已丢失，请重新构建')
    try:
        _complete_package(task_id, path, status['package_name'], status.get('image_count', 0),
                          status.get('oss_object_name'))
    except UploadFailed:
        pass  # 交付阶段已记录可重试失败
    except BuildCancelled:
        mark_task_cancelled(task_id)
    finally:
        clear_task_cancel(task_id)
        if not get_task_owner(task_id):
            release_build_slot(task_id)


def run_build_task(task_id, current_version, target_version, channel_key=DEFAULT_CHANNEL):
    """核心构建任务：执行构建list脚本→拉取镜像→打包TAR.GZ格式升级包"""
    if channel_key not in CHANNELS:
        channel_key = DEFAULT_CHANNEL
    channel = CHANNELS[channel_key]
    attempt_id = uuid.uuid4().hex[:12]
    try:
        # 创建任务专属目录（用版本+任务ID命名，确保唯一性，避免文件冲突）
        task_dir_name = f"{current_version}_to_{target_version}_{task_id}_{attempt_id}"
        task_image_dir = os.path.join(IMAGE_TAR_ROOT, task_dir_name)
        # 确保任务目录存在（不存在则创建，存在则清空历史残留）
        if os.path.exists(task_image_dir):
            shutil.rmtree(task_image_dir)
        os.makedirs(task_image_dir, exist_ok=True)

        # 为每个任务生成独立的镜像列表文件路径，避免并发竞争
        task_patch_list_path = os.path.join(LATEST_LIST_DIR, f"patch_image_tag_list_{task_id}_{attempt_id}.txt")

        # 创建任务专属日志文件（记录脚本详细输出，避免实时刷屏）
        task_log_path = os.path.join(LOG_DIR, f"task_{task_id}_{attempt_id}.log")

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
                result = _run_cancellable(
                    task_id, ['/bin/bash', PATCH_LIST_SCRIPT_PATH, current_version,
                              target_version, task_patch_list_path],
                    600, stdout=f, env=script_env)
                result.check_returncode()

            
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

            tar_files = _verify_image_exports(task_image_dir, task_patch_list_path)

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
            upgrade_path = _package_output_path(task_id, upgrade_package, attempt_id)

            # 打包TAR.GZ升级包（两步 transform：先去路径前缀，再加目录前缀）
            _create_upgrade_archive(files_to_pack, upgrade_path, task_log_path, task_id=task_id)
            
            update_progress(task_id, 90, "验证升级包完整性")
            # 验证包大小
            package_size = os.path.getsize(upgrade_path)
            write_log(f"升级包大小：{package_size/1024/1024:.2f}MB", task_id=task_id)

            _complete_package(task_id, upgrade_path, upgrade_package, len(tar_files))

        except TaskLeaseLost:
            raise
        except UploadFailed:
            pass  # 本地归档保留，交付阶段已记录失败状态
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
        clear_task_cancel(task_id)
        if not get_task_owner(task_id):
            release_build_slot(task_id)

def run_build_task_v7(task_id, images):
    """自定义镜像构建任务：按用户给定镜像清单→拉取镜像→打包TAR.GZ格式升级包"""
    attempt_id = uuid.uuid4().hex[:12]
    try:
        task_dir_name = f"v7_{task_id}_{attempt_id}"
        task_image_dir = os.path.join(IMAGE_TAR_ROOT, task_dir_name)
        task_patch_list_path = os.path.join(task_image_dir, "patch_image_tag_list.txt")
        if os.path.exists(task_image_dir):
            shutil.rmtree(task_image_dir)
        os.makedirs(task_image_dir, exist_ok=True)

        task_log_path = os.path.join(LOG_DIR, f"task_{task_id}_{attempt_id}.log")

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
                    # 客户定制升级脚本会补默认仓库；保留原有短名清单契约。
                    package_ref = image.removeprefix(DEFAULT_REPOSITORY)
                    f.write(f"{package_ref}\n")
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

            tar_files = _verify_image_exports(task_image_dir, task_patch_list_path)

            update_progress(task_id, 60, f"镜像拉取完成，共拉取 {len(tar_files)} 个")
            write_log(f"镜像拉取成功！共拉取{len(tar_files)}个镜像", task_id=task_id)

            update_progress(task_id, 70, "准备打包升级包")
            _raise_if_cancelled(task_id)

            if not os.path.exists(UPGRADE_SCRIPT_PATH) or not os.path.isfile(UPGRADE_SCRIPT_PATH):
                raise Exception(f"升级脚本不存在：{UPGRADE_SCRIPT_PATH}")
            files_to_pack = tar_files + [task_patch_list_path, UPGRADE_SCRIPT_PATH]

            update_progress(task_id, 75, "开始打包TAR.GZ升级包")
            upgrade_package = f"deepflow_patch_custom_{task_id}.tar.gz"
            upgrade_path = _package_output_path(task_id, upgrade_package, attempt_id)

            _create_upgrade_archive(files_to_pack, upgrade_path, task_log_path, task_id=task_id)

            update_progress(task_id, 90, "验证升级包完整性")
            package_size = os.path.getsize(upgrade_path)
            write_log(f"升级包大小：{package_size/1024/1024:.2f}MB", task_id=task_id)

            _complete_package(task_id, upgrade_path, upgrade_package, len(tar_files))

        except TaskLeaseLost:
            raise
        except UploadFailed:
            pass  # 本地归档保留，交付阶段已记录失败状态
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
        clear_task_cancel(task_id)
        if not get_task_owner(task_id):
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
    remote_exists = False
    if oss_enabled():
        try:
            remote_exists = _run_ossutil(['stat', package_oss_uri(filename)],
                                         STORAGE_CONFIG['oss_timeout_seconds']).returncode == 0
        except Exception:
            pass
    if remote_exists:
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
            try:
                images = normalize_images(images)
            except ValueError as exc:
                return jsonify({'success': False, 'message': str(exc)}), 400
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


_RETRY_UPLOAD = """-- retry_upload_v1
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local task = cjson.decode(raw)
if not task.complete or not task.upload_failed or not task.upload_retry_available
   or task.cancelled or task.artifact_worker_id ~= ARGV[1] then return -1 end
if redis.call('EXISTS', KEYS[2]) == 0 then return -2 end
if redis.call('EXISTS', KEYS[7]) == 1 then return -3 end
task.status = 'queued'
task.complete = false
task.error = false
task.percent = 90
task.message = '等待原 worker 重试上传，无需重新拉取镜像'
task.download_url = cjson.null
task.download_expire_at = cjson.null
task.updated_at = tonumber(ARGV[3])
redis.call('DEL', KEYS[3])
redis.call('PERSIST', KEYS[5])
redis.call('PERSIST', KEYS[6])
redis.call('SET', KEYS[1], cjson.encode(task))
redis.call('LPUSH', KEYS[4], ARGV[2])
return 1
"""


@app.route('/build/<task_id>/retry-upload', methods=['POST'])
def api_retry_upload(task_id):
    status = get_task_status(task_id, include_logs=False)
    if not status:
        return jsonify(success=False, message='任务不存在或已过期'), 404
    if not oss_enabled():
        return jsonify(success=False, message='当前未启用 OSS 存储'), 409
    worker_id = status.get('artifact_worker_id')
    if not worker_id:
        return jsonify(success=False, message='该任务无法重试上传'), 409
    payload = json.dumps({'task_id': task_id, 'task_type': status['task_type'],
                          'action': 'retry_upload'})
    result = redis_client.eval(_RETRY_UPLOAD, 7,
        task_meta_key(task_id), worker_heartbeat_key(worker_id), task_cancel_key(task_id),
        build_upload_queue_key(worker_id), task_logs_key(task_id), task_log_seq_key(task_id),
        task_slot_key(task_id), worker_id, payload, int(time.time()))
    if result == 0:
        return jsonify(success=False, message='任务不存在或已过期'), 404
    if result == -2:
        return jsonify(success=False, message='原 worker 已离线，本地升级包不可恢复，请重新构建'), 409
    if result == -3:
        return jsonify(success=False, message='原构建正在结束，请稍后重试上传'), 409
    if result != 1:
        return jsonify(success=False, message='该任务无法重试上传或已经在执行'), 409
    return jsonify(success=True, task_id=task_id)


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
            try:
                images = normalize_images(images)
            except ValueError as exc:
                return jsonify({'success': False, 'message': str(exc)}), 400
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
    if oss_enabled() and task_status.get('oss_uri'):
        url = presign_package_url(task_status.get('oss_object_name') or task_status['package_name'])
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
