import importlib.util
from pathlib import Path
import unittest

class EntrypointTests(unittest.TestCase):
    def module(self):
        path = Path(__file__).parents[1] / 'worker' / 'launch.py'
        spec = importlib.util.spec_from_file_location('worker_launch', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_resume_command_uses_supported_flags_and_task_session(self):
        command = self.module().build_command(True, 'gpt-6-luna')
        self.assertEqual(command[:4], ['codex', 'exec', 'resume', '--last'])
        self.assertNotIn('-C', command)
        self.assertIn('--json', command)
        self.assertIn('gpt-6-luna', command)

    def test_fresh_command_reads_task_from_stdin(self):
        command = self.module().build_command(False, None)
        self.assertEqual(command[:2], ['codex', 'exec'])
        self.assertEqual(command[-1], '-')
        self.assertNotIn('resume', command)

    def test_main_uses_only_task_specific_saved_sessions(self):
        import tempfile
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home, workspace = root / 'home', root / 'workspace'
            (home / '.codex' / 'sessions').mkdir(parents=True)
            workspace.mkdir()
            (home / '.codex' / 'sessions' / 'saved.jsonl').write_text('{}')
            module = self.module()
            with mock.patch.dict(os.environ, {}, clear=False), \
                 mock.patch.object(module.subprocess, 'run'), \
                 mock.patch.object(module.os, 'chdir'), \
                 mock.patch.object(module.os, 'execvp') as execute:
                self.assertEqual(module.main([], home=home, workspace=workspace), 0)
            self.assertIn('resume', execute.call_args.args[1])
            self.assertNotIn('-C', execute.call_args.args[1])

if __name__ == '__main__':
    unittest.main()
