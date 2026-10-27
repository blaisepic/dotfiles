"""Terminal interface; runtime imports are lazy so help needs no configuration."""
import argparse
import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _load_config(path):
    from .config import load_config
    return load_config(path)


def _task_service():
    from . import tasks
    return tasks


def _worker(config):
    from .worker import DockerWorker
    return DockerWorker(config)


def _auth_service():
    from . import auth
    return auth


def _emit(value, structured):
    print(json.dumps(value, sort_keys=True) if structured else json.dumps(value, indent=2, sort_keys=True))


def _parser():
    parser = argparse.ArgumentParser(description='Manage isolated mac-agent coding tasks')
    parser.add_argument('--config')
    parser.add_argument('--json', action='store_true')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor'); commands.add_parser('build'); commands.add_parser('list')
    for name in ('start', 'status', 'stop', 'resume', 'logs'):
        p = commands.add_parser(name); p.add_argument('id')
        if name == 'logs': p.add_argument('--tail', type=int, default=200)
    wait = commands.add_parser('wait'); wait.add_argument('id'); wait.add_argument('--timeout', type=float, default=3600); wait.add_argument('--interval', type=float, default=2)
    create = commands.add_parser('create'); create.add_argument('repository'); create.add_argument('--task', required=True); create.add_argument('--no-publish', action='store_true')
    auth = commands.add_parser('auth'); auth_commands = auth.add_subparsers(dest='auth_command', required=True)
    imp = auth_commands.add_parser('import-codex'); imp.add_argument('--from', dest='source', default='~/.codex/auth.json')
    return parser


def _safe_error(exc):
    # Never surface subprocess arguments, stdout/stderr, or exception detail.
    if isinstance(exc, (ValueError, KeyError, FileNotFoundError, PermissionError)):
        return str(exc) if isinstance(exc, ValueError) else 'requested operation could not be completed'
    return 'operation failed; details were withheld for safety'


