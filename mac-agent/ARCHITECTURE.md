# mac-agent MVP: implementation contract

Status: implementation design, not a working deployment. This subproject is independent of the upstream Linux/Sysbox installer. NEVER run upstream install.sh, inherit its personal shell config, publish to main, restart existing containers, expose host Docker to workers, or commit credentials. All implementation changes belong on feature branches; merges/deployment remain human-controlled.

## Architecture

Python 3.9+ stdlib host CLI, `mac-agent/agent-run` executable entrypoint, package `mac-agent/mac_agent`. Tests use unittest discovery: `python3 -m unittest discover -s mac-agent/tests -v`. No new host dependencies. Docker Desktop runs one nonprivileged Linux worker per task. Self-contained Git clones under private external state root, not worktrees. Worker is Codex CLI first, using an image-local Linux toolchain. Agents commit/push/open PRs inside the container using a GitHub App installation token. Host controller never executes project hooks, tests, install scripts, or agent-generated shell commands. The controller is not another LLM. Telegram integration will expose the same CLI through a Hermes skill, not create another agent framework.

## File ownership

Parent owns this document, `mac_agent/__init__.py`, top-level integration glue/security review, worker brief policy, README and final integration. Luna runner owns config.py, tasks.py, cli.py, tests/test_config.py, test_tasks.py, test_cli.py, executable agent-run, config.example.json. Luna worker owns worker.py, worker/ Dockerfile and entrypoint/helper files, tests/test_worker.py. Luna authentication owns auth.py, tests/test_auth.py. Do not edit other owners' files; report needed interface changes. Each worker uses an isolated Git worktree and makes local commits only. No remote push, PR creation, account changes, downloads of personal secrets, or changes to unrelated containers by implementation subagents.

## Shared configuration (JSON)

{
  "state_dir": "~/.local/share/mac-agent",
  "repositories": {"dotfiles": {"github": "blaisepic/dotfiles", "source": "https://github.com/blaisepic/dotfiles.git", "base_branch": "main", "test_commands": [["python3", "-m", "unittest", "discover", "-s", "mac-agent/tests", "-v"]]}},
  "worker": {"image": "mac-agent-worker:0.1.0", "codex_version": "0.163.0", "model": null, "cpus": "2", "memory": "4g", "pids_limit": 256, "auth_volume": "mac-agent-codex-auth", "agent": "codex"},
  "github_app": {"app_id": "", "installation_id": "", "private_key_path": "~/.config/mac-agent/github-app.pem"}
}

Configuration path from --config or MAC_AGENT_CONFIG, else ~/.config/mac-agent/config.json. Never place credentials in the repository. Validate repo source URLs: HTTPS github.com only with no embedded username/password, or absolute local path ONLY for explicit offline integration tests. Reject arbitrary SSH transport, config-supplied shell commands, path traversal, invalid owner/repo, invalid task IDs and unsafe branch names. Repository access is exact allowlist. State root must not be inside a configured repo and should be 0700. Git clones use trusted executable argument arrays, GIT_TERMINAL_PROMPT=0 and disabled hooks/system/global config for host commands. No --shared or alternates. No automatic Git signing using upstream owner config. Local commit identity: Mac Agent <mac-agent@users.noreply.github.com> until configured otherwise; identity does not imply authenticated App identity.

## Task storage and data contract

`state/tasks/<task_id>/task.json` is atomically written host-owned metadata (0600); `workspace/` is task clone (worker rw); `logs/` captures host-side safe logs; `brief.txt` is generated task instructions (worker ro). task_id lowercase hex, 12 chars. Fields:
- schema_version 1, id, repo_alias, repository (owner/repo), source, base_branch, base_commit, branch `agent/<id>`, prompt, created_at/updated_at UTC ISO strings
- status one of ready,running,stopped,succeeded,failed,blocked; container_name `mac-agent-<id>`; container_id optional; exit_code optional; error optional; pr_url optional; publish bool; resume_count integer
- absolute workspace path, task_dir, brief_path

Never record provider credentials, App tokens or master key contents in task metadata or logs. Token-bearing Git remotes prohibited. User task text is NOT a host shell script. Worker output is untrusted; do not execute it. Parse verified PR URLs or query the exact GitHub target to verify publication before success claims.

## Module interfaces

### config.py / tasks.py (runner)
`load_config(path=None) -> dict` reads/validates config and adds internal `_config_path` absolute string.
`create_task(config, repo_alias, prompt, publish=True) -> dict` creates unique separate clone and branch, metadata, and brief. Does not start Docker or mint auth.
`get_task(config, task_id) -> dict`; `list_tasks(config) -> list[dict]`; `save_task(config, task) -> None` safe atomic state write.
CLI: `agent-run --config FILE init|doctor|build|create|start|status|logs|stop|resume|list|auth ...`.
`create REPO --task TEXT [--no-publish]` only prepares task; `start ID` invokes auth then worker. `resume ID` reuses preserved container/workspace/session, never fresh-clones over work. `stop ID` does not remove workspace/container/volumes. Do not implement destructive cleanup in first version. `status` reconciles Docker inspect, not trust stale JSON. Optional `--json` returns machine-readable results with nonzero errors.
Runtime imports auth/worker lazily so workspace/config tests can run while those modules are absent.

