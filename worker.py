import json
import os
import socket
import threading
import time
import traceback
import uuid

from app import (
    REDIS_CONFIG,
    build_processing_key,
    build_queue_key,
    get_task_status,
    reconcile_build_counter,
    release_build_slot,
    redis_client,
    run_build_task,
    run_build_task_v7,
    try_acquire_build_slot,
    update_task_status,
    worker_heartbeat_key,
    write_log,
)

WORKER_ID = "{}-{}-{}".format(socket.gethostname(), os.getpid(), uuid.uuid4().hex[:8])
PROCESSING_QUEUE_KEY = build_processing_key(WORKER_ID)
HEARTBEAT_KEY = worker_heartbeat_key(WORKER_ID)


def heartbeat_loop():
    while True:
        redis_client.set(HEARTBEAT_KEY, str(int(time.time())), ex=15)
        time.sleep(5)


def reclaim_stale_tasks():
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
            redis_client.rpush(build_queue_key(), raw_payload)
        redis_client.delete(processing_key)
        reclaimed_count += 1
        write_log("检测到失活 worker {}，已回收 {} 个任务".format(worker_id, len(pending_tasks)), level="WARNING")

    if reclaimed_count > 0:
        reconcile_build_counter()


def reclaim_loop():
    while True:
        reclaim_stale_tasks()
        time.sleep(30)


def process_task(payload):
    task_id = payload["task_id"]
    task_type = payload["task_type"]
    run_started = False
    wait_start = time.time()
    max_wait_seconds = 600  # 等待槽位最长 10 分钟

    while not try_acquire_build_slot(task_id):
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
        update_task_status(task_id, {
            "status": "queued",
            "message": "任务已分配给 worker，准备开始执行"
        })
        run_started = True
        if task_type == "v7":
            run_build_task_v7(task_id, payload["images"])
        else:
            run_build_task(task_id, payload["current_version"], payload["target_version"])
    finally:
        if not run_started:
            release_build_slot(task_id)


def main():
    write_log("构建 worker 启动成功，worker_id={}".format(WORKER_ID))
    reclaim_stale_tasks()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=reclaim_loop, daemon=True).start()

    while True:
        raw_payload = redis_client.brpoplpush(build_queue_key(), PROCESSING_QUEUE_KEY, timeout=5)
        if not raw_payload:
            continue

        payload = json.loads(raw_payload)
        task_id = payload.get("task_id", "unknown")

        try:
            task_status = get_task_status(task_id, include_logs=False)
            if not task_status:
                write_log(f"跳过未知任务：{task_id}", level="WARNING")
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
