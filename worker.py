import json
import os
import socket
import threading
import time
import traceback
import uuid

from app import (
    DEFAULT_CHANNEL,
    REDIS_CONFIG,
    build_processing_key,
    build_queue_key,
    get_task_status,
    is_build_cancelled,
    mark_task_cancelled,
    reconcile_build_counter,
    release_build_slot,
    redis_client,
    run_build_task,
    run_build_task_v7,
    task_slot_key,
    try_acquire_build_slot,
    update_task_status,
    worker_heartbeat_key,
    write_log,
)

WORKER_ID = "{}-{}-{}".format(socket.gethostname(), os.getpid(), uuid.uuid4().hex[:8])
PROCESSING_QUEUE_KEY = build_processing_key(WORKER_ID)
HEARTBEAT_KEY = worker_heartbeat_key(WORKER_ID)

# 心跳 TTL 过短时，Redis 短暂抖动就可能导致运行中的任务被其他 worker 误回收并重复构建。
HEARTBEAT_INTERVAL_SECONDS = int(os.environ.get("WORKER_HEARTBEAT_INTERVAL_SECONDS", 10))
HEARTBEAT_TTL_SECONDS = int(os.environ.get("WORKER_HEARTBEAT_TTL_SECONDS", 60))
RECLAIM_INTERVAL_SECONDS = int(os.environ.get("WORKER_RECLAIM_INTERVAL_SECONDS", 30))
RECLAIM_LOCK_KEY = "{}:build:reclaim:lock".format(REDIS_CONFIG["key_prefix"])
RECLAIM_LOCK_TTL_SECONDS = 120


def heartbeat_loop():
    while True:
        try:
            redis_client.set(HEARTBEAT_KEY, str(int(time.time())), ex=HEARTBEAT_TTL_SECONDS)
        except Exception as exc:
            # 心跳线程不能因为一次 Redis 抖动就退出，否则本 worker 的任务会被误回收
            write_log("写入 worker 心跳失败：{}".format(str(exc)), level="WARNING")
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)


def reclaim_stale_tasks():
    # 多 worker 副本会同时执行回收；加一个短锁，避免同一批任务被重复入队。
    try:
        acquired = redis_client.set(RECLAIM_LOCK_KEY, WORKER_ID, nx=True, ex=RECLAIM_LOCK_TTL_SECONDS)
    except Exception as exc:
        write_log("获取回收锁失败：{}".format(str(exc)), level="WARNING")
        return 0
    if not acquired:
        return 0

    try:
        processing_pattern = "{}:build:processing:*".format(REDIS_CONFIG["key_prefix"])
        reclaimed_count = 0
        for processing_key in redis_client.scan_iter(match=processing_pattern):
            worker_id = processing_key.split(":")[-1]
            heartbeat_key = worker_heartbeat_key(worker_id)
            if redis_client.exists(heartbeat_key):
                continue

            pending_tasks = redis_client.lrange(processing_key, 0, -1)
            if not pending_tasks:
                redis_client.delete(processing_key)
                continue

            for raw_payload in reversed(pending_tasks):
                # 失活 worker 的 slot 不会自己释放；先清理，否则 max_concurrent_tasks=1
                # 时其他任务会一直抢不到槽位，直到 6 小时 TTL 到期。
                try:
                    payload = json.loads(raw_payload)
                    task_id = payload.get("task_id") if isinstance(payload, dict) else None
                    if task_id:
                        redis_client.delete(task_slot_key(task_id))
                except (TypeError, ValueError):
                    pass
                redis_client.rpush(build_queue_key(), raw_payload)
            redis_client.delete(processing_key)
            reclaimed_count += 1
            write_log("检测到失活 worker {}，已回收 {} 个任务".format(worker_id, len(pending_tasks)), level="WARNING")

        if reclaimed_count > 0:
            reconcile_build_counter()
        return reclaimed_count
    finally:
        try:
            # 锁过期后可能已被其他 worker 获取；只删除自己持有的锁。
            if redis_client.get(RECLAIM_LOCK_KEY) == WORKER_ID:
                redis_client.delete(RECLAIM_LOCK_KEY)
        except Exception:
            pass