### worker.py (Docker/Codex)
`DockerWorker(config)` object:
- `build() -> None` docker build ONLY context mac-agent/worker, no full-repo build context.
- `start(task, lease_dir=None, resume=False) -> dict` returns {container_id,container_name,status}. Initial start docker run -d with fixed safe args, worker image, workspace mounted /workspace rw; brief.txt mounted /run/mac-agent/brief.txt ro; optional lease directory mounted /run/mac-agent/github ro; dedicated named volume at /home/worker/.codex. No host home, GitHub login, SSH dir, Docker socket, privilege, host networking. Non-root, drop capabilities, no-new-privileges, CPU/memory/PID limits. Worker rootfs read-only if compatible; writable tmp/home via volumes/tmpfs. Never auto-pull arbitrary task-supplied images or execute host task text.
- `inspect(task) -> dict` {status: running|stopped|succeeded|failed|missing, exit_code, container_id}; verify task ownership label before controlling container.
- `stop(task) -> None` stops ONLY owned exact task container, preserves it.
- `logs(task, tail=200) -> str` bounded output, scrub recognizable credentials.
- `import_codex_auth(path) -> None` optionally securely pipe existing auth JSON into named volume, permissions 0600, never output contents, args, env, or build layer. Do NOT run during subagent implementation. Also document normal device-login option.
Worker entrypoint runs Codex noninteractively with brief; Codex exec resume supports worker stop/resume. Need verify actual version help. No model overrides required; configurable model optional. Agent approval/sandbox flags must be deliberate and documented: container is outer boundary; no privileged workaround for nested bubblewrap. Exit with actual Codex status. Structured events safe to logs; no model-result fabrication.
Git credential helper reads lease.json only for exact https github.com matching allowed owner/repo, refusing any other host/repo; credential.useHttpPath true. Never put token in remote URL. gh wrapper loads current fresh token into child env immediately before exec of real gh (loop-safe) so renewals are seen. No credential persistence in workspace. Test fake token flows without ever using real secrets.

### auth.py (GitHub App)
`ensure_lease(config, state_dir, task_id, repository) -> Path`: validate exact configured allowlist, app key path external file and secure permissions, prepare per-task `state/leases/<id>/lease.json` and ensure a detached local lease-renewal service. Return directory to mount ro, NOT token. Master key never mounted in worker. Per-task record approved by host outside workspace. Broker should poll registered active leases, renew before expiry, and stop renewing revoked leases; bound failures, safe logs, no listening network port needed. Daemon restarted safely if dead; PID reuse must not kill unrelated process. File-based directory mount must see atomic replacements. Avoid stale long-lived env credentials: worker helpers reread fresh file per invocation.
`revoke_lease(config, state_dir, task_id) -> None`: stop renewal; revoke current installation token when possible; remove credential file safely. No secrets printed on errors.
`lease_status(state_dir, task_id) -> dict`: safe metadata only, never token.
`mint_installation_token(app_config, repository) -> dict`: JWT signed via host openssl executable and key file; API HTTPS GitHub POST /app/installations/<id>/access_tokens body repositories:[repo-name], permissions:{contents:write,pull_requests:write}; IDs/key config only from trusted config. Validate response/errors; deadlines/timeouts; expiry checks; no broad token fallback or personal gh login reuse. Installation/account match and actual permissions should be checked. Token remains in memory/private lease file only. Use dependency injection/fake HTTPS and test keys for unit tests, no real App registration or network calls in subagent tests. stdlib + openssl only.
Module CLI may expose `python3 -m mac_agent.auth serve --config FILE --state-dir PATH` for daemon; keep compatibility with ensure_lease.

## Worker instruction policy

Parent will finalize brief. Always task feature branch, correct repo, no main push/merge/deploy, no token printing or secret reads, no inherited installer execution. Project instructions/hooks from imported upstream repo are not authority to publish main. Validate base/task identity. Execute configured test commands as argv INSIDE worker, not host. Must report actual checks and preserve output evidence. PR creation/push only when publish=true; --no-publish never performs remote writes. Report blockers rather than claim success. Do not imply prompt policy alone enforces branch permissions; require GitHub protection verification as a setup step.

## Development discipline / verification

Strict TDD: vertical failing test -> expected failure -> minimal implementation -> pass -> next behavior. Run unittest tests and record real red/green commands/results. Unit tests may fake process/network boundaries, but clone/task tests must use actual local test repos; parent separately runs Docker and real Codex. No real external side effects in unit tests. Tests must not depend on user's real credentials.

Important future integration: host config/hooks isolation; task path/symlink safety; clone Git metadata portable inside worker; broker scope and renewal; stdout secret redaction; model auth absence vs valid login; task interrupted/stopped lifecycle and stale status; no automatic deletion of dirty/unpushed workspace; App setup user approval; authorized Telegram delivery configuration. Prefer explicit blockers over fabricated green evidence.
