"""Docker-backed task worker."""
import json
import os
import subprocess
from pathlib import Path


class DockerWorker:
    def __init__(self, config, project_root=None):
        self.config = config
        self.worker_config = config.get('worker', {})
        self.project_root = Path(project_root) if project_root else Path(__file__).resolve().parents[1]

    def _run(self, args, **kwargs):
        return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)

    def build(self):
        self._run(['docker', 'build', '-t', self.worker_config.get('image', 'mac-agent-worker:0.1.0'),
                   str(self.project_root / 'worker')])

    def start(self, task, lease_dir=None, resume=False):
        name = task['container_name']
        args = ['docker', 'run', '-d', '--name', name, '--label', f"mac-agent.task_id={task['id']}",
                '--label', 'mac-agent.managed=true', '--network=none', '--read-only', '--cap-drop=ALL',
                '--security-opt=no-new-privileges:true', '--cpus', str(self.worker_config.get('cpus', '2')),
                '--memory', str(self.worker_config.get('memory', '4g')), '--pids-limit',
                str(self.worker_config.get('pids_limit', 256)), '--tmpfs', '/tmp:rw,nosuid,nodev,size=512m',
                '-v', f"{task['workspace']}:/workspace:rw", '-v',
                f"{task['brief_path']}:/run/mac-agent/brief.txt:ro", '-v',
                f"{self.worker_config.get('auth_volume', 'mac-agent-codex-auth')}:/home/worker/.codex:rw"]
        args += ['-e', f"MAC_AGENT_REPOSITORY={task['repository']}"]
        if lease_dir:
            args += ['-v', f'{lease_dir}:/run/mac-agent/github:ro']
        args += ['-w', '/workspace', self.worker_config.get('image', 'mac-agent-worker:0.1.0')]
        if resume:
            args += ['resume']
        result = self._run(args)
        return {'container_id': result.stdout.strip(), 'container_name': name, 'status': 'running'}

    def _owned(self, task):
        result = self._run(['docker', 'inspect', task['container_name']])
        data = json.loads(result.stdout)[0]
        labels = data.get('Config', {}).get('Labels') or {}
        if labels.get('mac-agent.task_id') != task['id'] or labels.get('mac-agent.managed') != 'true':
            raise PermissionError('container ownership label mismatch')
        return data

    def inspect(self, task):
        try:
            data = self._owned(task)
        except subprocess.CalledProcessError as exc:
            if 'No such object' in (exc.stderr or ''):
                return {'status': 'missing', 'exit_code': None, 'container_id': None}
            raise
        state = data['State']
        status = state.get('Status')
        if status == 'running':
            mapped = 'running'
        elif state.get('ExitCode', 1) == 0:
            mapped = 'succeeded'
        else:
            mapped = 'failed'
        return {'status': mapped, 'exit_code': state.get('ExitCode'), 'container_id': data.get('Id')}

    def stop(self, task):
        self._owned(task)
        self._run(['docker', 'stop', task['container_name']])

    def logs(self, task, tail=200):
        self._owned(task)
        text = self._run(['docker', 'logs', '--tail', str(max(0, min(int(tail), 2000))), task['container_name']]).stdout
        import re
        return re.sub(r'(?i)(token|password|authorization)([=: ]+)[^\s]+', r'\1\2[REDACTED]', text)

    def import_codex_auth(self, path):
        source = Path(path)
        if source.is_symlink() or not source.is_file():
            raise ValueError('auth source must be a regular file')
        with source.open('rb') as stream:
            self._run(['docker', 'run', '--rm', '-i', '-v',
                       f"{self.worker_config.get('auth_volume', 'mac-agent-codex-auth')}:/auth",
                       self.worker_config.get('image', 'mac-agent-worker:0.1.0'), 'import-auth'], stdin=stream)
