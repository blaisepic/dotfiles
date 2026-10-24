"""Terminal interface; runtime imports are lazy so help needs no configuration."""
import argparse
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
        observed = worker.inspect(task)
        if observed['status'] != 'missing':
            task.update(observed)
            # Preserve an explicitly requested stop despite Docker's common 143 exit.
            if task.get('status') == 'stopped' and task.get('error') == 'stopped by user': task['status'] = 'stopped'
            service.save_task(config, task)
        _emit(task, args.json); return 0
    if command == 'logs':
        print(worker.logs(task, tail=args.tail), end=''); return 0
    if command == 'stop':
        worker.stop(task)
        task.update(status='stopped', error='stopped by user')
        service.save_task(config, task)
        if task.get('publish'): _auth_service().revoke_lease(config, config['state_dir'], task['id'])
        _emit(task, args.json); return 0
    if command in ('resume', 'start'):
        allowed = ('stopped', 'failed', 'blocked') if command == 'resume' else ('ready',)
        if task['status'] not in allowed: raise ValueError('task status does not allow this operation')
        lease = _auth_service().ensure_lease(config, config['state_dir'], task['id'], task['repository']) if task.get('publish') else None
        try:
            result = worker.start(task, lease_dir=lease, resume=(command == 'resume'))
        except Exception as exc:
            task.update(status='blocked', error='worker start failed')
            service.save_task(config, task)
            if task.get('publish'): _auth_service().revoke_lease(config, config['state_dir'], task['id'])
            raise RuntimeError('worker start failed') from exc
        task.update(result)
        if command == 'resume': task['resume_count'] += 1
        task.update(status='running', error=None)
        service.save_task(config, task); _emit(task, args.json); return 0
    if command == 'wait':
        if args.timeout < 0 or args.interval <= 0: raise ValueError('timeout must be nonnegative and interval positive')
        deadline = time.monotonic() + args.timeout
        while True:
            observed = worker.inspect(task); task.update(observed)
            if observed['status'] in ('succeeded', 'failed', 'blocked', 'stopped'):
                service.save_task(config, task); _emit(task, args.json)
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
