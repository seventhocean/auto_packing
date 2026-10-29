import json
import os
import socket
import threading
import time
import traceback
import uuid
import redis

from app import (
    DEFAULT_CHANNEL,
    REDIS_CONFIG,
    TaskLeaseLost,
    _COMPARE_DELETE,
    assert_task_owned,
    build_processing_key,
    build_queue_key,
    build_upload_queue_key,
    execution_context,
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

# One Redis transaction checks liveness, fences the old attempt, and moves the
# messages. If a reply is lost, the next reclaim sees an empty source list.
_RECLAIM_PROCESSING = """-- reclaim_processing_v1
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
if redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
local count = redis.call('LLEN', KEYS[3])
for i = 1, count do
  local raw = redis.call('RPOP', KEYS[3])
  local valid, payload = pcall(cjson.decode, raw)
  if valid and type(payload) == 'table' and type(payload.task_id) == 'string' then
    local slot = ARGV[3] .. ':task:' .. payload.task_id .. ':slot'
    local owner = redis.call('GET', slot)
    if owner == '1' or owner == 'legacy' or
       (owner and string.sub(owner, 1, string.len(ARGV[2]) + 1) == ARGV[2] .. ':') then
      redis.call('DEL', slot)
    end
  end
  redis.call('LPUSH', KEYS[4], raw)
end
return count
"""


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

            moved = redis_client.eval(
                _RECLAIM_PROCESSING, 4, RECLAIM_LOCK_KEY, heartbeat_key,
                processing_key, build_queue_key(), WORKER_ID, worker_id,
                REDIS_CONFIG['key_prefix'])
            if moved:
                reclaimed_count += 1
                write_log("检测到失活 worker {}，已回收 {} 个任务".format(worker_id, moved), level="WARNING")

        # An upload retry is pinned to the worker holding its local artifact.
        # If that worker dies before dequeue, move the message as well so another
        # worker can terminate it with an explicit artifact-unavailable error.
        upload_pattern = "{}:build:upload:*".format(REDIS_CONFIG['key_prefix'])
        for upload_key in redis_client.scan_iter(match=upload_pattern):
            worker_id = upload_key.split(':')[-1]
            moved = redis_client.eval(
                _RECLAIM_PROCESSING, 4, RECLAIM_LOCK_KEY, worker_heartbeat_key(worker_id),
                upload_key, build_queue_key(), WORKER_ID, worker_id,
                REDIS_CONFIG['key_prefix'])
            if moved:
                reclaimed_count += 1

        if reclaimed_count > 0:
            reconcile_build_counter()
        return reclaimed_count
    finally:
        try:
            # 锁过期后可能已被其他 worker 获取；只删除自己持有的锁。
            redis_client.eval(_COMPARE_DELETE, 1, RECLAIM_LOCK_KEY, WORKER_ID)
        except Exception:
            pass


def reclaim_loop():
    while True:
        try:
            reclaim_stale_tasks()
        except Exception as exc:
            write_log("回收失活任务失败：{}".format(str(exc)), level="WARNING")
        time.sleep(RECLAIM_INTERVAL_SECONDS)


def process_task(payload, raw_payload=None):
    task_id = payload["task_id"]
    task_type = payload["task_type"]
    owner = "{}:{}".format(WORKER_ID, uuid.uuid4().hex)

    # Waiting is not a build failure. In particular a rolling-update worker may
    # have prefetched a task while all configured slots are still occupied.
    while True:
        try:
            status = get_task_status(task_id, include_logs=False)
            if not status or status.get('complete'):
                return
            if is_build_cancelled(task_id):
                mark_task_cancelled(task_id)
                return
            if try_acquire_build_slot(
                    task_id, owner, PROCESSING_QUEUE_KEY if raw_payload is not None else None,
                    raw_payload):
                break
            if redis_client.exists(task_slot_key(task_id)):
                # Another attempt owns this task; acknowledge the duplicate.
                return
        except TaskLeaseLost:
            return
        except redis.RedisError:
            # Keep the processing record until a decision can be made. Removing
            # it during an outage would leave a queued task with no queue item.
            pass
        time.sleep(2)

    with execution_context(task_id, owner):
        try:
            assert_task_owned(task_id)
            status = get_task_status(task_id, include_logs=False)
            if not status or status.get('complete'):
                return
            if is_build_cancelled(task_id):
                mark_task_cancelled(task_id)
                return
            update_task_status(task_id, {
                "status": "queued",
                "percent": 0,
                "message": "任务已分配给 worker，准备开始执行"
            })
            if payload.get('action') == 'retry_upload':
                if status.get('artifact_worker_id') != WORKER_ID:
                    update_task_status(task_id, {
                        "status": "error", "complete": True, "error": True,
                        "upload_retry_available": False,
                        "message": "原构建 worker 已失活，本地升级包不可恢复，请重新构建"
                    }, ttl_seconds=REDIS_CONFIG['failure_ttl_seconds'])
                    return
                from app import run_upload_retry
                run_upload_retry(task_id)
            elif task_type == "v7":
                run_build_task_v7(task_id, payload["images"])
            else:
                # Old queue messages may not carry a channel yet.
                run_build_task(
                    task_id, payload["current_version"], payload["target_version"],
                    payload.get("channel") or DEFAULT_CHANNEL,
                )
        except TaskLeaseLost:
            write_log("任务[{}]执行权已被回收，停止旧执行尝试".format(task_id), level="WARNING")
        except Exception as exc:
            # Keep error updates fenced too; the main loop has no execution
            # context after this scope exits and must not write task metadata.
            while True:
                try:
                    assert_task_owned(task_id)
                    status = get_task_status(task_id, include_logs=False)
                    if not (status and status.get('complete') and not status.get('error')):
                        update_task_status(task_id, {
                            "status": "error", "message": "构建失败：{}".format(exc),
                            "complete": True, "error": True
                        }, ttl_seconds=REDIS_CONFIG['failure_ttl_seconds'])
                    break
                except TaskLeaseLost:
                    break
                except redis.RedisError:
                    # Do not acknowledge an unfinished task just because Redis
                    # is unavailable while reporting its terminal state.
                    time.sleep(2)
            raise
        finally:
            release_build_slot(task_id, owner)


def main():
    write_log("构建 worker 启动成功，worker_id={}".format(WORKER_ID))
    redis_client.set(HEARTBEAT_KEY, str(int(time.time())), ex=HEARTBEAT_TTL_SECONDS)
    try:
        reclaim_stale_tasks()
    except Exception as exc:
        write_log("启动时回收失活任务失败：{}".format(str(exc)), level="WARNING")
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=reclaim_loop, daemon=True).start()

    while True:
        try:
            if not redis_client.exists(HEARTBEAT_KEY):
                time.sleep(1)
                continue
            raw_payload = redis_client.rpoplpush(build_upload_queue_key(WORKER_ID), PROCESSING_QUEUE_KEY)
            if not raw_payload:
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
            process_task(payload, raw_payload=raw_payload)
        except Exception as exc:
            write_log(f"worker 执行任务[{task_id}]失败：{str(exc)}", level="ERROR")
            traceback.print_exc()
        finally:
            redis_client.lrem(PROCESSING_QUEUE_KEY, 1, raw_payload)


if __name__ == "__main__":
    main()
