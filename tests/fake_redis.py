"""Thread-safe Redis test double; no network or production fallback.

Lua markers below mirror the small atomic operations used by this project.
Unknown scripts fail so tests cannot silently omit new Redis behavior.
"""
import fnmatch
import json
import threading
import time


class FakeRedis:
    def __init__(self, *args, **kwargs):
        self.store = {}
        self.expires = {}
        self.lock = threading.RLock()

    def _purge(self):
        for key, deadline in list(self.expires.items()):
            if deadline <= time.monotonic():
                self.store.pop(key, None)
                self.expires.pop(key, None)

    def ping(self):
        return True

    def set(self, name, value, nx=False, ex=None, xx=False):
        with self.lock:
            self._purge()
            if (nx and name in self.store) or (xx and name not in self.store):
                return None
            self.store[name] = str(value)
            self.expires.pop(name, None)
            if ex is not None:
                self.expires[name] = time.monotonic() + float(ex)
            return True

    def get(self, name):
        with self.lock:
            self._purge()
            return self.store.get(name)

    def delete(self, *names):
        with self.lock:
            self._purge()
            count = 0
            for name in names:
                count += int(name in self.store)
                self.store.pop(name, None)
                self.expires.pop(name, None)
            return count

    def exists(self, name):
        with self.lock:
            self._purge()
            return int(name in self.store)

    def scan_iter(self, match=None):
        with self.lock:
            self._purge()
            keys = [key for key in self.store if fnmatch.fnmatch(key, match or '*')]
        return iter(keys)

    def keys(self, pattern='*'):
        return list(self.scan_iter(match=pattern))

    def lpush(self, name, *values):
        with self.lock:
            self._purge()
            target = self.store.setdefault(name, [])
            for value in values:
                target.insert(0, value)
            return len(target)

    def rpush(self, name, *values):
        with self.lock:
            self._purge()
            target = self.store.setdefault(name, [])
            target.extend(values)
            return len(target)

    @staticmethod
    def _range(values, start, end):
        end = len(values) - 1 if end == -1 else end
        if start < 0:
            start = max(0, len(values) + start)
        if end < 0:
            end += len(values)
        return values[start:end + 1] if end >= start else []

    def lrange(self, name, start, end):
        with self.lock:
            self._purge()
            return list(self._range(self.store.get(name, []), start, end))

    def ltrim(self, name, start, end):
        with self.lock:
            self._purge()
            if name in self.store:
                values = self._range(self.store[name], start, end)
                if values:
                    self.store[name] = values
                else:
                    self.delete(name)
            return True

    def llen(self, name):
        with self.lock:
            self._purge()
            return len(self.store.get(name, []))

    def lrem(self, name, count, value):
        with self.lock:
            self._purge()
            values = list(self.store.get(name, []))
            reverse = count < 0
            if reverse:
                values.reverse()
            remaining, removed = [], 0
            for item in values:
                if item == value and (count == 0 or removed < abs(count)):
                    removed += 1
                else:
                    remaining.append(item)
            if reverse:
                remaining.reverse()
            if remaining:
                self.store[name] = remaining
            else:
                self.delete(name)
            return removed

    def rpop(self, name):
        with self.lock:
            self._purge()
            values = self.store.get(name, [])
            if not values:
                return None
            value = values.pop()
            if not values:
                self.delete(name)
            return value

    def rpoplpush(self, source, destination):
        with self.lock:
            value = self.rpop(source)
            if value is not None:
                self.lpush(destination, value)
            return value

    def brpoplpush(self, source, destination, timeout=0):
        deadline = time.monotonic() + timeout if timeout else float('inf')
        while True:
            value = self.rpoplpush(source, destination)
            if value is not None or time.monotonic() >= deadline:
                return value
            time.sleep(0.005)

    def expire(self, name, seconds):
        with self.lock:
            self._purge()
            if name not in self.store:
                return False
            self.expires[name] = time.monotonic() + float(seconds)
            return True

    def ttl(self, name):
        with self.lock:
            self._purge()
            if name not in self.store:
                return -2
            return max(0, int(self.expires[name] - time.monotonic())) if name in self.expires else -1

    def persist(self, name):
        with self.lock:
            self._purge()
            return int(self.expires.pop(name, None) is not None)

    def incr(self, name):
        with self.lock:
            value = int(self.get(name) or 0) + 1
            self.store[name] = str(value)
            return value

    def decr(self, name):
        with self.lock:
            value = int(self.get(name) or 0) - 1
            self.store[name] = str(value)
            return value

    def eval(self, script, number_of_keys, *values):
        with self.lock:
            self._purge()
            keys, args = list(values[:number_of_keys]), list(values[number_of_keys:])
            marker = script.splitlines()[0].strip()
            if marker == '-- write_task_status_v1':
                if args[0] and self.get(keys[0]) != args[0]:
                    return -1
                self.set(keys[1], args[1])
                if int(args[2]) > 0:
                    for key in keys[1:]:
                        self.expire(key, int(args[2]))
                else:
                    for key in keys[2:]:
                        self.persist(key)
                return 1
            if marker == '-- append_task_log_v1':
                if args[0] and self.get(keys[0]) != args[0]:
                    return -1
                if not self.exists(keys[1]):
                    return 0
                entry = json.loads(args[1])
                entry['seq'] = self.incr(keys[2])
                self.rpush(keys[3], json.dumps(entry, ensure_ascii=False))
                self.ltrim(keys[3], -int(args[2]), -1)
                ttl = self.ttl(keys[1])
                if ttl >= 0:
                    ttl = max(ttl, 1)
                    self.expire(keys[2], ttl)
                    self.expire(keys[3], ttl)
                else:
                    self.persist(keys[2])
                    self.persist(keys[3])
                return entry['seq']
            if marker == '-- task_cleanup_v1':
                if args[0] and self.get(keys[0]) != args[0]:
                    return -1
                for key in keys[1:]:
                    self.expire(key, int(args[1]))
                return 1
            if marker == '-- compare_delete_v1':
                return self.delete(keys[0]) if self.get(keys[0]) == args[0] else 0
            if marker == '-- clear_task_cancel_v1':
                if args[0] and self.get(keys[0]) != args[0]:
                    return 0
                return self.delete(keys[1])
            if marker == '-- acquire_build_slot_v1':
                if len(keys) == 3 and args[4] not in self.lrange(keys[2], 0, -1):
                    return -1
                owner = self.get(keys[0])
                if owner is not None:
                    return int(owner == args[0])
                slots = self.keys(args[3])
                if len(slots) >= int(args[1]):
                    return 0
                self.set(keys[0], args[0], ex=int(args[2]))
                self.set(keys[1], len(slots) + 1)
                return 1
            if marker == '-- reclaim_processing_v1':
                if self.get(keys[0]) != args[0] or self.exists(keys[1]):
                    return 0
                count = self.llen(keys[2])
                for _ in range(count):
                    raw = self.rpop(keys[2])
                    try:
                        payload = json.loads(raw)
                    except (TypeError, ValueError):
                        payload = None
                    if isinstance(payload, dict) and isinstance(payload.get('task_id'), str):
                        slot = '{}:task:{}:slot'.format(args[2], payload['task_id'])
                        owner = self.get(slot)
                        if owner in ('1', 'legacy') or (owner and owner.startswith(args[1] + ':')):
                            self.delete(slot)
                    self.lpush(keys[3], raw)
                return count
            if marker == '-- retry_upload_v1':
                raw = self.get(keys[0])
                if raw is None:
                    return 0
                status = json.loads(raw)
                if not (status.get('complete') and status.get('upload_failed')
                        and status.get('upload_retry_available') and not status.get('cancelled')
                        and status.get('artifact_worker_id') == args[0]):
                    return -1
                if not self.exists(keys[1]):
                    return -2
                if self.exists(keys[6]):
                    return -3
                self.delete(keys[2])
                status.update({
                    'status': 'queued', 'complete': False, 'error': False, 'percent': 90,
                    'message': '等待原 worker 重试上传，无需重新拉取镜像',
                    'download_url': None, 'download_expire_at': None,
                    'updated_at': int(args[2]),
                })
                self.set(keys[0], json.dumps(status, ensure_ascii=False))
                self.persist(keys[4])
                self.persist(keys[5])
                self.lpush(keys[3], args[1])
                return 1
            raise AssertionError('Unimplemented Redis script: ' + marker)
