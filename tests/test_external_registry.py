"""自定义镜像清单里出现外部 registry 时，入队前给出明确提示。"""
import unittest
from unittest.mock import patch

from tests.fake_redis import FakeRedis
from tests.test_app_logic import ap

EXTERNAL = ('dfcloud-image-registry-vpc.cn-beijing.cr.aliyuncs.com/'
            'dev/fauths:feature-6814-cas-logout-v7.1.826')


class ExternalRegistryGuardTest(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        patcher = patch.object(ap, 'redis_client', self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = ap.app.test_client()

    def _post(self, payload):
        return self.client.post('/build', json=payload)

    def test_rejects_external_registry(self):
        resp = self._post({'mode': 'custom', 'images': [EXTERNAL]})
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertFalse(body['success'])
        self.assertEqual(body['external_images'], [EXTERNAL])
        self.assertIn('默认仓库', body['message'])

    def test_allows_external_with_opt_in(self):
        resp = self._post({'mode': 'custom', 'images': [EXTERNAL],
                           'allow_external_registry': True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])

    def test_internal_short_name_still_works(self):
        resp = self._post({'mode': 'custom',
                           'images': ['fauths:feature-6814-cas-logout-v7.1.826']})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])