@contextlib.contextmanager
def _task_lock(config, task_id):
    if not isinstance(task_id, str) or len(task_id) != 12 or any(c not in '0123456789abcdef' for c in task_id):
        raise ValueError('invalid task id')
    from .auth import _private_dir
    root = _private_dir(config['state_dir'])
    locks = _private_dir(root / 'locks')
    fd = os.open(locks / (task_id + '.lock'), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _complete_task(config, task, worker):
    if task.get('completion_recorded') or task.get('status') not in ('succeeded', 'failed', 'blocked'):
        return
    try:
        if task.get('publish') and task['status'] == 'succeeded':
            from .publication import verify_pull_request
            try:
                lease = Path(config['state_dir']) / 'leases' / task['id'] / 'lease.json'
                token = json.loads(lease.read_text()).get('token') if lease.is_file() and not lease.is_symlink() else None
                from .tasks import _git_env
                head = subprocess.run(
                    ['git', '-c', 'core.hooksPath=' + os.devnull, '-C', task['workspace'], 'rev-parse', 'HEAD'],
                    env=_git_env(), check=True, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10,
                ).stdout.strip()
                task['worker_commit'] = head
                task.update(verify_pull_request(task['repository'], task['branch'], task['base_branch'],
                                              token=token, expected_commit=head))
            except Exception:
                task.update(status='blocked', publication_verified=False, error='Worker exited but its PR could not be independently verified')
        if task.get('task_dir'):
            try:
                directory = Path(task['task_dir']) / 'logs'
                directory.mkdir(mode=0o700, exist_ok=True)
                log = directory / 'worker.log'
                log.write_text(worker.logs(task, tail=2000))
                log.chmod(0o600)
            except Exception:
                task['log_warning'] = 'Worker log could not be archived'
    finally:
        if task.get('publish'):
            try:
                _auth_service().revoke_lease(config, config['state_dir'], task['id'])
            except Exception:
                task['credential_warning'] = 'Credential cleanup could not be fully verified'
    task['completion_recorded'] = True


def _observe(config, task, worker, service):
    with _task_lock(config, task['id']):
        latest = dict(service.get_task(config, task['id']))
        task.clear()
        task.update(latest)
        if task.get('completion_recorded') or task.get('status') == 'ready':
            return task
        previous = task.get('status')
        observed = worker.inspect(task)
        if observed['status'] == 'missing':
            if previous == 'running':
                task.update(status='blocked', error='Worker container missing; workspace is preserved')
        else:
            task.update(observed)
            if previous == 'stopped' and observed['status'] != 'running':
                task['status'] = 'stopped'
        _complete_task(config, task, worker)
        service.save_task(config, task)
        return task


def _run(args):
    config = _load_config(args.config)
    service = _task_service()
    command = args.command
    if command == 'create':
        _emit(service.create_task(config, args.repository, args.task, publish=not args.no_publish), args.json); return 0
    if command == 'list':
        _emit(service.list_tasks(config), args.json); return 0
    if command == 'build':
        _worker(config).build(); _emit({'built': True}, args.json); return 0
    if command == 'doctor':
        checks = {'python3': sys.version_info >= (3, 9), 'docker': shutil.which('docker') is not None,
                  'openssl': shutil.which('openssl') is not None,
                  'worker_image': subprocess.run(['docker', 'image', 'inspect', config.get('worker', {}).get('image', 'mac-agent-worker:0.1.0')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0 if shutil.which('docker') else False,
                  'codex_auth_present': Path.home().joinpath('.codex/auth.json').is_file()}
        _emit({'checks': checks, 'ok': all(checks.values())}, args.json); return 0 if all(checks.values()) else 1
    if command == 'auth':
        _worker(config).import_codex_auth(Path(args.source).expanduser()); _emit({'imported': True}, args.json); return 0
    task = service.get_task(config, args.id)
    worker = _worker(config)
    if command == 'status':
        _emit(_observe(config, task, worker, service), args.json); return 0
    if command == 'logs':
        print(worker.logs(task, tail=args.tail), end=''); return 0
    if command == 'stop':
        with _task_lock(config, args.id):
            task = service.get_task(config, args.id)
            worker.stop(task)
            task.update(status='stopped', error='stopped by user')
            if task.get('publish'):
                try: _auth_service().revoke_lease(config, config['state_dir'], task['id'])
                except Exception: task['credential_warning'] = 'Local lease removed; remote revocation not verified'
            service.save_task(config, task)
        _emit(task, args.json); return 0
    if command in ('resume', 'start'):
        with _task_lock(config, args.id):
            task = service.get_task(config, args.id)
            allowed = ('stopped', 'failed', 'blocked') if command == 'resume' else ('ready',)
            if task['status'] not in allowed: raise ValueError('task status does not allow this operation')
            try:
                lease = _auth_service().ensure_lease(config, config['state_dir'], task['id'], task['repository']) if task.get('publish') else None
                result = worker.start(task, lease_dir=lease, resume=(command == 'resume'))
            except Exception as exc:
                task.update(status='blocked', error='worker start failed')
                if task.get('publish'):
                    try: _auth_service().revoke_lease(config, config['state_dir'], task['id'])
                    except Exception: task['credential_warning'] = 'Local lease removed; remote revocation not verified'
                service.save_task(config, task)
                raise RuntimeError('worker start failed') from exc
            task.update(result)
            if command == 'resume': task['resume_count'] += 1
            task.pop('completion_recorded', None)
            task.pop('publication_verified', None)
            task.update(status='running', error=None)
            service.save_task(config, task); _emit(task, args.json); return 0
    if command == 'wait':
        if args.timeout < 0 or args.interval <= 0: raise ValueError('timeout must be nonnegative and interval positive')
        deadline = time.monotonic() + args.timeout
        while True:
            _observe(config, task, worker, service)
            if task['status'] == 'ready':
                _emit({'id': task['id'], 'status': 'ready', 'error': 'Task has not been started'}, args.json)
                return 1
            if task['status'] in ('succeeded', 'failed', 'blocked', 'stopped'):
                _emit(task, args.json)
                return 0 if task['status'] == 'succeeded' else 1
            if time.monotonic() >= deadline:
                _emit({'id': task['id'], 'status': 'timeout'}, args.json); return 124
            time.sleep(min(args.interval, max(0, deadline-time.monotonic())))
    return 0


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try: return _run(args)
    except Exception as exc:
        _emit({'error': _safe_error(exc)} if args.json else {'error': _safe_error(exc)}, args.json)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
