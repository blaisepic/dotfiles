import contextlib
import io
import unittest
import json
from unittest import mock
from mac_agent import cli

class CliTests(unittest.TestCase):
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

if __name__ == '__main__':
    unittest.main()
