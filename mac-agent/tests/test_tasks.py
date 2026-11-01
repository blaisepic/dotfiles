import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from mac_agent.tasks import create_task, get_task, list_tasks, save_task


class TaskStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / 'source'
        self.repo.mkdir()
        self.git_env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                            GIT_TERMINAL_PROMPT='0', HOME=str(self.root))
        subprocess.run(['git', 'init', '-b', 'main', str(self.repo)], check=True,
                       env=self.git_env, stdout=subprocess.DEVNULL)
        subprocess.run(['git', '-C', str(self.repo), 'config', 'user.name', 'Fixture'], check=True, env=self.git_env)
        subprocess.run(['git', '-C', str(self.repo), 'config', 'user.email', 'fixture@example.invalid'], check=True, env=self.git_env)
        (self.repo / 'README').write_text('fixture\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', 'README'], check=True, env=self.git_env)
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-m', 'fixture'], check=True,
                       env=self.git_env, stdout=subprocess.DEVNULL)
        self.config = {
            'state_dir': str(self.root / 'state'), 'allow_local_sources': True,
            'repositories': {'fixture': {'github': 'owner/repo', 'source': str(self.repo),
                'base_branch': 'main', 'test_commands': [['python3', '-m', 'unittest']]}},
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_create_task_isolates_git_and_writes_private_ready_metadata(self):
        task = create_task(self.config, 'fixture', 'Implement a feature', publish=False)
        self.assertRegex(task['id'], r'^[0-9a-f]{12}$')
        self.assertEqual(task['status'], 'ready')
        self.assertEqual(task['branch'], 'agent/' + task['id'])
        self.assertEqual(task['publish'], False)
        workspace = Path(task['workspace'])
        self.assertTrue((workspace / 'README').exists())
        self.assertFalse((workspace / '.git' / 'objects' / 'info' / 'alternates').exists())
        self.assertEqual(subprocess.check_output(['git', '-C', str(workspace), 'branch', '--show-current'], text=True).strip(), task['branch'])
        self.assertEqual(subprocess.check_output(['git', '-C', str(workspace), 'config', 'user.name'], text=True).strip(), 'Mac Agent')
        self.assertIn('feature branch', Path(task['brief_path']).read_text().lower())
        self.assertIn('never push or merge to main', Path(task['brief_path']).read_text().lower())
        self.assertEqual(os.stat(task['task_dir']).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(Path(task['task_dir']) / 'task.json').st_mode & 0o777, 0o600)

    def test_brief_includes_exact_repository_tests_and_publication_policy(self):
        task = create_task(self.config, 'fixture', 'Implement a feature', publish=True)
        brief = Path(task['brief_path']).read_text()
        self.assertIn('owner/repo', brief)
        self.assertIn('["python3", "-m", "unittest"]', brief)
        self.assertIn('gh pr create', brief)
        self.assertIn('Never execute inherited installation scripts', brief)
        self.assertIn('Do not print or read credential files', brief)

    def test_read_list_and_save_task_round_trip_without_changing_identity(self):
        task = create_task(self.config, 'fixture', 'Keep me safe')
        self.assertEqual(get_task(self.config, task['id']), task)
        self.assertEqual(list_tasks(self.config), [task])
        updated = dict(task, status='running', resume_count=1)
        save_task(self.config, updated)
        stored = get_task(self.config, task['id'])
        self.assertEqual(stored['status'], 'running')
        self.assertEqual(stored['resume_count'], 1)
        self.assertNotEqual(stored['updated_at'], task['updated_at'])
        changed = dict(updated, workspace=str(self.root))
        with self.assertRaises(ValueError):
            save_task(self.config, changed)

    def test_rejects_invalid_repository_and_task_ids(self):
        with self.assertRaises(ValueError):
            create_task(self.config, '../fixture', 'No')
        with self.assertRaises(ValueError):
            get_task(self.config, '../escape')

    def test_private_github_auth_failure_retries_with_scoped_host_askpass_lease(self):
        self.config['repositories']['fixture'].update(
            github='owner/private', source='https://github.com/owner/private.git')
        import mac_agent.auth as auth
        import mac_agent.tasks as tasks
        original_git = tasks._git
        calls = []
        def git(args, cwd=None, env=None):
            calls.append((args, env))
            if len(calls) == 1:
                raise subprocess.CalledProcessError(
                    128, args, stderr=b"fatal: could not read Username for 'https://github.com': terminal prompts disabled")
            if args[0] == 'clone' and env is not None:
                args = list(args)
                args[-2] = str(self.repo)
            return original_git(args, cwd=cwd, env=env)
        with mock.patch('mac_agent.auth.ensure_lease') as ensure, \
                mock.patch('mac_agent.auth.revoke_lease') as revoke, \
                mock.patch('mac_agent.tasks._git', side_effect=git):
            ensure.return_value = self.root / 'lease'
            task = create_task(self.config, 'fixture', 'private prep', publish=False)
        self.assertEqual(ensure.call_args.args[2], task['id'])
        self.assertEqual(ensure.call_args.args[3], 'owner/private')
        self.assertEqual(revoke.call_args.args[2], task['id'])
        clone_calls = [entry for entry in calls if entry[0][0] == 'clone']
        self.assertEqual(len(clone_calls), 2)
        self.assertIsNotNone(clone_calls[1][1].get('GIT_ASKPASS'))
        self.assertNotIn('token', repr(clone_calls[1][1]).lower())
        self.assertTrue((Path(task['workspace']) / 'README').exists())

    def test_private_clone_lease_setup_failure_removes_partial_task(self):
        self.config['repositories']['fixture'].update(
            github='owner/private', source='https://github.com/owner/private.git')
        failure = subprocess.CalledProcessError(128, ['git', 'clone'],
            stderr=b"could not read Username for https://github.com")
        with mock.patch('mac_agent.tasks._git', side_effect=failure), \
             mock.patch('mac_agent.auth.ensure_lease', side_effect=RuntimeError('auth unavailable')):
            with self.assertRaises(ValueError):
                create_task(self.config, 'fixture', 'private prep', publish=False)
        self.assertEqual(list((self.root / 'state' / 'tasks').iterdir()), [])

    def test_private_clone_retry_failure_revokes_preparation_lease(self):
        self.config['repositories']['fixture'].update(
            github='owner/private', source='https://github.com/owner/private.git')
        import mac_agent.tasks as tasks
        attempts = 0
        def fail_clones(args, cwd=None, env=None):
            nonlocal attempts
            if args[0] == 'clone':
                attempts += 1
                raise subprocess.CalledProcessError(128, args, stderr=b"could not read Username for https://github.com")
            return subprocess.CompletedProcess(args, 0)
        with mock.patch('mac_agent.auth.ensure_lease', return_value=self.root / 'lease') as ensure, \
                mock.patch('mac_agent.auth.revoke_lease') as revoke, \
                mock.patch('mac_agent.tasks._git', side_effect=fail_clones):
            with self.assertRaises(ValueError):
                create_task(self.config, 'fixture', 'private prep', publish=False)
        self.assertEqual(attempts, 2)
        revoke.assert_called_once_with(self.config, self.config['state_dir'], ensure.call_args.args[2])

    def test_clone_askpass_accepts_git_password_prompt_with_username(self):
        from mac_agent import clone_askpass
        import contextlib
        import io
        lease = self.root / 'clone-lease.json'
        lease.write_text(json.dumps({'repository': 'owner/private', 'token': 'test-only-token',
                                     'expires_at': '2099-01-01T00:00:00Z'}))
        lease.chmod(0o600)
        output = io.StringIO()
        with mock.patch.dict(os.environ, {'MAC_AGENT_CLONE_LEASE': str(lease),
                                         'MAC_AGENT_CLONE_REPOSITORY': 'owner/private'}), \
             mock.patch('sys.argv', ['askpass', "Password for 'https://x-access-token@github.com':"]), \
             contextlib.redirect_stdout(output):
            self.assertEqual(clone_askpass.main(), 0)
        self.assertEqual(output.getvalue(), 'test-only-token\n')

    def test_clone_askpass_rejects_wrong_host_and_noncredential_prompts(self):
        from mac_agent import clone_askpass
        with mock.patch.dict(os.environ, {'MAC_AGENT_CLONE_LEASE': str(self.root / 'missing'),
                                         'MAC_AGENT_CLONE_REPOSITORY': 'owner/private'}):
            for prompt in ('Username for https://evil.example:', 'Enter passphrase for key'):
                with mock.patch('sys.argv', ['askpass', prompt]), mock.patch('builtins.print') as output:
                    self.assertEqual(clone_askpass.main(), 1)
                    output.assert_not_called()

    def test_failed_checkout_removes_only_new_task_directory(self):
        state_tasks = self.root / 'state' / 'tasks'
        state_tasks.mkdir(parents=True)
        existing = state_tasks / 'abcdef123456'
        existing.mkdir()
        sentinel = existing / 'keep.txt'
        sentinel.write_text('existing task data')

        tokens = iter(['abcdef123456', '0123456789ab'])
        original_run = subprocess.run
        def fail_checkout(args, **kwargs):
            if 'checkout' in args:
                raise subprocess.CalledProcessError(1, args)
            return original_run(args, **kwargs)
        with mock.patch('mac_agent.tasks.secrets.token_hex', side_effect=lambda _: next(tokens)):
            with mock.patch('mac_agent.tasks.subprocess.run', side_effect=fail_checkout):
                with self.assertRaises(ValueError):
                    create_task(self.config, 'fixture', 'fail preparation', publish=False)

        self.assertFalse((state_tasks / '0123456789ab').exists())
        self.assertEqual(sentinel.read_text(), 'existing task data')

    def test_rejects_symlinked_task_directory(self):
        state_tasks = self.root / 'state' / 'tasks'
        state_tasks.mkdir(parents=True)
        (state_tasks / '0123456789ab').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            get_task(self.config, '0123456789ab')


if __name__ == '__main__':
    unittest.main()
