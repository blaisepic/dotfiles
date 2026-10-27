import json
import tempfile
import unittest
from pathlib import Path

from mac_agent.config import load_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'config.json'
        self.config = {
            'state_dir': str(self.root / 'state'),
            'repositories': {'r': {'github': 'owner/repo', 'source': 'https://github.com/owner/repo.git', 'base_branch': 'main', 'test_commands': [['python3', '-m', 'unittest']]}},
            'worker': {'image': 'mac-agent-worker:0.1.0', 'codex_version': '0.163.0', 'model': None, 'cpus': '2', 'memory': '4g', 'pids_limit': 256, 'auth_volume': 'mac-agent-codex-auth', 'agent': 'codex'},
            'github_app': {'app_id': '', 'installation_id': '', 'private_key_path': str(self.root / 'outside.pem')},
        }

    def tearDown(self):
        self.temp.cleanup()

    def load(self, mutate=None):
        cfg = json.loads(json.dumps(self.config))
        if mutate:
            mutate(cfg)
        self.path.write_text(json.dumps(cfg), encoding='utf-8')
        return load_config(self.path)

    def assert_invalid(self, mutate):
        with self.assertRaises(ValueError):
            self.load(mutate)

    def test_valid_config_has_absolute_internal_path_and_defaults(self):
        result = self.load()
        self.assertEqual(result['_config_path'], str(self.path.resolve()))
        self.assertEqual(result['worker']['image'], 'mac-agent-worker:0.1.0')
        self.assertEqual(result['worker']['codex_version'], '0.163.0')

    def test_github_source_identity_must_match_allowlist(self):
        self.assert_invalid(lambda c: c['repositories']['r'].update(source='https://github.com/other/repo.git'))

    def test_github_source_rejects_credentials_query_and_extra_path(self):
        for source in ('https://user@github.com/owner/repo.git', 'https://github.com/owner/repo.git?x=1', 'https://github.com/owner/repo.git/../evil'):
            with self.subTest(source=source):
                self.assert_invalid(lambda c, s=source: c['repositories']['r'].update(source=s))

    def test_local_source_requires_top_level_explicit_opt_in(self):
        local = self.root / 'checkout'
        local.mkdir()
        self.assert_invalid(lambda c: c['repositories']['r'].update(source=str(local)))
        result = self.load(lambda c: (c.update(allow_local_sources=True), c['repositories']['r'].update(source=str(local))))
        self.assertEqual(result['repositories']['r']['source'], str(local.resolve()))

    def test_local_source_rejects_relative_and_traversal(self):
        self.assert_invalid(lambda c: (c.update(allow_local_sources=True), c['repositories']['r'].update(source='../repo')))

    def test_state_dir_cannot_be_within_local_repository(self):
        local = self.root / 'checkout'
        local.mkdir()
        self.assert_invalid(lambda c: (c.update(allow_local_sources=True, state_dir=str(local / 'state')), c['repositories']['r'].update(source=str(local))))

    def test_state_root_rejects_symlink(self):
        real = self.root / 'real'
        real.mkdir()
        link = self.root / 'link'
        link.symlink_to(real)
        self.assert_invalid(lambda c: c.update(state_dir=str(link)))

    def test_worker_limits_and_fields_are_validated(self):
        for field, value in [('cpus', '0'), ('memory', '0g'), ('pids_limit', 0), ('image', 'bad image'), ('auth_volume', '../unsafe'), ('model', 7)]:
            with self.subTest(field=field, value=value):
                self.assert_invalid(lambda c, f=field, v=value: c['worker'].update({f: v}))

    def test_base_ref_is_strict_and_private_key_path_is_external(self):
        self.assert_invalid(lambda c: c['repositories']['r'].update(base_branch='main..oops'))
        local = self.root / 'checkout'
        local.mkdir()
        self.assert_invalid(lambda c: (c.update(allow_local_sources=True), c['repositories']['r'].update(source=str(local)), c['github_app'].update(private_key_path=str(local / 'key.pem'))))
