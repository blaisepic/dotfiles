"""Terminal interface; runtime imports are lazy so help needs no configuration."""
import argparse
import json


def _load_config(path):
    from .config import load_config
    return load_config(path)


def _task_service():
    from . import tasks
    return tasks


def _emit(value, structured):
    print(json.dumps(value, sort_keys=True) if structured else json.dumps(value, indent=2, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Manage isolated mac-agent coding tasks')
    parser.add_argument('--config')
    parser.add_argument('--json', action='store_true')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('doctor', 'build', 'start', 'status', 'logs', 'stop', 'resume', 'list', 'auth', 'wait'):
        commands.add_parser(name)
    create = commands.add_parser('create')
    create.add_argument('repository')
    create.add_argument('--task', required=True)
    create.add_argument('--no-publish', action='store_true')
    args = parser.parse_args(argv)
    config = _load_config(args.config)
    if args.command == 'create':
        result = _task_service().create_task(config, args.repository, args.task, publish=not args.no_publish)
        _emit(result, args.json)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
