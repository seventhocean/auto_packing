# -*- coding: utf-8 -*-
"""app.py 关键逻辑的回归测试（不依赖真实 Redis）。

运行方式：
    python -m unittest tests.test_app_logic -v
"""
import fnmatch
import os
import shutil
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import redis  # noqa: E402


class FakeRedis:
    """只实现 app.py 用到的最小 Redis 接口。"""

    def __init__(self, *args, **kwargs):
        self.store = {}

    def ping(self):
        return True

    def set(self, name, value, nx=False, ex=None):
        if nx and name in self.store:
            return None
        self.store[name] = value
        return True

    def get(self, name):
        return self.store.get(name)

    def delete(self, name):
        return 1 if self.store.pop(name, None) is not None else 0

    def exists(self, name):
        return 1 if name in self.store else 0

    def scan_iter(self, match=None):
        for key in list(self.store):
            if fnmatch.fnmatch(key, match or '*'):
                yield key

    def lpush(self, name, value):
        self.store.setdefault(name, []).insert(0, value)

    def rpush(self, name, value):
        self.store.setdefault(name, []).append(value)

    def lrange(self, name, start, end):
        return self.store.get(name, [])[start:]

    def ltrim(self, name, start, end):
        return True

    def lrem(self, name, count, value):
        return 0

    def expire(self, name, seconds):
        return name in self.store

    def incr(self, name):
        self.store[name] = int(self.store.get(name, 0)) + 1
        return self.store[name]

    def decr(self, name):
        self.store[name] = int(self.store.get(name, 0)) - 1
        return self.store[name]


redis.Redis = FakeRedis

import app as ap  # noqa: E402


class AppLogicTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='autopacking-test-')
        self.old_log_dir = ap.LOG_DIR
        ap.LOG_DIR = self.tmpdir
        with open(os.path.join(self.tmpdir, 'app.log'), 'w', encoding='utf-8') as f:
            f.write('')
        ap.REDIS_CONFIG['max_concurrent_tasks'] = 2
        for key in list(ap.redis_client.store):
            if key.startswith(ap.REDIS_CONFIG['key_prefix']):
                ap.redis_client.delete(key)

    def tearDown(self):
        ap.LOG_DIR = self.old_log_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_version_validation_rejects_path_traversal(self):
        self.assertTrue(ap._is_safe_version_value('01-20250430-26798-BUG'))
        self.assertFalse(ap._is_safe_version_value('../../etc'))
        self.assertFalse(ap._is_safe_version_value('a/b'))
        self.assertFalse(ap._is_safe_version_value('.hidden'))
        self.assertFalse(ap._is_safe_version_value(''))

    def test_package_filename_rejects_traversal(self):
        ok, _ = ap.validate_package_filename('../evil.tar.gz')
        self.assertFalse(ok)
        ok, _ = ap.validate_package_filename('sub/evil.tar.gz')
        self.assertFalse(ok)

    def test_slot_limit_and_idempotent_acquire(self):
        self.assertTrue(ap.try_acquire_build_slot('t1'))
        self.assertTrue(ap.try_acquire_build_slot('t1'))
        self.assertEqual(ap._count_active_slots(), 1)

        self.assertTrue(ap.try_acquire_build_slot('t2'))
        self.assertFalse(ap.try_acquire_build_slot('t3'))
        self.assertEqual(ap._count_active_slots(), 2)

        ap.release_build_slot('t1')
        self.assertEqual(ap._count_active_slots(), 1)
        self.assertTrue(ap.try_acquire_build_slot('t3'))
        self.assertEqual(ap._count_active_slots(), 2)

        ap.release_build_slot('t2')
        ap.release_build_slot('t3')
        self.assertEqual(ap._count_active_slots(), 0)

    def test_archive_is_atomic_and_prefixed(self):
        img = os.path.join(self.tmpdir, 'a.tar')
        lst = os.path.join(self.tmpdir, 'patch_image_tag_list.txt')
        with open(img, 'wb') as f:
            f.write(b'image')
        with open(lst, 'w', encoding='utf-8') as f:
            f.write('deepflow-server_tag: v6.6.1\n')

        target = os.path.join(self.tmpdir, 'deepflow_patch_v669_01_13.tar.gz')
        ap._create_upgrade_archive([img, lst], target, os.path.join(self.tmpdir, 'task.log'))

        self.assertTrue(os.path.isfile(target))
        self.assertEqual([n for n in os.listdir(self.tmpdir) if n.endswith('.tmp')], [])
        with tarfile.open(target, 'r:gz') as tf:
            names = tf.getnames()
        self.assertTrue(all(n.startswith('deepflow_patch_v669_01_13/') for n in names))
        self.assertEqual(len(names), 2)

    def test_health_endpoint(self):
        client = ap.app.test_client()
        resp = client.get('/health')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['status'], 'ok')

    def test_build_route_rejects_unsafe_version(self):
        client = ap.app.test_client()
        resp = client.get('/build?series=v6&current=../../etc&target=02-x')
        self.assertEqual(resp.status_code, 400)

    def test_incremental_logs_by_seq(self):
        ap.initialize_task_status('log-task', 'queued')
        ap.append_task_log('log-task', '2026-01-01 00:00:00', 'INFO', 'a')
        ap.append_task_log('log-task', '2026-01-01 00:00:01', 'INFO', 'b')
        ap.append_task_log('log-task', '2026-01-01 00:00:02', 'INFO', 'c')

        full = ap.get_task_status('log-task')
        self.assertEqual([l['content'] for l in full['logs']], ['a', 'b', 'c'])
        self.assertEqual(full['log_seq'], 3)

        incremental = ap.get_task_status('log-task', since_seq=2)
        self.assertEqual([l['content'] for l in incremental['logs']], ['c'])
        self.assertEqual(incremental['log_seq'], 3)

        client = ap.app.test_client()
        resp = client.get('/task-status/log-task?since=1')
        self.assertEqual(resp.status_code, 200)
        logs = resp.get_json()['status']['logs']
        self.assertEqual([l['content'] for l in logs], ['b', 'c'])

    def test_run_and_stream_times_out(self):
        log_path = os.path.join(self.tmpdir, 'timeout.log')
        started = time.time()
        # 故意使用子 shell + 孙进程：只杀 bash 不够，必须整组终止
        with self.assertRaises(TimeoutError):
            ap._run_and_stream(
                'timeout-task',
                ['/bin/bash', '-c', 'sh -c "sleep 30"'],
                log_path,
                timeout=1
            )
        self.assertLess(time.time() - started, 3)


if __name__ == '__main__':
    unittest.main()