def reclaim_loop():
    while True:
        try:
            reclaim_stale_tasks()
        except Exception as exc:
            write_log("回收失活任务失败：{}".format(str(exc)), level="WARNING")
        time.sleep(RECLAIM_INTERVAL_SECONDS)


def process_task(payload):
    task_id = payload["task_id"]
    task_type = payload["task_type"]
    run_started = False
    wait_start = time.time()
    max_wait_seconds = 600  # 等待槽位最长 10 分钟

    if is_build_cancelled(task_id):
        mark_task_cancelled(task_id)
        write_log("任务[{}]在开始前已被取消".format(task_id), level="WARNING")
        return

    while not try_acquire_build_slot(task_id):
        if is_build_cancelled(task_id):
            mark_task_cancelled(task_id)
            write_log("任务[{}]在排队期间被取消".format(task_id), level="WARNING")
            return
        if time.time() - wait_start > max_wait_seconds:
            update_task_status(task_id, {
                "status": "error",
                "message": "等待构建槽位超时（{}秒），请稍后重试".format(max_wait_seconds),
                "complete": True,
                "error": True
            }, ttl_seconds=REDIS_CONFIG["failure_ttl_seconds"])
            write_log("任务[{}]等待槽位超时，已标记失败".format(task_id), level="ERROR")
            return
        update_task_status(task_id, {
            "status": "queued",
            "message": "等待可用构建 worker，请稍后"
        })
        time.sleep(2)

    try:
        if is_build_cancelled(task_id):
            mark_task_cancelled(task_id)
            write_log("任务[{}]获得槽位前已被取消".format(task_id), level="WARNING")
            return
        update_task_status(task_id, {
            "status": "queued",
            "message": "任务已分配给 worker，准备开始执行"
        })
        run_started = True
        if task_type == "v7":
            run_build_task_v7(task_id, payload["images"])
        else:
            # 老任务（发布前入队）没有 channel 字段，回退到默认版本线
            run_build_task(
                task_id,
                payload["current_version"],
                payload["target_version"],
                payload.get("channel") or DEFAULT_CHANNEL,
            )
    finally:
        if not run_started:
            release_build_slot(task_id)


def main():
    write_log("构建 worker 启动成功，worker_id={}".format(WORKER_ID))
    try:
        reclaim_stale_tasks()
    except Exception as exc:
        write_log("启动时回收失活任务失败：{}".format(str(exc)), level="WARNING")
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=reclaim_loop, daemon=True).start()

    while True:
        try:
            raw_payload = redis_client.brpoplpush(build_queue_key(), PROCESSING_QUEUE_KEY, timeout=5)
        except Exception as exc:
            # Redis 短暂不可用时继续重试，避免 worker 进程直接退出
            write_log("读取构建队列失败：{}".format(str(exc)), level="WARNING")
            time.sleep(5)
            continue
        if not raw_payload:
            continue

        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            write_log("跳过无法解析的队列消息：{}".format(str(raw_payload)[:200]), level="WARNING")
            redis_client.lrem(PROCESSING_QUEUE_KEY, 1, raw_payload)
            continue

        task_id = payload.get("task_id", "unknown")

        try:
            task_status = get_task_status(task_id, include_logs=False)
            if not task_status:
                write_log(f"跳过未知任务：{task_id}", level="WARNING")
                redis_client.lrem(PROCESSING_QUEUE_KEY, 1, raw_payload)
                continue
            if task_status.get('complete'):
                write_log(f"跳过已结束任务：{task_id}", level="WARNING")
                redis_client.lrem(PROCESSING_QUEUE_KEY, 1, raw_payload)
                continue
            process_task(payload)
        except Exception as exc:
            write_log(f"worker 执行任务[{task_id}]失败：{str(exc)}", level="ERROR", task_id=task_id)
            traceback.print_exc()
            if get_task_status(task_id, include_logs=False):
                update_task_status(task_id, {
                    "status": "error",
                    "message": f"构建失败：{str(exc)}",
                    "complete": True,
                    "error": True
                }, ttl_seconds=REDIS_CONFIG["failure_ttl_seconds"])
        finally:
            redis_client.lrem(PROCESSING_QUEUE_KEY, 1, raw_payload)


if __name__ == "__main__":
    main()
