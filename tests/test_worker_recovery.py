"""Failure-injection coverage for task ownership and atomic Redis operations."""
import json
import os
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from urllib.parse import urlparse

from tests.test_app_logic import ap
from tests.fake_redis import FakeRedis
import worker


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        for target in (ap, worker):
            guard = patch.object(target, 'redis_client', self.redis)
            guard.start()
            self.addCleanup(guard.stop)
        for target in (ap, worker):
            guard = patch.object(target, 'write_log')
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch.dict(ap.REDIS_CONFIG, max_concurrent_tasks=3, max_logs=3)
        guard.start()
        self.addCleanup(guard.stop)

    def task(self, task_id='task'):
        ap.initialize_task_status(task_id, 'queued', task_type='v7')
        return {'task_id': task_id, 'task_type': 'v7', 'images': ['agent:v1']}

    def dead_processing(self, task_id='task', owner='dead:attempt'):
        payload = self.task(task_id)
        self.redis.set(ap.task_slot_key(task_id), owner)
        self.redis.lpush(ap.build_processing_key('dead'), json.dumps(payload))

    def test_slot_belongs_to_attempt_and_old_owner_cannot_release(self):
        self.assertTrue(ap.try_acquire_build_slot('task', 'first:1'))
        self.assertTrue(ap.try_acquire_build_slot('task', 'first:1'))
        self.assertFalse(ap.try_acquire_build_slot('task', 'second:1'))
        ap.release_build_slot('task', 'second:1')
        self.assertEqual(self.redis.get(ap.task_slot_key('task')), 'first:1')
        ap.release_build_slot('task', 'first:1')
        self.assertFalse(self.redis.exists(ap.task_slot_key('task')))

    def test_concurrent_acquire_obeys_limit(self):
        barrier = threading.Barrier(10)
        acquired = []

        def acquire(index):
            barrier.wait()
            if ap.try_acquire_build_slot(str(index), 'owner:' + str(index)):
                acquired.append(index)

        threads = [threading.Thread(target=acquire, args=(i,)) for i in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(acquired), 3)
        self.assertEqual(ap._count_active_slots(), 3)

    def test_reclaim_lost_response_does_not_duplicate_message(self):
        self.dead_processing()
        real_eval = self.redis.eval
        lost = False

        def fail_after_commit(script, *args):
            nonlocal lost
            value = real_eval(script, *args)
            if script.startswith('-- reclaim_processing_v1') and not lost:
                lost = True
                raise ConnectionError('reply lost after committed reclaim')
            return value

        with patch.object(self.redis, 'eval', side_effect=fail_after_commit):
            with self.assertRaises(ConnectionError):
                worker.reclaim_stale_tasks()
            self.assertEqual(worker.reclaim_stale_tasks(), 0)
        self.assertEqual(self.redis.llen(ap.build_queue_key()), 1)
        self.assertEqual(self.redis.llen(ap.build_processing_key('dead')), 0)
        self.assertFalse(self.redis.exists(ap.task_slot_key('task')))

    def test_reclaim_rechecks_heartbeat_atomically(self):
        self.dead_processing()
        real_eval = self.redis.eval

        def revive_before_transaction(script, *args):
            if script.startswith('-- reclaim_processing_v1'):
                self.redis.set(ap.worker_heartbeat_key('dead'), str(int(time.time())), ex=60)
            return real_eval(script, *args)

        with patch.object(self.redis, 'eval', side_effect=revive_before_transaction):
            self.assertEqual(worker.reclaim_stale_tasks(), 0)
        self.assertEqual(self.redis.llen(ap.build_queue_key()), 0)
        self.assertEqual(self.redis.get(ap.task_slot_key('task')), 'dead:attempt')

    def test_old_processing_record_cannot_delete_new_owners_slot(self):
        self.dead_processing(owner='new-worker:attempt')
        worker.reclaim_stale_tasks()
        self.assertEqual(self.redis.get(ap.task_slot_key('task')), 'new-worker:attempt')

    def test_lease_loss_fences_status_logs_cleanup_and_cancel(self):
        self.task()
        ap.try_acquire_build_slot('task', 'old:1')
        with ap.execution_context('task', 'old:1'):
            self.redis.set(ap.task_slot_key('task'), 'new:2')
            self.redis.set(ap.task_cancel_key('task'), '1')
            for operation in (
                lambda: ap.assert_task_owned('task'),
                lambda: ap.update_task_status('task', {'status': 'complete'}),
                lambda: ap.append_task_log('task', 'now', 'INFO', 'old log'),
                lambda: ap.schedule_task_cleanup('task', 1),
            ):
                with self.assertRaises(ap.TaskLeaseLost):
                    operation()
            ap.clear_task_cancel('task')
            ap.release_build_slot('task')
        self.assertEqual(self.redis.get(ap.task_slot_key('task')), 'new:2')
        self.assertTrue(self.redis.exists(ap.task_cancel_key('task')))
        self.assertEqual(ap.get_task_status('task')['status'], 'queued')
        self.assertEqual(self.redis.ttl(ap.task_meta_key('task')), -1)

    def test_duplicate_message_is_acknowledged_without_second_execution(self):
        payload = self.task()
        ap.try_acquire_build_slot('task', 'other-worker:attempt')
        with patch.object(worker, 'run_build_task_v7') as run:
            worker.process_task(payload)
        run.assert_not_called()
        self.assertEqual(self.redis.get(ap.task_slot_key('task')), 'other-worker:attempt')

    def test_reclaimed_waiting_worker_cannot_claim_without_processing_record(self):
        payload = self.task()
        with patch.object(worker, 'run_build_task_v7') as run:
            worker.process_task(payload, raw_payload=json.dumps(payload))
        run.assert_not_called()
        self.assertEqual(ap._count_active_slots(), 0)

    def test_waiting_for_capacity_is_not_a_ten_minute_failure(self):
        payload = self.task()
        ap.REDIS_CONFIG['max_concurrent_tasks'] = 1
        ap.try_acquire_build_slot('busy', 'busy:1')
        elapsed = []

        def wait(_):
            elapsed.append(1200)
            ap.release_build_slot('busy', 'busy:1')

        with patch.object(worker.time, 'sleep', side_effect=wait), patch.object(worker, 'run_build_task_v7') as run:
            worker.process_task(payload)
        run.assert_called_once_with('task', ['agent:v1'])
        self.assertEqual(elapsed, [1200])
        self.assertFalse(ap.get_task_status('task')['error'])
        self.assertEqual(ap._count_active_slots(), 0)

    def test_redis_outage_before_acquire_keeps_task_for_retry(self):
        import redis
        payload = self.task()
        read_status = worker.get_task_status
        calls = 0

        def fail_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise redis.ConnectionError('Redis restarting')
            return read_status(*args, **kwargs)

        with patch.object(worker, 'get_task_status', side_effect=fail_once), \
                patch.object(worker.time, 'sleep'), patch.object(worker, 'run_build_task_v7') as run:
            worker.process_task(payload)
        run.assert_called_once()
        self.assertEqual(ap._count_active_slots(), 0)

    def test_failure_waits_for_redis_before_terminal_ack(self):
        import redis
        payload = self.task()
        fail_status = worker.update_task_status
        reporting = []

        def fail_first_report(task_id, updates, *args, **kwargs):
            if updates.get('error') and not reporting:
                reporting.append(True)
                raise redis.ConnectionError('Redis restarting during failure report')
            return fail_status(task_id, updates, *args, **kwargs)

        with patch.object(worker, 'run_build_task_v7', side_effect=OSError('build failed')), \
                patch.object(worker, 'update_task_status', side_effect=fail_first_report), \
                patch.object(worker.time, 'sleep'):
            with self.assertRaises(OSError):
                worker.process_task(payload)
        self.assertTrue(ap.get_task_status('task')['complete'])
        self.assertTrue(ap.get_task_status('task')['error'])

    def test_lease_lost_during_build_does_not_fail_new_attempt(self):
        payload = self.task()

        def revoked(*args):
            self.redis.set(ap.task_slot_key('task'), 'replacement:2')
            ap.update_task_status('task', {'status': 'error'})

        with patch.object(worker, 'run_build_task_v7', side_effect=revoked):
            worker.process_task(payload)
        self.assertEqual(ap.get_task_status('task')['status'], 'queued')
        self.assertEqual(self.redis.get(ap.task_slot_key('task')), 'replacement:2')

    def test_diagnostic_failure_after_completion_preserves_success(self):
        payload = self.task()

        def completed_then_cleanup_failed(*args):
            ap.update_task_status('task', {
                'status': 'complete', 'complete': True, 'error': False, 'percent': 100
            })
            raise OSError('diagnostic disk write failed')

        with patch.object(worker, 'run_build_task_v7', side_effect=completed_then_cleanup_failed):
            with self.assertRaises(OSError):
                worker.process_task(payload)
        status = ap.get_task_status('task')
        self.assertEqual(status['status'], 'complete')
        self.assertFalse(status['error'])

    def test_upload_retry_after_worker_loss_terminates_readably(self):
        payload = self.task()
        payload['action'] = 'retry_upload'
        ap.update_task_status('task', {'artifact_worker_id': 'dead'})
        self.redis.lpush(ap.build_upload_queue_key('dead'), json.dumps(payload))
        worker.reclaim_stale_tasks()
        recovered = json.loads(self.redis.rpop(ap.build_queue_key()))
        worker.process_task(recovered)
        status = ap.get_task_status('task')
        self.assertTrue(status['complete'])
        self.assertTrue(status['error'])
        self.assertFalse(status['upload_retry_available'])

    def retry_upload(self, task_id='task', worker_id='artifact-worker'):
        payload = {'task_id': task_id, 'task_type': 'v7', 'action': 'retry_upload'}
        return self.redis.eval(
            ap._RETRY_UPLOAD, 7, ap.task_meta_key(task_id), ap.worker_heartbeat_key(worker_id),
            ap.task_cancel_key(task_id), ap.build_upload_queue_key(worker_id),
            ap.task_logs_key(task_id), ap.task_log_seq_key(task_id), ap.task_slot_key(task_id),
            worker_id, json.dumps(payload), int(time.time()))

    def failed_upload(self):
        self.task()
        ap.update_task_status('task', {
            'status': 'error', 'complete': True, 'error': True, 'upload_failed': True,
            'upload_retry_available': True, 'artifact_worker_id': 'artifact-worker',
        })

    def test_upload_retry_lost_reply_cannot_enqueue_twice(self):
        self.failed_upload()
        self.redis.set(ap.worker_heartbeat_key('artifact-worker'), 'now', ex=60)
        real_eval = self.redis.eval
        lost = False

        def lose_reply(script, *args):
            nonlocal lost
            result = real_eval(script, *args)
            if script.startswith('-- retry_upload_v1') and not lost:
                lost = True
                raise ConnectionError('queued successfully; reply lost')
            return result

        with patch.object(self.redis, 'eval', side_effect=lose_reply):
            with self.assertRaises(ConnectionError):
                self.retry_upload()
            self.assertEqual(self.retry_upload(), -1)
        self.assertEqual(self.redis.llen(ap.build_upload_queue_key('artifact-worker')), 1)
        status = ap.get_task_status('task')
        self.assertEqual(status['status'], 'queued')
        self.assertFalse(status['complete'])
        self.assertEqual(self.redis.ttl(ap.task_meta_key('task')), -1)

    def test_upload_retry_rejects_offline_worker_and_cancellation(self):
        self.failed_upload()
        self.assertEqual(self.retry_upload(), -2)
        self.assertEqual(self.redis.llen(ap.build_upload_queue_key('artifact-worker')), 0)
        self.redis.set(ap.worker_heartbeat_key('artifact-worker'), 'now', ex=60)
        ap.update_task_status('task', {'cancelled': True})
        self.assertEqual(self.retry_upload(), -1)
        self.assertEqual(self.redis.llen(ap.build_upload_queue_key('artifact-worker')), 0)

    def test_upload_retry_waits_until_previous_attempt_releases_slot(self):
        self.failed_upload()
        self.redis.set(ap.worker_heartbeat_key('artifact-worker'), 'now', ex=60)
        self.redis.set(ap.task_slot_key('task'), 'artifact-worker:ending')
        self.assertEqual(self.retry_upload(), -3)
        self.assertEqual(self.redis.llen(ap.build_upload_queue_key('artifact-worker')), 0)

    def test_logs_appended_after_snapshot_are_delivered_next_time(self):
        self.task()
        original = self.redis.lrange
        appended = False

        def append_after_read(key, *args):
            nonlocal appended
            values = original(key, *args)
            if key == ap.task_logs_key('task') and not appended:
                appended = True
                ap.append_task_log('task', 'now', 'INFO', 'arrived between reads')
            return values

        with patch.object(self.redis, 'lrange', side_effect=append_after_read):
            first = ap.get_task_status('task')
        self.assertEqual(first['log_seq'], 0)
        next_status = ap.get_task_status('task', since_seq=first['log_seq'])
        self.assertEqual([x['content'] for x in next_status['logs']], ['arrived between reads'])

    def test_concurrent_logs_are_ordered_and_trimmed(self):
        self.task()
        threads = [threading.Thread(target=ap.append_task_log, args=('task', 'now', 'INFO', str(i)))
                   for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        status = ap.get_task_status('task')
        self.assertEqual([entry['seq'] for entry in status['logs']], [18, 19, 20])
        self.assertEqual(status['log_seq'], 20)

    def test_log_expiry_tracks_completed_metadata(self):
        self.task()
        ap.schedule_task_cleanup('task', 60)
        ap.append_task_log('task', 'now', 'INFO', 'late cleanup log')
        self.assertGreater(self.redis.ttl(ap.task_logs_key('task')), 0)
        self.assertGreater(self.redis.ttl(ap.task_log_seq_key('task')), 0)

    def test_reopened_task_keeps_log_sequence_and_existing_logs_alive(self):
        self.task()
        ap.append_task_log('task', 'now', 'INFO', 'before retry')
        ap.schedule_task_cleanup('task', 60)
        ap.update_task_status('task', {'status': 'queued', 'complete': False})
        self.assertEqual(self.redis.ttl(ap.task_logs_key('task')), -1)
        self.assertEqual(self.redis.ttl(ap.task_log_seq_key('task')), -1)
        ap.append_task_log('task', 'now', 'INFO', 'after retry')
        self.assertEqual(ap.get_task_status('task')['log_seq'], 2)

    def test_fake_redis_implements_expiration_and_list_removal(self):
        self.redis.set('short', '1', ex=0.01)
        time.sleep(0.02)
        self.assertFalse(self.redis.exists('short'))
        self.redis.rpush('items', 'a', 'b', 'a', 'c')
        self.assertEqual(self.redis.lrem('items', -1, 'a'), 1)
        self.assertEqual(self.redis.lrange('items', 0, -1), ['a', 'b', 'c'])
        self.redis.ltrim('items', -2, -1)
        self.assertEqual(self.redis.lrange('items', 0, -1), ['b', 'c'])


@unittest.skipUnless(os.environ.get('TEST_REDIS_URL'), 'set TEST_REDIS_URL for local Redis integration')
class RedisAtomicIntegrationTest(RecoveryTest):
    """Execute the same protocol assertions against actual local Redis Lua.

    CI supplies a disposable Redis service. A random key prefix isolates all
    fixtures; flushdb is intentionally never used.
    """
    def setUp(self):
        import redis
        url = os.environ['TEST_REDIS_URL']
        if urlparse(url).hostname not in ('127.0.0.1', 'localhost', '::1'):
            self.fail('TEST_REDIS_URL must refer to a disposable local Redis')
        prefix = 'auto_packing_test_' + uuid.uuid4().hex
        cfg = patch.dict(ap.REDIS_CONFIG, key_prefix=prefix, max_concurrent_tasks=3, max_logs=3)
        cfg.start()
        self.addCleanup(cfg.stop)
        self.redis = redis.Redis.from_url(url, decode_responses=True, socket_timeout=3, socket_connect_timeout=1)
        self.redis.ping()
        self.addCleanup(lambda: [self.redis.delete(key) for key in self.redis.scan_iter(match=prefix + ':*')])
        for target in (ap, worker):
            for name, value in (('redis_client', self.redis), ('write_log', lambda *a, **kw: None)):
                guard = patch.object(target, name, value)
                guard.start()
                self.addCleanup(guard.stop)
        guard = patch.object(worker, 'RECLAIM_LOCK_KEY', prefix + ':build:reclaim:lock')
        guard.start()
        self.addCleanup(guard.stop)

    # The expiration test uses fractional TTL accepted only by the test double.
    def test_fake_redis_implements_expiration_and_list_removal(self):
        key = ap.REDIS_CONFIG['key_prefix'] + ':short'
        self.redis.set(key, '1', px=10)
        time.sleep(0.03)
        self.assertFalse(self.redis.exists(key))


if __name__ == '__main__':
    unittest.main()
