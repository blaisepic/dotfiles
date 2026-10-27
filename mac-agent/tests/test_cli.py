import contextlib
import io
import unittest
import json
from unittest import mock
from mac_agent import cli

class CliTests(unittest.TestCase):
    def setUp(self):
        self.lock_patch = mock.patch.object(cli, '_task_lock', return_value=contextlib.nullcontext())
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)
    def test_wait_reports_not_started_instead_of_timeout_for_ready_task(self):
        cfg = {'state_dir': '/state'}
        service, worker = mock.Mock(), mock.Mock()
        task = {'id': 'abcdef123456', 'status': 'ready'}
        service.get_task.return_value = task
        out = io.StringIO()
        with mock.patch.object(cli, '_load_config', return_value=cfg), \
             mock.patch.object(cli, '_task_service', return_value=service), \
             mock.patch.object(cli, '_worker', return_value=worker), \
             mock.patch.object(cli.time, 'monotonic', side_effect=[0, 2]), \
             mock.patch.object(cli.time, 'sleep') as sleep, contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(['--json', 'wait', task['id'], '--timeout', '1']), 1)
        result = json.loads(out.getvalue())
        self.assertEqual(result['status'], 'ready')
        self.assertIn('not been started', result['error'])
        worker.inspect.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(task['status'], 'ready')

    def test_ready_status_does_not_require_a_running_docker_engine(self):
        cfg = {'state_dir': '/state'}
        service, worker = mock.Mock(), mock.Mock()
        service.get_task.return_value = {'id': 'abcdef123456', 'status': 'ready'}
        worker.inspect.side_effect = RuntimeError('Docker unavailable')
        out = io.StringIO()
        with mock.patch.object(cli, '_load_config', return_value=cfg), \
             mock.patch.object(cli, '_task_service', return_value=service), \
             mock.patch.object(cli, '_worker', return_value=worker), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(['--json', 'status', 'abcdef123456']), 0)
        self.assertEqual(json.loads(out.getvalue())['status'], 'ready')
        worker.inspect.assert_not_called()
        service.save_task.assert_not_called()

    def test_wait_saves_only_under_lock_and_releases_lock_between_polls(self):
        cfg = {'state_dir': '/state'}
        service, worker = mock.Mock(), mock.Mock()
        task = {'id': 'abcdef123456', 'status': 'running'}
        service.get_task.return_value = task
        worker.inspect.side_effect = [{'status': 'running'}, {'status': 'succeeded', 'exit_code': 0}]
        held = []

        @contextlib.contextmanager
        def lock(*args):
            held.append(True)
            try:
                yield
            finally:
                held.pop()

        service.save_task.side_effect = lambda *args: self.assertTrue(held, 'metadata saved outside lock')
        with mock.patch.object(cli, '_load_config', return_value=cfg), \
             mock.patch.object(cli, '_task_service', return_value=service), \
             mock.patch.object(cli, '_worker', return_value=worker), \
             mock.patch.object(cli, '_task_lock', side_effect=lock), \
             mock.patch.object(cli.time, 'monotonic', return_value=0), \
             mock.patch.object(cli.time, 'sleep', side_effect=lambda _: self.assertFalse(held)) as sleep, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['wait', task['id'], '--timeout', '1']), 0)
        self.assertEqual(service.save_task.call_count, 2)
        sleep.assert_called_once()

    def test_start_and_resume_reload_status_after_acquiring_lock(self):
        for command, previous in (('start', 'ready'), ('resume', 'stopped')):
            with self.subTest(command=command):
                cfg = {'state_dir': '/state'}
                service, worker = mock.Mock(), mock.Mock()
                task = {'id': 'abcdef123456', 'status': previous, 'publish': False}
                current = dict(task)
                service.get_task.side_effect = lambda *args: dict(current)
                worker.start.return_value = {'status': 'running'}

                @contextlib.contextmanager
                def lock(*args):
                    current['status'] = 'running'
                    yield

                with mock.patch.object(cli, '_load_config', return_value=cfg), \
                     mock.patch.object(cli, '_task_service', return_value=service), \
                     mock.patch.object(cli, '_worker', return_value=worker), \
                     mock.patch.object(cli, '_task_lock', side_effect=lock), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main([command, task['id']]), 1)
                worker.start.assert_not_called()
                service.save_task.assert_not_called()

    def test_observer_reloads_current_state_while_holding_task_lock(self):
        cfg = {'state_dir': '/state'}
        stale = {'id': 'abcdef123456', 'status': 'stopped'}
        fresh = {'id': stale['id'], 'status': 'running'}
        service, worker = mock.Mock(), mock.Mock()
        held = []

        @contextlib.contextmanager
        def lock(config, task_id):
            held.append(task_id)
            try:
                yield
            finally:
                held.pop()

        def get_task(config, task_id):
            self.assertEqual(held, [task_id], 'metadata read outside task lock')
            return fresh

        def inspect(task):
            self.assertEqual(held, [task['id']], 'inspection outside task lock')
            return {'status': 'succeeded', 'exit_code': 0}

        service.get_task.side_effect = get_task
        worker.inspect.side_effect = inspect
        with mock.patch.object(cli, '_task_lock', side_effect=lock):
            observed = cli._observe(cfg, stale, worker, service)
        self.assertEqual(observed['status'], 'succeeded')
        self.assertTrue(observed['completion_recorded'])
        service.get_task.assert_called_once_with(cfg, stale['id'])

    def test_completion_binds_pr_to_worker_clone_head(self):
        cfg = {'state_dir': '/state'}
        head = 'a' * 40
        task = {'id': 'abcdef123456', 'status': 'succeeded', 'publish': True,
                'repository': 'owner/repo', 'branch': 'agent/abcdef123456',
                'base_branch': 'main', 'workspace': '/test-workspace'}
        worker, auth = mock.Mock(), mock.Mock()
        with mock.patch.object(cli.subprocess, 'run', return_value=mock.Mock(stdout=head + '\n')) as git, \
             mock.patch.object(cli, '_auth_service', return_value=auth), \
             mock.patch('mac_agent.publication.verify_pull_request', return_value={
                 'publication_verified': True, 'published_commit': head}) as verify:
            cli._complete_task(cfg, task, worker)
        self.assertEqual(git.call_args.args[0][-2:], ['rev-parse', 'HEAD'])
        self.assertIn('/test-workspace', git.call_args.args[0])
        self.assertEqual(verify.call_args.kwargs['expected_commit'], head)
        self.assertEqual(task['worker_commit'], head)
        self.assertTrue(task['publication_verified'])
        auth.revoke_lease.assert_called_once()

    def test_completion_revokes_lease_when_log_archival_fails(self):
        cfg = {'state_dir': '/state'}
        task = {'id': 'abcdef123456', 'status': 'failed', 'publish': True,
                'task_dir': '/nonexistent-test-task'}
        worker, auth = mock.Mock(), mock.Mock()
        with mock.patch.object(cli.Path, 'mkdir', side_effect=OSError('private-log-detail')), \
             mock.patch.object(cli, '_auth_service', return_value=auth):
            cli._complete_task(cfg, task, worker)
        auth.revoke_lease.assert_called_once_with(cfg, cfg['state_dir'], task['id'])
        self.assertTrue(task['completion_recorded'])
        self.assertIn('log_warning', task)
        self.assertNotIn('private-log-detail', json.dumps(task))

    def test_stop_keeps_stopped_state_when_remote_revoke_fails(self):
        cfg = {'state_dir': '/state'}
        service, worker, auth = mock.Mock(), mock.Mock(), mock.Mock()
        task = {'id': 'abcdef123456', 'status': 'running', 'publish': True}
        service.get_task.return_value = task
        auth.revoke_lease.side_effect = RuntimeError('secret')
        out = io.StringIO()
        with mock.patch.object(cli, '_load_config', return_value=cfg), mock.patch.object(cli, '_task_service', return_value=service), mock.patch.object(cli, '_worker', return_value=worker), mock.patch.object(cli, '_auth_service', return_value=auth), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(['--json', 'stop', task['id']]), 0)
        self.assertEqual(task['status'], 'stopped')
        self.assertIn('credential_warning', task)
        self.assertNotIn('secret', out.getvalue())

    def test_start_lease_failure_persists_blocked(self):
        cfg = {'state_dir': '/state'}
        service, worker, auth = mock.Mock(), mock.Mock(), mock.Mock()
        task = {'id': 'abcdef123456', 'repository': 'o/r', 'status': 'ready', 'publish': True}
        service.get_task.return_value = task
        auth.ensure_lease.side_effect = RuntimeError('secret')
        with mock.patch.object(cli, '_load_config', return_value=cfg), mock.patch.object(cli, '_task_service', return_value=service), mock.patch.object(cli, '_worker', return_value=worker), mock.patch.object(cli, '_auth_service', return_value=auth), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['start', task['id']]), 1)
        self.assertEqual(task['status'], 'blocked')
        service.save_task.assert_called_once_with(cfg, task)

    def test_help_is_available_without_configuration(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            cli.main(['--help'])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn('create', out.getvalue())

    def test_create_dispatches_task_text_without_shell_execution(self):
        cfg = {'state_dir': '/state'}
        service = mock.Mock()
        service.create_task.return_value = {'id': 'abcdef123456', 'status': 'ready'}
        out = io.StringIO()
        with mock.patch.object(cli, '_load_config', return_value=cfg, create=True), \
             mock.patch.object(cli, '_task_service', return_value=service, create=True), \
             contextlib.redirect_stdout(out):
            result = cli.main(['--json', 'create', 'dotfiles', '--task', 'fix $(not-a-shell)', '--no-publish'])
        self.assertEqual(result, 0)
        service.create_task.assert_called_once_with(cfg, 'dotfiles', 'fix $(not-a-shell)', publish=False)
        self.assertEqual(json.loads(out.getvalue())['id'], 'abcdef123456')

    def test_start_uses_worker_and_saves_running_state(self):
        cfg = {'state_dir': '/state'}
        service, worker = mock.Mock(), mock.Mock()
        task = {'id': 'abcdef123456', 'repository': 'blaisepic/dotfiles', 'status': 'ready', 'publish': False, 'resume_count': 0}
        service.get_task.return_value = task
        worker.start.return_value = {'status': 'running', 'container_id': 'container-id'}
        with mock.patch.object(cli, '_load_config', return_value=cfg), \
             mock.patch.object(cli, '_task_service', return_value=service), \
             mock.patch.object(cli, '_worker', return_value=worker, create=True), \
             contextlib.redirect_stdout(io.StringIO()):
            result = cli.main(['--json', 'start', task['id']])
        self.assertEqual(result, 0)
        worker.start.assert_called_once_with(task, lease_dir=None, resume=False)
        self.assertEqual(task['status'], 'running')
        service.save_task.assert_called_once_with(cfg, task)

    def test_start_failure_persists_blocked_without_exception_details(self):
        cfg = {'state_dir': '/state'}
        service, worker = mock.Mock(), mock.Mock()
        task = {'id': 'abcdef123456', 'repository': 'blaisepic/dotfiles', 'status': 'ready', 'publish': False, 'resume_count': 0}
        service.get_task.return_value = task
        worker.start.side_effect = RuntimeError('secret token leaked')
        out = io.StringIO()
        with mock.patch.object(cli, '_load_config', return_value=cfg), mock.patch.object(cli, '_task_service', return_value=service), mock.patch.object(cli, '_worker', return_value=worker), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(['--json', 'start', task['id']]), 1)
        self.assertEqual(task['status'], 'blocked')
        self.assertNotIn('secret token', out.getvalue())

    def test_resume_reuses_existing_worker_and_increments_count(self):
        cfg = {'state_dir': '/state'}
        service, worker = mock.Mock(), mock.Mock()
        task = {'id': 'abcdef123456', 'repository': 'blaisepic/dotfiles', 'status': 'stopped', 'publish': False, 'resume_count': 0}
        service.get_task.return_value = task
        worker.start.return_value = {'status': 'running', 'container_id': 'old-container'}
        with mock.patch.object(cli, '_load_config', return_value=cfg), mock.patch.object(cli, '_task_service', return_value=service), mock.patch.object(cli, '_worker', return_value=worker), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--json', 'resume', task['id']]), 0)
        worker.start.assert_called_once_with(task, lease_dir=None, resume=True)
        self.assertEqual(task['resume_count'], 1)

    def test_status_reconciles_observed_worker_state(self):
        cfg = {'state_dir': '/state'}
        service, worker = mock.Mock(), mock.Mock()
        task = {'id': 'abcdef123456', 'status': 'running'}
        service.get_task.return_value = task
        worker.inspect.return_value = {'status': 'succeeded', 'exit_code': 0}
        out = io.StringIO()
        with mock.patch.object(cli, '_load_config', return_value=cfg), mock.patch.object(cli, '_task_service', return_value=service), mock.patch.object(cli, '_worker', return_value=worker), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(['--json', 'status', task['id']]), 0)
        self.assertEqual(json.loads(out.getvalue())['status'], 'succeeded')

    def test_zero_exit_does_not_claim_publication_without_verified_pr(self):
        cfg = {'state_dir': '/nonexistent-test-state'}
        service, worker, auth = mock.Mock(), mock.Mock(), mock.Mock()
        task = {'id': 'abcdef123456', 'status': 'running', 'publish': True, 'repository': 'owner/repo', 'branch': 'agent/abcdef123456', 'base_branch': 'main'}
        service.get_task.return_value = task
        worker.inspect.return_value = {'status': 'succeeded', 'exit_code': 0}
        with mock.patch.object(cli, '_load_config', return_value=cfg), mock.patch.object(cli, '_task_service', return_value=service), mock.patch.object(cli, '_worker', return_value=worker), mock.patch.object(cli, '_auth_service', return_value=auth), mock.patch('mac_agent.publication.verify_pull_request', side_effect=ValueError('missing PR')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--json', 'status', task['id']]), 0)
        self.assertEqual(task['status'], 'blocked')
        self.assertFalse(task['publication_verified'])
        auth.revoke_lease.assert_called_once_with(cfg, cfg['state_dir'], task['id'])

    def test_logs_honors_tail_argument(self):
        cfg = {'state_dir': '/state'}
        service, worker = mock.Mock(), mock.Mock()
        task = {'id': 'abcdef123456'}
        service.get_task.return_value = task
        worker.logs.return_value = 'safe log\n'
        out = io.StringIO()
        with mock.patch.object(cli, '_load_config', return_value=cfg), mock.patch.object(cli, '_task_service', return_value=service), mock.patch.object(cli, '_worker', return_value=worker), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(['logs', task['id'], '--tail', '12']), 0)
        worker.logs.assert_called_once_with(task, tail=12)
        self.assertEqual(out.getvalue(), 'safe log\n')

class TaskLockTests(unittest.TestCase):
    def test_lock_rejects_symlinked_state_ancestor(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'real').mkdir()
            (root / 'link').symlink_to(root / 'real')
            with self.assertRaises(ValueError):
                with cli._task_lock({'state_dir': str(root / 'link' / 'state')}, 'abcdef123456'):
                    pass


if __name__ == '__main__':
    unittest.main()
