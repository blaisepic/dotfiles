"""Codex argv construction, shared by fresh and resumed worker launches."""

import os
import sys
import json
import subprocess
import tempfile
from pathlib import Path


def main(argv=None, home=None, workspace=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if home is None and not Path('/.dockerenv').is_file():
        raise RuntimeError('Worker entrypoint must run inside its Docker container')
    home = Path(home or '/home/worker')
    codex_home = home / '.codex'
    codex_home.mkdir(parents=True, exist_ok=True)
    if argv == ['import-auth']:
        content = sys.stdin.buffer.read(5 * 1024 * 1024)
        if not isinstance(json.loads(content), dict):
            raise ValueError('Invalid Codex credential document')
        fd, temporary = tempfile.mkstemp(dir=codex_home, prefix='.auth-')
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
            os.replace(temporary, codex_home / 'auth.json')
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return 0
    sessions = codex_home / 'sessions'
    sessions.mkdir(parents=True, exist_ok=True)
    (home / 'state').mkdir(exist_ok=True)
    global_config = home / 'worker.gitconfig'
    os.environ['GIT_CONFIG_GLOBAL'] = str(global_config)
    for key, value in [('credential.useHttpPath', 'true'), ('credential.helper', '/usr/local/bin/git-credential-mac-agent'), ('user.name', 'Mac Agent'), ('user.email', 'mac-agent@users.noreply.github.com'), ('commit.gpgsign', 'false'), ('safe.directory', '/workspace')]:
        subprocess.run(['git', 'config', '--file', str(global_config), '--replace-all', key, value], check=True)
    os.chdir(workspace or '/workspace')
    resume = any(sessions.rglob('*.jsonl'))
    command = build_command(resume, os.environ.get('MAC_AGENT_MODEL'))
    if not resume:
        with open('/run/mac-agent/brief.txt', 'rb') as stream:
            os.dup2(stream.fileno(), 0)
    os.execvp(command[0], command)
    return 0


def build_command(resume, model):
    command = ['codex', 'exec']
    if resume:
        command += ['resume', '--last']
    command += ['--dangerously-bypass-approvals-and-sandbox', '--json']
    if model:
        command += ['--model', model]
    command += ['Continue the assigned task from the saved state. Preserve existing work; do not merge or deploy.' if resume else '-']
    return command


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('Worker startup failed (' + type(exc).__name__ + ')', file=sys.stderr)
        raise SystemExit(1)
