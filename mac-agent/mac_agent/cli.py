"""Terminal interface; runtime imports are lazy so help needs no configuration."""
import argparse
import json


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


def main(argv=None):
    parser = argparse.ArgumentParser(description='Manage isolated mac-agent coding tasks')
    parser.add_argument('--config')
    parser.add_argument('--json', action='store_true')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('doctor', 'build', 'status', 'logs', 'stop', 'resume', 'list', 'auth', 'wait'):
        commands.add_parser(name)
    start = commands.add_parser('start')
    start.add_argument('id')
    create = commands.add_parser('create')
    create.add_argument('repository')
    create.add_argument('--task', required=True)
    create.add_argument('--no-publish', action='store_true')
    args = parser.parse_args(argv)
    config = _load_config(args.config)
    if args.command == 'create':
        result = _task_service().create_task(config, args.repository, args.task, publish=not args.no_publish)
        _emit(result, args.json)
    elif args.command == 'start':
        service = _task_service()
        task = service.get_task(config, args.id)
        if task['status'] != 'ready':
            raise ValueError('Only ready tasks can start; use resume for stopped/failed tasks')
        lease = None
        if task['publish']:
            lease = _auth_service().ensure_lease(config, config['state_dir'], task['id'], task['repository'])
        result = _worker(config).start(task, lease_dir=lease, resume=False)
        task.update(result)
        service.save_task(config, task)
        _emit(task, args.json)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
