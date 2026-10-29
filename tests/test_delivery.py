"""Offline regression tests for image export and deployment tooling."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from image_refs import DEFAULT_REPOSITORY, normalize_image_ref, normalize_images, validate_image_ref


FAKE_TOOL = r'''
import json, os, pathlib, sys
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
log = pathlib.Path(os.environ['TOOL_CALLS'])
if tool == 'skopeo':
    call = {'tool': tool, 'source': args[-2], 'destination': args[-1]}
else:
    call = {'tool': tool, 'args': args}
with log.open('a') as handle:
    handle.write(json.dumps(call) + '\n')
if tool == 'skopeo':
    target = pathlib.Path(args[-1].split(':', 2)[1])
    target.write_bytes(b'' if os.environ.get('EMPTY_COPY') else b'exported-image')
    if os.environ.get('FAIL_COPY'):
        sys.exit(1)
elif tool in ('docker', 'nerdctl') and 'save' in args:
    pathlib.Path(args[args.index('-o') + 1]).write_bytes(b'container-image')
elif tool == 'kubectl':
    if 'get' in args:
        selected = os.environ.get('PINNED_NODE')
        selector = {'kubernetes.io/hostname': selected} if selected else {}
        items = [{'metadata': {'name': name}, 'spec': {'template': {'spec': {'nodeSelector': selector}}}}
                 for name in ('maintenance-app', 'maintenance-worker')]
        print(json.dumps({'items': items}))
    elif 'create' in args:
        print('apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: maintenance-app-config')
    elif 'apply' in args:
        sys.stdin.read()
'''


class ImageReferenceTest(unittest.TestCase):
    def test_normalization_and_deduplication(self):
        self.assertEqual(normalize_images(['agent:v1', DEFAULT_REPOSITORY + 'agent:v1']),
                         [DEFAULT_REPOSITORY + 'agent:v1'])
        self.assertEqual(normalize_image_ref('registry:5000/team/agent:_v1'),
                         'registry:5000/team/agent:_v1')
        self.assertEqual(normalize_image_ref('localhost/team/agent:v1'), 'localhost/team/agent:v1')

    def test_reject_invalid_references(self):
        for ref in ['agent', 'registry:5000/agent', 'agent@sha256:abcd', 'Upper/agent:v1',
                    'registry:0/team/agent:v1', 'registry:65536/team/agent:v1',
                    'registry:port/team/agent:v1', 'a//b:v1', 'a:v 1', 'a:v1\nb:v2',
                    'https://registry/agent:v1', 'a:' + 't' * 129]:
            with self.subTest(ref=ref):
                self.assertFalse(validate_image_ref(ref))
                with self.assertRaises(ValueError):
                    normalize_images(['agent:v1', ref])

    def test_legacy_format_retains_complete_tag_and_literal_custom_name(self):
        self.assertEqual(normalize_image_ref('agent_tag: v7.1.1-feature', legacy=True),
                         DEFAULT_REPOSITORY + 'agent:v7.1.1-feature')
        self.assertEqual(normalize_image_ref('agent_tag:v1', legacy=True),
                         DEFAULT_REPOSITORY + 'agent_tag:v1')


class DeliveryScriptTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='packing-delivery-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for directory in ('bin', 'apply', 'logs', 'tools', 'output'):
            (self.root / directory).mkdir()
        for script in ('pull_save.sh', 'redeploy.sh', 'sync_manifest.py'):
            shutil.copy2(ROOT / 'bin' / script, self.root / 'bin' / script)
        shutil.copy2(ROOT / 'image_refs.py', self.root / 'image_refs.py')
        shutil.copy2(ROOT / 'config.yaml', self.root / 'config.yaml')
        shutil.copy2(ROOT / 'apply/maintenance-app-deployment.yaml', self.root / 'apply/maintenance-app-deployment.yaml')
        self.manifest = self.root / 'apply/maintenance-app-deployment.yaml'
        self.calls = self.root / 'calls.jsonl'
        for name in ('skopeo', 'docker', 'kubectl', 'nerdctl', 'sleep'):
            tool = self.root / 'tools' / name
            tool.write_text('#!' + sys.executable + '\n' + FAKE_TOOL, encoding='utf-8')
            tool.chmod(0o755)
        self.environment = dict(os.environ, PATH=str(self.root / 'tools') + os.pathsep + os.environ['PATH'],
                                PYTHON=sys.executable, COPY_TOOL='skopeo', TOOL_CALLS=str(self.calls))
        # These tests never inherit any publication/credential settings from the caller.
        for key in ('REGISTRY', 'NERDCTL_IMPORT', 'IMAGE_REPO', 'IMAGE_TAG', 'PINNED_NODE',
                    'IMPORT_NODE', 'EMPTY_COPY', 'FAIL_COPY', 'SYNC_CONFIG', 'UPDATE_MANIFEST'):
            self.environment.pop(key, None)
        self.environment['KUBECTL'] = str(self.root / 'tools/kubectl')
        self.environment['NERDCTL'] = str(self.root / 'tools/nerdctl')

    def run_script(self, script, *arguments, **environment):
        return subprocess.run(['bash', str(self.root / 'bin' / script), *map(str, arguments)],
                              env=dict(self.environment, **environment), capture_output=True,
                              text=True, timeout=15)

    def export(self, text, **environment):
        listing = self.root / 'list.txt'
        listing.write_text(text, encoding='utf-8')
        return self.run_script('pull_save.sh', '-d', self.root / 'output', '-f', listing, **environment)

    def tool_calls(self, name):
        return [item for item in map(json.loads, self.calls.read_text().splitlines())
                if item['tool'] == name] if self.calls.exists() else []

    def test_namespace_collision_exports_both_images(self):
        result = self.export('team-a/agent:v7.1.1\nteam-b/agent:v7.1.1\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        exports = (self.root / 'output/image_exports.tsv').read_text().splitlines()
        self.assertEqual(len(exports), 2)
        names = [row.split('\t')[1] for row in exports]
        self.assertEqual(len(set(names)), 2)
        self.assertTrue(all((self.root / 'output' / name).stat().st_size > 0 for name in names))
        self.assertEqual(len(self.tool_calls('skopeo')), 2)

    def test_complete_and_port_registry_references_are_unchanged(self):
        result = self.export('hub.deepflow.yunshan.net/dev/agent:v1\nregistry:5000/team/agent:v2\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c['source'] for c in self.tool_calls('skopeo')], [
            'docker://hub.deepflow.yunshan.net/dev/agent:v1', 'docker://registry:5000/team/agent:v2'])

    def test_list_is_validated_before_any_export(self):
        result = self.export('agent:v1\nnot-an-image\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('第 2 行', result.stderr)
        self.assertEqual(self.tool_calls('skopeo'), [])
        self.assertFalse((self.root / 'output/image_exports.tsv').exists())

    def test_duplicate_references_are_exported_once(self):
        result = self.export('agent_tag: v7.1.1-feature\nagent:v7.1.1-feature\n'
                             'hub.deepflow.yunshan.net/dev/agent:v7.1.1-feature\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.tool_calls('skopeo')), 1)
        self.assertTrue((self.root / 'output/agent_v7.1.1-feature.tar').is_file())
        self.assertIn('IMAGE_EXPORT_COUNT=1', result.stdout)

    def test_positional_input_uses_the_same_normalization(self):
        result = self.run_script('pull_save.sh', '-d', self.root / 'output',
                                 'agent:v1', 'hub.deepflow.yunshan.net/dev/agent:v1',
                                 'registry:5000/team/agent:v2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.tool_calls('skopeo')), 2)

    def test_existing_tar_is_an_error(self):
        (self.root / 'output/agent_v1.tar').write_bytes(b'old-image')
        result = self.export('agent:v1\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.tool_calls('skopeo'), [])
        self.assertEqual((self.root / 'output/agent_v1.tar').read_bytes(), b'old-image')

    def test_empty_or_failed_exports_never_publish_manifest(self):
        for mode in ('EMPTY_COPY', 'FAIL_COPY'):
            with self.subTest(mode=mode):
                result = self.export('agent:v1\n', **{mode: '1'})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / 'output/image_exports.tsv').exists())
                self.assertEqual(list((self.root / 'output').glob('*.tar')), [])
                self.assertEqual(list((self.root / 'output').glob('*.part')), [])

    def test_archive_fallback_writes_the_same_export_manifest(self):
        result = self.export('agent:v1\n', COPY_TOOL='archive')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.tool_calls('skopeo')), 0)
        self.assertTrue((self.root / 'output/agent_v1.tar').is_file())
        self.assertIn(DEFAULT_REPOSITORY + 'agent:v1\tagent_v1.tar',
                      (self.root / 'output/image_exports.tsv').read_text())

    def test_checked_in_configmap_matches_source(self):
        docs = list(yaml.safe_load_all(self.manifest.read_text()))
        configmap = next(d for d in docs if d and d.get('kind') == 'ConfigMap')
        self.assertEqual(yaml.safe_load(configmap['data']['config.yaml']),
                         yaml.safe_load((self.root / 'config.yaml').read_text()))
        worker = next(d for d in docs if d and d.get('metadata', {}).get('name') == 'maintenance-worker')
        environment = worker['spec']['template']['spec']['containers'][0]['env']
        self.assertIn({'name': 'REQUIRE_OSS_STORAGE', 'value': '1'}, environment)

    def test_sync_detects_drift_then_updates_without_reformatting_other_documents(self):
        original = self.manifest.read_text()
        (self.root / 'config.yaml').write_text('redis:\n  max_concurrent_tasks: 2\npackage_storage:\n  backend: oss\n')
        command = [sys.executable, str(self.root / 'bin/sync_manifest.py')]
        checked = subprocess.run(command + ['--check'], capture_output=True, text=True)
        self.assertNotEqual(checked.returncode, 0)
        self.assertEqual(self.manifest.read_text(), original)
        subprocess.run(command, check=True, capture_output=True)
        subprocess.run(command + ['--check'], check=True, capture_output=True)
        self.assertEqual(self.manifest.read_text().split('kind: Secret', 1)[1],
                         original.split('kind: Secret', 1)[1])

    def test_consecutive_registry_releases_update_both_container_images(self):
        for tag in ('release-1', 'release-2'):
            result = self.run_script('redeploy.sh', REGISTRY='registry.example.com/team', IMAGE_TAG=tag)
            self.assertEqual(result.returncode, 0, result.stderr)
            documents = list(yaml.safe_load_all(self.manifest.read_text()))
            for name in ('maintenance-app', 'maintenance-worker'):
                deployment = next(d for d in documents if d and d.get('kind') == 'Deployment'
                                  and d.get('metadata', {}).get('name') == name)
                self.assertEqual(deployment['spec']['template']['spec']['containers'][0]['image'],
                                 f'registry.example.com/team/auto_packing:{tag}')
        self.assertEqual(len([c for c in self.tool_calls('docker') if c['args'][0] == 'push']), 2)

    def test_multinode_local_import_is_rejected_before_build(self):
        result = self.run_script('redeploy.sh', NERDCTL_IMPORT='1', IMPORT_NODE='node-1', IMAGE_TAG='probe')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.tool_calls('docker'), [])
        self.assertEqual(self.tool_calls('nerdctl'), [])

    def test_pinned_single_node_import_is_allowed(self):
        result = self.run_script('redeploy.sh', NERDCTL_IMPORT='1', IMPORT_NODE='node-1',
                                 PINNED_NODE='node-1', IMAGE_TAG='probe')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.tool_calls('nerdctl')), 1)

    def test_missing_distribution_strategy_is_rejected(self):
        result = self.run_script('redeploy.sh', IMAGE_TAG='probe')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.tool_calls('docker'), [])

    def test_local_storage_is_rejected_before_release(self):
        (self.root / 'config.yaml').write_text('package_storage:\n  backend: local\n')
        result = self.run_script('redeploy.sh', REGISTRY='registry.example.com', IMAGE_TAG='probe')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.tool_calls('docker'), [])


if __name__ == '__main__':
    unittest.main()
