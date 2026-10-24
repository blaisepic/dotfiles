"""Load and validate trusted mac-agent configuration (Python 3.9 stdlib)."""
import json
import os
from pathlib import Path
import re


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _mapping(value, label):
    _require(isinstance(value, dict), '{} must be an object'.format(label))
    return value


def _string(value, label, nonempty=True):
    _require(isinstance(value, str) and (not nonempty or bool(value.strip())), '{} must be a{}string'.format(label, ' non-empty ' if nonempty else ' '))
    return value


def _inside(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _valid_ref(value):
    # Enforce git check-ref-format's forbidden forms without invoking git.
    return (isinstance(value, str) and value and not value.startswith(('-', '/')) and
            not value.endswith(('/', '.', '.lock')) and '//' not in value and '..' not in value and
            not any(c in value for c in ' ~^:?*[\\') and
            not any(part in ('', '.', '..') or part.startswith('.') or part.endswith('.lock') for part in value.split('/')) and
            '@{' not in value and all(ord(c) >= 32 and ord(c) != 127 for c in value))


def load_config(path=None):
    raw_path = path or os.environ.get('MAC_AGENT_CONFIG') or '~/.config/mac-agent/config.json'
    config_path = Path(raw_path).expanduser().resolve()
    try:
        data = json.loads(config_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('cannot read configuration: {}'.format(exc))
    _mapping(data, 'configuration')
    for key in ('state_dir', 'repositories', 'worker', 'github_app'):
        _require(key in data, 'missing configuration key: ' + key)
    _require(type(data.get('allow_local_sources', False)) is bool, 'allow_local_sources must be boolean')
    repos = _mapping(data['repositories'], 'repositories')
    _require(bool(repos), 'repositories must be non-empty')
    local_roots = []
    for alias, repo in repos.items():
        _require(isinstance(alias, str) and re.fullmatch(r'[A-Za-z0-9_-]+', alias), 'invalid repository alias')
        repo = _mapping(repo, 'repository entry')
        identity = _string(repo.get('github'), 'github identity')
        _require(bool(re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', identity)) and '..' not in identity, 'invalid github owner/repository')
        source = _string(repo.get('source'), 'source')
        if source.startswith('https://'):
            match = re.fullmatch(r'https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)\.git', source)
            _require(match is not None and match.group(1).lower() == identity.split('/')[0].lower() and match.group(2).removesuffix('.git').lower() == identity.split('/')[1].removesuffix('.git').lower(), 'source must exactly match configured GitHub identity')
        else:
            _require(data.get('allow_local_sources') is True, 'local sources require allow_local_sources=true')
            p = Path(source)
            _require(p.is_absolute() and '..' not in p.parts, 'local source must be an absolute traversal-free path')
            resolved = p.resolve()
            _require(resolved.exists() and resolved.is_dir(), 'local source must be an existing directory')
            local_roots.append(resolved)
            repo['source'] = str(resolved)
        _require(_valid_ref(repo.get('base_branch')), 'invalid base branch')
        commands = repo.get('test_commands')
        _require(isinstance(commands, list) and all(isinstance(c, list) and c and all(isinstance(a, str) and '\x00' not in a for a in c) for c in commands), 'test_commands must be argv arrays')
    worker = _mapping(data['worker'], 'worker')
    for key in ('image', 'codex_version', 'cpus', 'memory', 'auth_volume', 'agent'):
        _require(key in worker, 'missing worker key: ' + key)
        _string(worker[key], 'worker.' + key)
    _require('pids_limit' in worker, 'missing worker key: pids_limit')
    _require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*', worker['image']) is not None, 'unsafe worker image')
    _require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', worker['auth_volume']) is not None, 'unsafe auth volume')
    _require(re.fullmatch(r'[1-9][0-9]*(?:\.[0-9]+)?', worker['cpus']) is not None, 'cpus must be positive')
    _require(re.fullmatch(r'[1-9][0-9]*(?:[kKmMgGtT])?', worker['memory']) is not None, 'memory must be positive')
    _require(type(worker['pids_limit']) is int and worker['pids_limit'] > 0, 'pids_limit must be a positive integer')
    _require(worker['agent'] == 'codex', 'unsupported worker agent')
    _require(worker.get('model') is None or isinstance(worker.get('model'), str), 'model must be a string or null')
    app = _mapping(data['github_app'], 'github_app')
    for k in ('app_id', 'installation_id', 'private_key_path'):
        _require(k in app, 'missing github_app key: ' + k)
    _string(app['app_id'], 'github_app.app_id', nonempty=False)
    _string(app['installation_id'], 'github_app.installation_id', nonempty=False)
    key_path = Path(_string(app['private_key_path'], 'github_app.private_key_path')).expanduser().resolve()
    app['private_key_path'] = str(key_path)
    state_input = Path(_string(data['state_dir'], 'state_dir')).expanduser()
    _require(not state_input.is_symlink(), 'state_dir must not be a symlink')
    state = state_input.resolve()
    _require(not any(_inside(state, root) for root in local_roots), 'state_dir cannot be inside a configured local repository')
    _require(not any(_inside(key_path, root) for root in local_roots), 'GitHub App key path must be external to configured repositories')
    data['state_dir'] = str(state)
    data['_config_path'] = str(config_path)
    return data
