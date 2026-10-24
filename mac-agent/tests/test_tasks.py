import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

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

    def test_rejects_symlinked_task_directory(self):
        state_tasks = self.root / 'state' / 'tasks'
        state_tasks.mkdir(parents=True)
        (state_tasks / '0123456789ab').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            get_task(self.config, '0123456789ab')


if __name__ == '__main__':
    unittest.main()
