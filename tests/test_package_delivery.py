"""Artifact correctness, interrupted processes, and upload-only recovery."""
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from image_refs import image_tar_name, normalize_image_ref
from tests.test_app_logic import ap
from tests.fake_redis import FakeRedis
import worker


class PackageDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='packing-delivery-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('images', 'packages', 'logs', 'lists'):
            (self.root / name).mkdir()
        self.redis = FakeRedis()
        for target in (ap, worker):
            self.guard(patch.object(target, 'redis_client', self.redis))
        self.guard(patch.multiple(ap, IMAGE_TAR_ROOT=str(self.root / 'images'),
                                 UPGRADE_PACKAGE_DIR=str(self.root / 'packages'),
                                 LOG_DIR=str(self.root / 'logs'),
                                 LATEST_LIST_DIR=str(self.root / 'lists')))
        self.guard(patch.dict(ap.STORAGE_CONFIG, backend='oss', delete_local_after_upload=True))
        self.guard(patch.dict(ap.RETENTION_CONFIG, enabled=False))
        self.guard(patch.dict(ap.REDIS_CONFIG, max_concurrent_tasks=3))
        self.guard(patch.object(ap, 'write_log'))
        self.external = self.guard(patch.object(ap, '_run_ossutil', side_effect=AssertionError('unexpected external call')))
        self.addCleanup(self.external.assert_not_called)
        self.client = ap.app.test_client()

    def guard(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def task(self, name='task'):
        ap.initialize_task_status(name, 'queued', task_type='v7', extra={'images': ['agent:v1']})
        return {'task_id': name, 'task_type': 'v7', 'images': ['agent:v1']}

    def export(self, task_id, cmd, log):
        directory = Path(cmd[cmd.index('-d') + 1])
        source = Path(cmd[cmd.index('-f') + 1])
        rows = []
        for line in source.read_text().splitlines():
            ref = normalize_image_ref(line, legacy=True)
            name = image_tar_name(ref)
            (directory / name).write_bytes(b'image-content')
            rows.append(ref + '\t' + name)
        (directory / 'image_exports.tsv').write_text('\n'.join(rows) + '\n')
        return 0

    def failed_upload(self):
        payload = self.task()
        self.redis.set(ap.worker_heartbeat_key(worker.WORKER_ID), str(int(time.time())), ex=60)
        with patch.object(ap, '_run_and_stream', side_effect=self.export) as export, \
                patch.object(ap, 'upload_package_to_oss', return_value=None) as upload, \
                patch.object(ap.time, 'sleep'):
            worker.process_task(payload)
        self.assertEqual(export.call_count, 1)
        self.assertEqual(upload.call_count, 3)
        return ap.get_task_status('task', include_logs=False)

    def test_failed_upload_keeps_archive_and_does_not_publish_download(self):
        status = self.failed_upload()
        self.assertEqual(status['status'], 'error')
        self.assertTrue(status['upload_failed'])
        self.assertTrue(status['upload_retry_available'])
        self.assertTrue(Path(status['package_path']).is_file())
        self.assertIsNone(status['download_url'])
        with patch.object(ap, 'presign_package_url') as sign:
            self.assertEqual(self.client.get('/download/task').status_code, 400)
            sign.assert_not_called()
        with tarfile.open(status['package_path']) as archive:
            self.assertEqual(len(archive.getmembers()), 3)
            self.assertTrue(all(member.isfile() for member in archive.getmembers()))
            image = next(m for m in archive.getmembers() if m.name.endswith('.tar'))
            self.assertEqual(archive.extractfile(image).read(), b'image-content')
            listing = next(m for m in archive.getmembers() if m.name.endswith('patch_image_tag_list.txt'))
            self.assertEqual(archive.extractfile(listing).read(), b'agent:v1\n')
            # 包内脚本补默认仓库后，应与实际导出的完整引用相符。
            self.assertEqual(normalize_image_ref('agent:v1'),
                             'hub.deepflow.yunshan.net/dev/agent:v1')
        self.assertEqual(list((self.root / 'images').iterdir()), [])
        self.assertFalse(self.redis.exists(ap.task_slot_key('task')))

    def test_retry_upload_uses_original_archive_and_original_worker(self):
        status = self.failed_upload()
        self.assertEqual(self.client.post('/build/task/retry-upload').status_code, 200)
        self.assertEqual(self.client.post('/build/task/retry-upload').status_code, 409)
        queue = ap.build_upload_queue_key(worker.WORKER_ID)
        self.assertEqual(self.redis.llen(queue), 1)
        payload = json.loads(self.redis.rpoplpush(queue, worker.PROCESSING_QUEUE_KEY))
        with patch.object(ap, '_run_and_stream') as export, \
                patch.object(ap, 'upload_package_to_oss', side_effect=lambda p, n, **kw: ap.package_oss_uri(n)) as upload:
            worker.process_task(payload)
        export.assert_not_called()
        self.assertEqual(upload.call_args.args[0], status['package_path'])
        self.assertFalse(Path(status['package_path']).exists())
        finished = ap.get_task_status('task', include_logs=False)
        self.assertEqual(finished['status'], 'complete')
        self.assertFalse(finished['upload_failed'])
        with patch.object(ap, 'presign_package_url', return_value='https://example.invalid/object') as sign:
            response = self.client.get('/download/task')
        self.assertEqual(response.status_code, 302)
        sign.assert_called_once_with(finished['oss_object_name'])
        self.assertNotEqual(finished['oss_object_name'], finished['package_name'])

    def test_retry_upload_rejects_dead_original_worker(self):
        self.failed_upload()
        self.redis.delete(ap.worker_heartbeat_key(worker.WORKER_ID))
        self.assertEqual(self.client.post('/build/task/retry-upload').status_code, 409)
        self.assertEqual(self.redis.llen(ap.build_upload_queue_key(worker.WORKER_ID)), 0)

    def test_success_is_not_overwritten_by_diagnostic_failure(self):
        self.task()
        path = self.root / 'packages' / 'package.tar.gz'
        path.write_bytes(b'archive')
        with patch.object(ap, 'upload_package_to_oss', return_value='oss://test/object'), \
                patch.object(ap, 'write_log', side_effect=OSError('log disk full')):
            ap._complete_package('task', str(path), path.name, 1)
        self.assertEqual(ap.get_task_status('task')['status'], 'complete')
        self.assertFalse(path.exists())
        self.assertGreater(self.redis.ttl(ap.task_meta_key('task')), 0)

    def test_missing_export_or_empty_tar_fails_before_archive(self):
        source = self.root / 'images' / 'list.txt'
        source.write_text('team-a/agent:v1\nteam-b/agent:v1\n')
        first = normalize_image_ref('team-a/agent:v1')
        (source.parent / 'a.tar').write_bytes(b'image')
        (source.parent / 'image_exports.tsv').write_text(first + '\ta.tar\n')
        with self.assertRaisesRegex(ValueError, '不一致'):
            ap._verify_image_exports(str(source.parent), str(source))
        source.write_text('team-a/agent:v1\n')
        (source.parent / 'a.tar').write_bytes(b'')
        with self.assertRaisesRegex(ValueError, '有效 tar'):
            ap._verify_image_exports(str(source.parent), str(source))

    def test_invalid_custom_images_rejected_and_aliases_deduplicated(self):
        for invalid in ('registry:70000/image:v1', 'bad image:v1', 'agent', '../agent:v1'):
            response = self.client.post('/build', json={'mode': 'custom', 'images': ['agent:v1', invalid]})
            self.assertEqual(response.status_code, 400, invalid)
        response = self.client.post('/build', json={'mode': 'custom', 'images': [
            'agent:v1', 'hub.deepflow.yunshan.net/dev/agent:v1']})
        status = ap.get_task_status(response.json['task_id'])
        self.assertEqual(status['image_count'], 1)

    def test_log_failure_kills_child_without_removing_timeout(self):
        started = time.monotonic()
        with patch.object(ap, 'append_task_log', side_effect=ConnectionError('redis unavailable')):
            with self.assertRaises(ConnectionError):
                ap._run_and_stream('task', ['/bin/bash', '-c', 'echo started; sleep 30'],
                                   str(self.root / 'logs' / 'stream'), timeout=0.3)
        self.assertLess(time.monotonic() - started, 2)

    def test_nonstreaming_command_can_be_cancelled(self):
        import threading
        timer = threading.Timer(0.15, lambda: self.redis.set(ap.task_cancel_key('task'), '1'))
        timer.start()
        self.addCleanup(timer.join)
        with self.assertRaises(ap.BuildCancelled):
            ap._run_cancellable('task', ['/bin/bash', '-c', 'sleep 30'], timeout=20)

    def test_cancel_during_archive_does_not_replace_existing_package(self):
        self.task()
        image = self.root / 'images' / 'a.tar'
        image.write_bytes(b'image')
        target = self.root / 'packages' / 'old.tar.gz'
        target.write_bytes(b'old-valid-package')
        real_run = ap._run_cancellable
        def cancel_after_tar(*args, **kwargs):
            result = real_run(*args, **kwargs)
            self.redis.set(ap.task_cancel_key('task'), '1')
            return result
        with patch.object(ap, '_run_cancellable', side_effect=cancel_after_tar):
            with self.assertRaises(ap.BuildCancelled):
                ap._create_upgrade_archive([str(image)], str(target), str(self.root / 'logs' / 'tar'), task_id='task')
        self.assertEqual(target.read_bytes(), b'old-valid-package')
        self.assertEqual(list(target.parent.glob('*.tmp')), [])

    def test_oss_retention_includes_remote_only_and_keeps_newest(self):
        now = time.time()
        entries = [dict(name=f'package-{i}.tar.gz', size=1024, mtime=now-10000+i,
                        source='oss') for i in range(5)]
        with patch.dict(ap.RETENTION_CONFIG, enabled=True, max_count=2, min_keep=2, min_age_minutes=0), \
                patch.object(ap, 'list_oss_packages', return_value=entries), \
                patch.object(ap, 'delete_oss_package', return_value=True) as delete:
            summary = ap.cleanup_old_packages()
        self.assertEqual(summary['kept'], 2)
        self.assertEqual([c.args[0] for c in delete.call_args_list], ['package-0.tar.gz', 'package-1.tar.gz', 'package-2.tar.gz'])

    def test_retention_list_or_delete_failure_does_not_remove_local_copy(self):
        local = self.root / 'packages' / 'old.tar.gz'
        local.write_bytes(b'keep-me')
        os.utime(local, (1, 1))
        with patch.dict(ap.RETENTION_CONFIG, enabled=True, max_count=0, min_keep=1, min_age_minutes=0), \
                patch.object(ap, 'list_oss_packages', side_effect=RuntimeError('list failed')):
            with self.assertRaises(RuntimeError):
                ap.cleanup_old_packages()
        self.assertTrue(local.exists())
        entries = [dict(name='old.tar.gz', size=7, mtime=1), dict(name='new.tar.gz', size=3, mtime=time.time())]
        with patch.dict(ap.RETENTION_CONFIG, enabled=True, max_count=1, min_keep=1, min_age_minutes=0), \
                patch.object(ap, 'list_oss_packages', return_value=entries), \
                patch.object(ap, 'delete_oss_package', return_value=False):
            self.assertEqual(ap.cleanup_old_packages()['deleted'], [])
        self.assertTrue(local.exists())

    def test_archive_timeout_leaves_existing_package_unchanged(self):
        target = self.root / 'packages' / 'old.tar.gz'
        target.write_bytes(b'old')
        image = self.root / 'images' / 'a.tar'
        image.write_bytes(b'image')
        with patch.object(ap, '_run_cancellable', side_effect=subprocess.TimeoutExpired('tar', 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                ap._create_upgrade_archive([str(image)], str(target), str(self.root / 'logs' / 'tar'))
        self.assertEqual(target.read_bytes(), b'old')


if __name__ == '__main__':
    unittest.main()
