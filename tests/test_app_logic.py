# -*- coding: utf-8 -*-
"""app.py 关键逻辑的回归测试（不依赖真实 Redis）。

运行方式：
    python -m unittest tests.test_app_logic -v
"""
import fnmatch
import glob
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import redis  # noqa: E402


from unittest.mock import patch
from tests.fake_redis import FakeRedis

# 仅在导入时替换连接工厂，避免污染其他模块的真实 Redis 集成测试。
with patch.object(redis, 'Redis', FakeRedis):
    import app as ap  # noqa: E402


class AppLogicTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='autopacking-test-')
        self.old_log_dir = ap.LOG_DIR
        self._external_guard = patch.object(
            ap, '_run_ossutil', side_effect=AssertionError('测试禁止未模拟的 OSS 调用'))
        self.external_calls = self._external_guard.start()
        self.addCleanup(self._external_guard.stop)
        self._storage_patch = patch.dict(ap.STORAGE_CONFIG, backend='local')
        self._storage_patch.start()
        self.addCleanup(self._storage_patch.stop)
        self._package_patch = patch.object(ap, 'UPGRADE_PACKAGE_DIR', self.tmpdir)
        self._package_patch.start()
        self.addCleanup(self._package_patch.stop)
        ap.LOG_DIR = self.tmpdir
        with open(os.path.join(self.tmpdir, 'app.log'), 'w', encoding='utf-8') as f:
            f.write('')
        ap.REDIS_CONFIG['max_concurrent_tasks'] = 2
        for key in list(ap.redis_client.store):
            if key.startswith(ap.REDIS_CONFIG['key_prefix']):
                ap.redis_client.delete(key)

    def tearDown(self):
        self.external_calls.assert_not_called()
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

    def test_index_uses_external_assets(self):
        client = ap.app.test_client()
        html = client.get('/').get_data(as_text=True)
        self.assertIn('static/app.css', html)
        self.assertIn('static/app.js', html)
        self.assertNotIn('bootstrap', html)
        self.assertNotIn('<style>', html)

    def test_package_name_template(self):
        self.assertEqual(
            ap.PACKAGE_NAME_TEMPLATE.format(current='01', target='13'),
            'deepflow_patch_v669_01_13.tar.gz'
        )

    def test_versions_response_exposes_package_template(self):
        original = ap.get_oss_versions
        ap.get_oss_versions = lambda channel_key=ap.DEFAULT_CHANNEL, use_cache=True: [
            {'display': 'v6.6.1', 'value': 'v6.6.1-20260101', 'seq_num': 1}
        ]
        self.addCleanup(lambda: setattr(ap, 'get_oss_versions', original))

        client = ap.app.test_client()
        data = client.get('/versions?series=v6').get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['package_name_template'], ap.PACKAGE_NAME_TEMPLATE)

    def test_channel_registry_covers_supported_lines(self):
        self.assertEqual(
            list(ap.CHANNELS),
            ['6.4.9', '6.5.9', '6.6.9', '7.0.9', '7.1.9']
        )
        self.assertEqual(ap.DEFAULT_CHANNEL, '6.6.9')
        # 6.6.9 必须继续保持 v669，否则续不上已有历史包
        self.assertEqual(
            ap.CHANNELS['6.6.9']['package_template'].format(current='247', target='248'),
            'deepflow_patch_v669_247_248.tar.gz'
        )
        self.assertEqual(
            ap.CHANNELS['7.1.9']['package_template'].format(current='97', target='99'),
            'deepflow_patch_v719_97_99.tar.gz'
        )

    def test_channel_oss_and_nuwa_prefixes(self):
        ch = ap.CHANNELS['7.0.9']
        self.assertEqual(ch['oss_prefix'], 'oss://df-patch-no-delete/patch/7.0/7.0.9/')
        self.assertEqual(ch['nuwa_dir'], 'nuwa/7.0/7.0.9')
        self.assertEqual(ch['tag_prefix'], 'v7.0')
        self.assertEqual(ch['pkg_code'], '709')

    def test_resolve_channel_accepts_legacy_series(self):
        self.assertEqual(ap.resolve_channel(None), ap.DEFAULT_CHANNEL)
        self.assertEqual(ap.resolve_channel('v6'), ap.DEFAULT_CHANNEL)
        self.assertEqual(ap.resolve_channel('6.6.9'), '6.6.9')
        self.assertEqual(ap.resolve_channel('7.1.9'), '7.1.9')
        # 只传大版本时按唯一匹配归一
        self.assertEqual(ap.resolve_channel('7.1'), '7.1.9')
        self.assertEqual(ap.resolve_channel('6.4'), '6.4.9')
        self.assertIsNone(ap.resolve_channel('9.9.9'))

    def test_channels_endpoint_lists_all_lines(self):
        client = ap.app.test_client()
        data = client.get('/channels').get_json()
        self.assertTrue(data['success'])
        self.assertEqual(len(data['channels']), 5)
        self.assertEqual(data['default'], ap.DEFAULT_CHANNEL)
        self.assertEqual([c['key'] for c in data['channels']], list(ap.CHANNELS))

    def test_versions_endpoint_passes_channel_through(self):
        seen = {}
        original = ap.get_oss_versions

        def fake(channel_key=ap.DEFAULT_CHANNEL, use_cache=False):
            seen['channel'] = channel_key
            return [{'display': '97-x', 'value': '97-x', 'seq_num': 97}]

        ap.get_oss_versions = fake
        self.addCleanup(lambda: setattr(ap, 'get_oss_versions', original))

        client = ap.app.test_client()
        data = client.get('/versions?channel=7.1.9').get_json()
        self.assertTrue(data['success'])
        self.assertEqual(seen['channel'], '7.1.9')
        self.assertEqual(data['channel'], '7.1.9')
        self.assertEqual(data['package_name_template'], 'deepflow_patch_v719_{current}_{target}.tar.gz')

    def test_versions_endpoint_rejects_unknown_channel(self):
        client = ap.app.test_client()
        resp = client.get('/versions?channel=9.9.9')
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()['success'])

    def test_post_build_patch_mode_carries_channel(self):
        client = ap.app.test_client()
        resp = client.post('/build', json={
            'mode': 'patch',
            'channel': '7.1.9',
            'current': '97-20261021-0003-FEATURE',
            'target': '99-20261022-7224-BUG'
        })
        self.assertEqual(resp.status_code, 200)
        task_id = resp.get_json()['task_id']
        status = ap.get_task_status(task_id)
        self.assertEqual(status['channel'], '7.1.9')
        self.assertEqual(status['mode'], 'patch')
        queued = json.loads(ap.redis_client.lrange(ap.build_queue_key(), 0, -1)[0])
        self.assertEqual(queued['channel'], '7.1.9')
        self.assertEqual(queued['task_type'], 'v6')

    def test_post_build_custom_mode_has_no_channel(self):
        client = ap.app.test_client()
        resp = client.post('/build', json={
            'mode': 'custom',
            'images': ['deepflow-agent:v7.1.1']
        })
        self.assertEqual(resp.status_code, 200)
        task_id = resp.get_json()['task_id']
        status = ap.get_task_status(task_id)
        self.assertEqual(status['mode'], 'custom')
        self.assertEqual(status['task_type'], 'v7')
        self.assertTrue(task_id.startswith('build_custom_'))
        # 自定义清单要留在 meta 里，"重试"才能回填
        self.assertEqual(status['images'], ['hub.deepflow.yunshan.net/dev/deepflow-agent:v7.1.1'])

    def test_post_build_patch_mode_rejects_unknown_channel(self):
        client = ap.app.test_client()
        resp = client.post('/build', json={
            'mode': 'patch',
            'channel': '9.9.9',
            'current': '01-a',
            'target': '02-b'
        })
        self.assertEqual(resp.status_code, 400)

    def test_prepare_upgrade_script_fills_target_patch(self):
        tmpdir = tempfile.mkdtemp(prefix='autopacking-script-')
        self.addCleanup(shutil.rmtree, tmpdir, True)
        path, filled = ap._prepare_upgrade_script(
            ap.CHANNELS['6.6.9'], '248-20261020-7239-BUG', tmpdir, 't1'
        )
        self.assertEqual(filled, 'DeepFlow-V6.6.9-248')
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn('upgrade_version="DeepFlow-V6.6.9-248"', content)
        # 包内脚本必须保持原名，客户解压后可直接执行
        self.assertEqual(os.path.basename(path), 'deepflow_patch_upgrade.sh')
        # 其余定制项不应被改动
        self.assertIn('target_registry="hubmgt-uat.paic.com.cn/deepflow"', content)

    def test_prepare_upgrade_script_skips_non_numeric_patch(self):
        tmpdir = tempfile.mkdtemp(prefix='autopacking-script-')
        self.addCleanup(shutil.rmtree, tmpdir, True)
        path, filled = ap._prepare_upgrade_script(
            ap.CHANNELS['6.6.9'], 'abc-def', tmpdir, 't2'
        )
        self.assertEqual(path, ap.UPGRADE_SCRIPT_PATH)
        self.assertIsNone(filled)

    def test_build_route_rejects_unsafe_version(self):
        client = ap.app.test_client()
        resp = client.get('/build?series=v6&current=../../etc&target=02-x')
        self.assertEqual(resp.status_code, 400)

    def test_create_v6_build_via_post(self):
        client = ap.app.test_client()
        resp = client.post('/build', json={
            'series': 'v6',
            'current': '01-20250101-1',
            'target': '02-20250202-2'
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])

        task_id = data['task_id']
        status = ap.get_task_status(task_id)
        self.assertEqual(status['status'], 'queued')
        self.assertEqual(status['task_type'], 'v6')
        self.assertFalse(status['complete'])

        queued = json.loads(ap.redis_client.lrange(ap.build_queue_key(), 0, -1)[0])
        self.assertEqual(queued['task_id'], task_id)
        self.assertEqual(queued['current_version'], '01-20250101-1')
        self.assertEqual(queued['target_version'], '02-20250202-2')

    def test_post_v6_rejects_bad_versions(self):
        client = ap.app.test_client()

        resp = client.post('/build', json={
            'series': 'v6', 'current': '../../etc', 'target': '02-x'
        })
        self.assertEqual(resp.status_code, 400)

        resp = client.post('/build', json={
            'series': 'v6', 'current': '01-20250101-1', 'target': '01-20250101-1'
        })
        self.assertEqual(resp.status_code, 400)

        resp = client.post('/build', json={'series': 'v6', 'current': '01-20250101-1'})
        self.assertEqual(resp.status_code, 400)

    def test_tasks_overview_empty(self):
        client = ap.app.test_client()
        resp = client.get('/tasks/overview')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['active'], [])
        self.assertEqual(data['recent'], [])
        self.assertFalse(data['system']['worker_online'])
        self.assertEqual(data['system']['queue_length'], 0)
        self.assertEqual(data['system']['active_slots'], 0)

    def test_tasks_overview(self):
        client = ap.app.test_client()
        ap.initialize_task_status(
            'ov-active', '任务已创建', task_type='v6',
            extra={'current_version': 'v6.6.1-a', 'target_version': 'v6.6.2-b'}
        )
        ap.initialize_task_status(
            'ov-done', '构建完成', task_type='v7', extra={'image_count': 3}
        )
        ap.update_task_status('ov-done', {
            'status': 'complete', 'complete': True, 'percent': 100,
            'package_name': 'deepflow_patch_v7_demo.tar.gz'
        })

        ap.enqueue_build_task({'task_id': 'queued-1'})
        ap.enqueue_build_task({'task_id': 'queued-2'})
        ap.redis_client.set(ap.worker_heartbeat_key('w1'), str(int(time.time())))
        ap.try_acquire_build_slot('ov-active')
        self.addCleanup(lambda: ap.release_build_slot('ov-active'))

        resp = client.get('/tasks/overview')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get('Cache-Control'), 'no-store')
        data = resp.get_json()
        self.assertTrue(data['success'])

        self.assertEqual([t['task_id'] for t in data['active']], ['ov-active'])
        self.assertEqual(data['active'][0]['current_version'], 'v6.6.1-a')
        self.assertEqual(data['active'][0]['target_version'], 'v6.6.2-b')

        self.assertEqual([t['task_id'] for t in data['recent']], ['ov-done'])
        self.assertEqual(data['recent'][0]['image_count'], 3)
        self.assertEqual(data['recent'][0]['package_name'], 'deepflow_patch_v7_demo.tar.gz')

        system = data['system']
        self.assertTrue(system['worker_online'])
        self.assertEqual(system['queue_length'], 2)
        self.assertEqual(system['active_slots'], 1)
        self.assertEqual(system['max_concurrent_tasks'], ap.REDIS_CONFIG['max_concurrent_tasks'])
        self.assertGreater(system['disk_total'], 0)
        self.assertIn('server_time', system)

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

    def test_cancel_queued_task(self):
        client = ap.app.test_client()
        ap.initialize_task_status('cancel-route', 'queued')

        resp = client.post('/build/cancel-route/cancel')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        status = ap.get_task_status('cancel-route')
        self.assertTrue(status['complete'])
        self.assertTrue(status['cancelled'])
        self.assertEqual(status['message'], '构建已取消')

        # 已结束的任务不允许再次取消
        resp = client.post('/build/cancel-route/cancel')
        self.assertEqual(resp.status_code, 409)

        resp = client.post('/build/not-exist/cancel')
        self.assertEqual(resp.status_code, 404)

    def test_worker_skips_cancelled_task(self):
        import worker

        ap.initialize_task_status('cancel-worker', 'queued')
        ap.redis_client.set(ap.task_cancel_key('cancel-worker'), '1')
        worker.process_task({'task_id': 'cancel-worker', 'task_type': 'v7', 'images': []})

        status = ap.get_task_status('cancel-worker')
        self.assertTrue(status['cancelled'])
        # 没有占用构建槽位
        self.assertEqual(ap._count_active_slots(), 0)

    def test_reclaim_clears_stale_slot_and_requeues_task(self):
        import worker

        ap.REDIS_CONFIG['max_concurrent_tasks'] = 1
        ap.initialize_task_status('dead-task', 'queued')
        self.assertTrue(ap.try_acquire_build_slot('dead-task'))
        payload = json.dumps({'task_id': 'dead-task', 'task_type': 'v7', 'images': []})
        ap.redis_client.rpush(ap.build_processing_key('dead-worker'), payload)

        reclaimed = worker.reclaim_stale_tasks()

        self.assertEqual(reclaimed, 1)
        # 失效 worker 的 slot 必须被清掉，否则单并发下其他任务会一直排队
        self.assertEqual(ap._count_active_slots(), 0)
        self.assertEqual(ap.redis_client.llen(ap.build_queue_key()), 1)
        self.assertEqual(ap.redis_client.llen(ap.build_processing_key('dead-worker')), 0)

        ap.initialize_task_status('new-task', 'queued')
        self.assertTrue(ap.try_acquire_build_slot('new-task'))
        ap.release_build_slot('new-task')

    def test_reclaim_is_skipped_when_lock_is_held(self):
        import worker

        ap.initialize_task_status('dead-task', 'queued')
        payload = json.dumps({'task_id': 'dead-task', 'task_type': 'v7', 'images': []})
        ap.redis_client.rpush(ap.build_processing_key('dead-worker'), payload)
        ap.redis_client.set(worker.RECLAIM_LOCK_KEY, 'other-worker')

        self.assertEqual(worker.reclaim_stale_tasks(), 0)
        self.assertEqual(ap.redis_client.llen(ap.build_queue_key()), 0)
        self.assertEqual(ap.redis_client.llen(ap.build_processing_key('dead-worker')), 1)

    def test_run_and_stream_can_be_cancelled(self):
        log_path = os.path.join(self.tmpdir, 'cancel.log')
        threading.Timer(0.5, lambda: ap.redis_client.set(ap.task_cancel_key('cancel-stream'), '1')).start()
        started = time.time()
        with self.assertRaises(ap.BuildCancelled):
            ap._run_and_stream(
                'cancel-stream',
                ['/bin/bash', '-c', 'sh -c "sleep 30"'],
                log_path,
                timeout=20
            )
        self.assertLess(time.time() - started, 5)

    def test_download_existing_package(self):
        # 固定为本地后端：这个用例校验的是"本机有文件时直接下发"，
        # 不能受部署配置里 backend=oss 的影响
        self._patch_storage(backend='local')
        name = 'download-test.tar.gz'
        path = os.path.join(ap.UPGRADE_PACKAGE_DIR, name)
        with open(path, 'wb') as f:
            f.write(b'package-bytes')
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        client = ap.app.test_client()
        resp = client.get('/download-existing/' + name)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b'package-bytes')
        self.assertIn('attachment', resp.headers.get('Content-Disposition', ''))
        resp.close()

    def test_sse_stream_reads_since_outside_request_context(self):
        ap.initialize_task_status('sse-task', 'queued')
        ap.update_task_status('sse-task', {
            'status': 'complete', 'complete': True, 'percent': 100
        })
        client = ap.app.test_client()
        resp = client.get('/build?task_id=sse-task&since=0')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('sse-task', body)
        resp.close()

    def test_run_and_stream_pushes_all_output_to_redis(self):
        ap.initialize_task_status('log-stream', 'queued')
        log_path = os.path.join(self.tmpdir, 'stream.log')
        rc = ap._run_and_stream(
            'log-stream',
            ['/bin/bash', '-c', 'echo hello-from-script; echo error: boom'],
            log_path,
            timeout=10
        )
        self.assertEqual(rc, 0)

        status = ap.get_task_status('log-stream')
        contents = [entry['content'] for entry in status['logs']]
        self.assertIn('hello-from-script', contents)
        error_entries = [entry for entry in status['logs'] if entry['content'].startswith('error:')]
        self.assertTrue(error_entries)
        self.assertEqual(error_entries[0]['level'], 'ERROR')

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


    # ---------------- 升级包保留策略 ----------------

    def _make_packages(self, count, age_minutes=120):
        """造 count 个包，索引越大越新。"""
        d = tempfile.mkdtemp(prefix='autopacking-pkg-')
        self.addCleanup(shutil.rmtree, d, True)
        now = time.time()
        for i in range(count):
            path = os.path.join(d, 'deepflow_patch_v669_%02d_%02d.tar.gz' % (i, i + 1))
            with open(path, 'wb') as f:
                f.write(b'x' * 1024)
            ts = now - (count - i) * age_minutes * 60
            os.utime(path, (ts, ts))
        return d

    def _patch_retention(self, package_dir, **overrides):
        original_dir = ap.UPGRADE_PACKAGE_DIR
        original_cfg = dict(ap.RETENTION_CONFIG)
        ap.UPGRADE_PACKAGE_DIR = package_dir
        ap.RETENTION_CONFIG.update({
            'enabled': True, 'max_count': 40, 'max_total_gb': 60.0,
            'min_keep': 3, 'min_age_minutes': 0, 'dry_run': False,
        })
        ap.RETENTION_CONFIG.update(overrides)

        def restore():
            ap.UPGRADE_PACKAGE_DIR = original_dir
            ap.RETENTION_CONFIG.clear()
            ap.RETENTION_CONFIG.update(original_cfg)

        self.addCleanup(restore)

    def test_cleanup_trims_to_max_count(self):
        d = self._make_packages(5)
        self._patch_retention(d, max_count=3, min_keep=1)

        summary = ap.cleanup_old_packages()

        self.assertEqual(len(summary['deleted']), 2)
        self.assertEqual(summary['kept'], 3)
        # 删的必须是最旧的两个
        self.assertIn('deepflow_patch_v669_00_01.tar.gz', summary['deleted'])
        self.assertIn('deepflow_patch_v669_01_02.tar.gz', summary['deleted'])
        self.assertEqual(len(glob.glob(os.path.join(d, '*.tar.gz'))), 3)
        self.assertGreater(summary['freed_bytes'], 0)

    def test_cleanup_never_below_min_keep(self):
        d = self._make_packages(5)
        self._patch_retention(d, max_count=0, min_keep=3)

        summary = ap.cleanup_old_packages()

        self.assertEqual(summary['kept'], 3)
        self.assertEqual(len(glob.glob(os.path.join(d, '*.tar.gz'))), 3)

    def test_cleanup_skips_fresh_packages(self):
        d = self._make_packages(5, age_minutes=1)
        self._patch_retention(d, max_count=1, min_keep=1, min_age_minutes=600)

        summary = ap.cleanup_old_packages()

        self.assertEqual(summary['deleted'], [])
        self.assertEqual(len(glob.glob(os.path.join(d, '*.tar.gz'))), 5)

    def test_cleanup_protects_current_package(self):
        d = self._make_packages(4)
        self._patch_retention(d, max_count=1, min_keep=1)
        keep = 'deepflow_patch_v669_00_01.tar.gz'   # 最旧，本来会被删

        summary = ap.cleanup_old_packages(keep_name=keep)

        self.assertNotIn(keep, summary['deleted'])
        self.assertTrue(os.path.exists(os.path.join(d, keep)))

    def test_cleanup_disabled_is_noop(self):
        d = self._make_packages(5)
        self._patch_retention(d, enabled=False, max_count=1)

        summary = ap.cleanup_old_packages()

        self.assertFalse(summary['enabled'])
        self.assertEqual(summary['deleted'], [])
        self.assertEqual(len(glob.glob(os.path.join(d, '*.tar.gz'))), 5)

    def test_cleanup_dry_run_keeps_files(self):
        d = self._make_packages(5)
        self._patch_retention(d, max_count=2, min_keep=1, dry_run=True)

        summary = ap.cleanup_old_packages()

        self.assertTrue(summary['deleted'])
        self.assertEqual(len(glob.glob(os.path.join(d, '*.tar.gz'))), 5)

    def test_cleanup_respects_total_size_cap(self):
        d = self._make_packages(4)          # 每个 1KB
        self._patch_retention(d, max_count=99, min_keep=1, max_total_gb=0.000002)

        summary = ap.cleanup_old_packages()

        self.assertTrue(len(summary['deleted']) >= 2)
        self.assertLessEqual(summary['total_bytes'], 2 * 1024)


    # ---------------- OSS 存储后端 ----------------

    def _patch_storage(self, **overrides):
        original = dict(ap.STORAGE_CONFIG)
        ap.STORAGE_CONFIG.update({
            'backend': 'oss',
            'oss_bucket': 'publicshare-unsafe',
            'oss_prefix': 'tmp/auto_packing/packages/',
            'presign_seconds': 900,
            'oss_timeout_seconds': 120,
            'upload_timeout_seconds': 3600,
        })
        ap.STORAGE_CONFIG.update(overrides)

        def restore():
            ap.STORAGE_CONFIG.clear()
            ap.STORAGE_CONFIG.update(original)

        self.addCleanup(restore)

    def test_package_oss_uri_builds_full_path(self):
        self._patch_storage()
        self.assertEqual(
            ap.package_oss_uri('a.tar.gz'),
            'oss://publicshare-unsafe/tmp/auto_packing/packages/a.tar.gz')
        self.assertEqual(
            ap.package_oss_uri(''),
            'oss://publicshare-unsafe/tmp/auto_packing/packages/')

    def test_package_oss_uri_without_prefix(self):
        self._patch_storage(oss_prefix='')
        self.assertEqual(ap.package_oss_uri('a.tar.gz'),
                         'oss://publicshare-unsafe/a.tar.gz')

    def test_oss_ls_line_parsing(self):
        line = ('2026-10-28 11:09:56 +0000 UTC            5         Standard   '
                'D8E8FCA2DC0F896FD7CB4CB0031BA249      '
                'oss://publicshare-unsafe/tmp/auto_packing/packages/x.tar.gz')
        m = ap._OSS_LS_LINE_RE.match(line.strip())
        self.assertIsNotNone(m)
        self.assertEqual(m.group('size'), '5')
        self.assertTrue(m.group('uri').endswith('/x.tar.gz'))

    def test_oss_helpers_are_noop_when_backend_local(self):
        self._patch_storage(backend='local')
        self.assertFalse(ap.oss_enabled())
        self.assertIsNone(ap.upload_package_to_oss('/tmp/whatever', 'x.tar.gz'))
        self.assertEqual(ap.list_oss_packages(), [])
        self.assertIsNone(ap.presign_package_url('x.tar.gz'))
        self.assertFalse(ap.delete_oss_package('x.tar.gz'))

    def test_presign_returns_url_from_ossutil(self):
        self._patch_storage()
        original = ap._run_ossutil
        ap._run_ossutil = lambda args, timeout: type('R', (), {
            'returncode': 0, 'stdout': 'https://example.com/x?sig=1\n0.001s elapsed\n'})()
        self.addCleanup(lambda: setattr(ap, '_run_ossutil', original))

        self.assertEqual(ap.presign_package_url('x.tar.gz'),
                         'https://example.com/x?sig=1')

    def test_list_oss_packages_parses_objects(self):
        self._patch_storage()
        output = (
            'LastModifiedTime                   Size(B)     StorageClass   ETAG   ObjectName\n'
            '2026-10-28 11:09:56 +0000 UTC            5         Standard   ABC   '
            'oss://publicshare-unsafe/tmp/auto_packing/packages/a.tar.gz\n'
            'Object Number is: 1\n'
        )
        original = ap._run_ossutil
        ap._run_ossutil = lambda args, timeout: type('R', (), {
            'returncode': 0, 'stdout': output})()
        self.addCleanup(lambda: setattr(ap, '_run_ossutil', original))

        packages = ap.list_oss_packages()
        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0]['name'], 'a.tar.gz')
        self.assertEqual(packages[0]['size'], 5)
        self.assertEqual(packages[0]['source'], 'oss')
        self.assertGreater(packages[0]['mtime'], 0)

    def test_existing_packages_merges_oss_and_local(self):
        self._patch_storage()
        import time as _t
        original_local = ap.get_existing_packages
        original_remote = ap.list_oss_packages
        ap.get_existing_packages = lambda: [
            {'name': 'local.tar.gz', 'size': 10, 'mtime': _t.time(), 'source': 'local'}]
        ap.list_oss_packages = lambda: [
            {'name': 'remote.tar.gz', 'size': 20, 'mtime': _t.time() - 60, 'source': 'oss'},
            {'name': 'local.tar.gz', 'size': 10, 'mtime': _t.time(), 'source': 'oss'}]
        self.addCleanup(lambda: setattr(ap, 'get_existing_packages', original_local))
        self.addCleanup(lambda: setattr(ap, 'list_oss_packages', original_remote))

        data = ap.app.test_client().get('/existing-packages').get_json()
        names = [f['name'] for f in data['files']]
        self.assertEqual(sorted(names), ['local.tar.gz', 'remote.tar.gz'])
        self.assertEqual(data['storage_backend'], 'oss')

    def test_cleanup_deletes_oss_object_for_removed_package(self):
        d = self._make_packages(3)
        self._patch_retention(d, max_count=1, min_keep=1)
        self._patch_storage()
        remote = [dict(e, source='oss') for e in ap._package_entries()]
        remote_mock = patch.object(ap, 'list_oss_packages', return_value=remote)
        remote_mock.start()
        self.addCleanup(remote_mock.stop)
        removed = []
        original_delete = ap.delete_oss_package
        ap.delete_oss_package = lambda name: removed.append(name) or True
        self.addCleanup(lambda: setattr(ap, 'delete_oss_package', original_delete))

        summary = ap.cleanup_old_packages()

        self.assertEqual(len(summary['deleted']), 2)
        self.assertEqual(sorted(removed), sorted(summary['deleted']))


if __name__ == '__main__':
    unittest.main()
