import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mac_agent.worker import DockerWorker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.root_path = Path(self.root.name)
        self.workspace = self.root_path / 'workspace'
        self.workspace.mkdir()
        (self.workspace / '.git').mkdir()
        (self.root_path / 'brief.txt').write_text('do the task')
        self.config = {'worker': {'image': 'mac-agent-worker:0.1.0', 'cpus': '2', 'memory': '4g', 'pids_limit': 256,
                                  'auth_volume': 'codex-auth'}}
        self.worker = DockerWorker(self.config, project_root=self.root_path)
        self.task = {'id': '012345abcdef', 'container_name': 'mac-agent-012345abcdef',
                     'repository': 'acme/widgets', 'workspace': str(self.workspace),
                     'brief_path': str(self.root_path / 'brief.txt'), 'publish': False}

    def test_start_uses_isolated_fixed_container_arguments(self):
        with patch('mac_agent.worker.subprocess.run') as run:
            run.return_value.stdout = 'container-id\n'
            run.return_value.returncode = 0
            result = self.worker.start(self.task)
        args = run.call_args.args[0]
        self.assertEqual(result['container_id'], 'container-id')
        self.assertIn('--network=none', args)
        self.assertIn('--read-only', args)
        self.assertFalse(any('docker.sock' in arg or '/Users/' in arg for arg in args))
        self.assertIn('mac-agent.task_id=012345abcdef', args)

    def test_inspect_refuses_unowned_container(self):
        with patch('mac_agent.worker.subprocess.run') as run:
            run.return_value.stdout = json.dumps([{'Id': 'cid', 'State': {'Status': 'running'},
                                                   'Config': {'Labels': {'mac-agent.task_id': 'other'}}}])
            with self.assertRaises(PermissionError):
                self.worker.inspect(self.task)

    def test_git_credential_is_exact_repository_scoped_and_fresh(self):
        helper = Path(__file__).parents[1] / 'worker' / 'git-credential-mac-agent'
        lease = self.root_path / 'lease'
        lease.mkdir()
        record = lease / 'lease.json'
        record.write_text(json.dumps({'token': 'fake-secret', 'expires_at': '2099-01-01T00:00:00Z',
                                      'repository': 'acme/widgets'}))
        env = dict(os.environ, MAC_AGENT_LEASE_FILE=str(record), MAC_AGENT_REPOSITORY='acme/widgets')
        def invoke(url):
            import subprocess
            p = subprocess.run([str(helper), 'get'], input=f'protocol=https\nhost=github.com\npath={url}\n\n',
                               text=True, capture_output=True, env=env)
            return p
        self.assertIn('password=fake-secret', invoke('acme/widgets.git').stdout)
        self.assertNotIn('password=', invoke('acme/other.git').stdout)
        record.write_text(json.dumps({'token': 'rotated', 'expires_at': '2099-01-01T00:00:00Z',
                                      'repository': 'acme/widgets'}))
        self.assertIn('password=rotated', invoke('acme/widgets').stdout)


if __name__ == '__main__':
    unittest.main()
