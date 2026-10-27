# mac-agent: Mac-hosted coding workers

A separate MVP alongside the upstream Linux/Sysbox setup. A small Python runner on macOS launches Codex in a Linux container. Each task has its own Git clone and feature branch. The worker tests, commits, pushes and opens a PR using a narrowly scoped GitHub App installation token. Humans retain merge/deployment control.

Do not run the upstream `install.sh` to use this project. It does not modify your shell configuration, install the original owner's agent stack, or require Sysbox, nested Docker, Envoy, or a new cloud service. Existing development containers are not restarted or repaired by this setup.

## Requirements

- macOS with Docker Desktop running; Python 3.9+, Git, and OpenSSL on the Mac.
- Network access to image/package registries, the model provider, and GitHub.
- A Codex CLI login authorized for use in the dedicated Docker credential volume.
- A private GitHub App installed on the explicitly approved repository.
- A Mac that remains awake while local work is running.

The image pins Codex to 0.163.0. Its Linux user UID/GID are built to match the Mac user, so private owner-readable bind-mounted lease files are readable without making them public or running the worker as root. Rebuild the image if moving to a different host user.

## Configuration and credentials

Copy `config.example.json` to `~/.config/mac-agent/config.json` and edit it. Keep configuration and runtime state outside this repository. Do not put tokens, PEM contents, or provider login documents in source control.

Configuration includes:

- `state_dir`: private task/workspace/broker storage, normally `~/.local/share/mac-agent`.
- `repositories`: exact alias-to-owner/repository and HTTPS source allowlist, base branch, and test commands expressed as argv arrays.
- `worker`: image name, Codex version, optional model, CPU/memory/PID limits and provider-auth volume name.
- `github_app`: App ID, installation ID and the external private-key file path.

The bootstrap configuration on the pilot machine uses `feat/macos-agent-mvp` as its base until the implementation is merged. After the human merges it, the base can be changed to `main`. A task PR during bootstrap targets the implementation branch; it is not automatically a PR against upstream or main.

Local repository sources are disabled by default. `allow_local_sources=true` is an explicit offline-test facility, not a substitute for production repository allowlisting. Arbitrary SSH URLs and credential-bearing Git URLs are rejected.

### GitHub App setup

1. Register a private App restricted to your account. For this publishing-only setup, disable webhooks and do not request user OAuth authorization.
2. Grant repository Contents read/write and Pull requests read/write. Metadata read access is automatic. Do not grant administration, Actions/workflow write, or unrelated permissions without a concrete authorized need.
3. Install it on only the approved pilot repository.
4. Generate its private key and store it outside Git with owner-only file permissions, under an owner-only configuration directory. Enter the App and installation IDs in the private configuration file.
5. Protect main on GitHub. The pilot uses an active ruleset restricting updates/deletion/non-fast-forward changes with a repository-admin bypass and no App bypass. Admin humans retain control; branch safety is not enforced merely by a prompt or a repo-scoped token.

The Mac signs short-lived App JWTs and requests repository-scoped installation tokens. A local file-based broker renews leases before expiry. Each container sees only its task lease directory, read-only, never the App private key. Git and gh helpers reread the current lease for each invocation. Tokens do not appear in Git remotes or Docker environment configuration.

A publishing task has enough repository permission to alter code on its task branches. This is intentional, not a zero-trust execution system. Narrow repository installation, GitHub rules, human review, and careful task selection remain necessary.

The Mac account running the controller is trusted. Its host processes can already read the private App key and configuration; private lease/registry files are not a security boundary against other processes running as that same account. The boundary is between the trusted Mac controller and the constrained worker container. Workers cannot mount or edit the host broker registry or App key. The controller intentionally handles installation tokens in memory for authenticated GitHub verification; those tokens are never returned in CLI output.

## Build and authenticate

From the checkout root:

```sh
mac-agent/agent-run --help
mac-agent/agent-run build
mac-agent/agent-run auth import-codex
mac-agent/agent-run --json doctor
```

`auth import-codex` is an explicit sensitive operation: it pipes the existing `~/.codex/auth.json` into a dedicated Docker volume. The document is not passed as a command argument, included in an image layer, or printed. Do not run it without the account owner's authorization. A different source can be selected with `auth import-codex --from PATH`.

Provider login is separate from GitHub App authentication. The provider credential volume is shared, but worker session files and writable home/state are task-specific. Start with one active worker; concurrent provider-token refresh and parallel task operations have not been stress-tested.

Workers run non-root with a read-only image filesystem, dropped capabilities, no-new-privileges and resource limits. Writable task/home/session storage is explicit. Codex's inner approval/sandbox bypass is used only inside that outer container so unattended tool execution works; it is never applied to a host Codex process. The container does not receive the Docker daemon socket, a whole-home mount, personal SSH/gh credentials, or production secrets. Outbound networking is currently enabled, not destination-allowlisted.

## Task lifecycle

Global options precede the command. Use `--config FILE` to override the default private configuration.

```sh
mac-agent/agent-run --json create dotfiles --task 'Make a small, specific change and add tests.'
mac-agent/agent-run --json start TASK_ID
mac-agent/agent-run --json status TASK_ID
mac-agent/agent-run logs TASK_ID --tail 12
mac-agent/agent-run --json wait TASK_ID --timeout 3600 --interval 3
mac-agent/agent-run --json stop TASK_ID
mac-agent/agent-run --json resume TASK_ID
mac-agent/agent-run --json list
```

Task IDs contain 12 lowercase hexadecimal characters. `create` prepares a separate clone and branch but does not start Docker or mint a token. Add `--no-publish` to prepare a task without GitHub publishing authority. Task text is data, not a host shell script.

The workspace lives under `state_dir/tasks/TASK_ID/workspace`, beside private task metadata and the read-only worker brief. Git operations during preparation disable host global/system configuration and hooks. Workers do not use Mac absolute-path worktree registrations.

A running task is reconciled against Docker, not only its saved JSON. A zero Codex exit is not PR proof: publishing completion checks the worker clone's Git HEAD against the exact repository/head/base and bot PR metadata before recording `publication_verified=true`. An already closed or human-merged matching PR counts as historical publication; its current state is reported separately and does not imply code acceptance. Tests and their outputs must still be reviewed; a PR is not proof the code is correct. Terminal logs are archived on observed completion and revocation of the current App lease is attempted. Keep a `wait`/status monitor active so completion and cleanup are observed.

`status` for a ready task does not require Docker. `wait` returns 0 for success, 1 for a terminal failure/stopped/blocked result or a task not yet started, and 124 for timeout. A timeout does not delete or stop the worker. `stop` preserves the container, clone, files and session and revokes the current GitHub lease; `resume` obtains a new lease and starts the same owned container. Completed tasks are not resumed as new tasks. There is deliberately no automatic destructive cleanup command in this first version.

## Telegram through Hermes

Hermes is the conversation layer; the runner is not another LLM or Telegram server. The same CLI is usable from either surface.

The default Hermes profile has a user-local `mac-agent-runner` skill installed. For a different installation, adapt the companion skill's runner path and install it only into the intended profile. The gateway must already be configured with owner-only/approved-user access; do not enable open access to make a test work. No worker receives the Telegram token.

A read-only connection test:

1. Open the configured Hermes Telegram bot.
2. Start a fresh session if needed so it sees the installed skill.
3. Ask: `Use mac-agent-runner to show the status and PR link for task TASK_ID. Do not start a new task.`
4. Verify that the received status and PR correspond to the CLI output.

For new coding work, state the repository alias and task. Hermes checks prerequisites, invokes create/start, retains the returned ID, and monitors status/wait. Long-running waits can use supported gateway background notifications; plain terminal sessions have no automatic message-delivery channel. Notification durability across gateway/Mac restarts is not guaranteed in this MVP. Task work/state remains on disk.

## Tests and verified milestones

```sh
PYTHONPATH=mac-agent python3 -m unittest discover -s mac-agent/tests -v
```

Unit tests use real local Git repositories and fake process/network boundaries; they never require real credentials or mutate GitHub. These are not substitutes for the following live checks performed on the pilot machine:

- Native Linux/ARM worker image built and Codex login imported/recognized.
- Scoped App token read-back from inside the non-root container, followed by token revocation.
- Actual Codex-created file, in-container test execution and local commit.
- Actual interrupted task stopped and resumed in the same container with its checkpoint preserved; the resumed worker completed tests and committed the intended file.
- Actual container-side Git push and bot-created pilot PR, independently read back from GitHub. The first pilot run passed 39 tests; the suite has grown since that run.
- CLI doctor/list/status/wait/log operations exercised against that retained task.

The first publishing proof is task `ebadc9e17da5`, PR #1 in the pilot fork. It adds only `mac-agent/PILOT-PR.md` against the implementation branch. Nothing was merged or deployed.

The owner confirmed the Telegram read-only status request returned the correct successful task, PR link, and original 39-test pilot result after pairing approval. This verifies the read-only Telegram-to-runner path, not automatic new-task execution or durable completion notifications. Independent review and final acceptance remain separate gates; unit-test counts alone do not approve a release.

## Limits and recovery

- Existing worktree-based containers retain their original Git-mount problem until a separate maintenance change; they are not modified by this pilot.
- The broker has unit coverage for renewal and failure reporting, but a multi-hour renewal soak has not been performed. Revoking the current lease does not promise revocation of any previously copied/older token before its own expiry.
- Interrupted Codex tool calls can leave an upstream diagnostic about missing tool output on resume. The live resume check completed successfully, but review the resumed work rather than assume exactly-once command execution.
- Logs are best-effort redacted, not a guarantee against every possible secret representation. Never ask workers to inspect or print credentials.
- No automatic merge/deploy, scheduling, GitHub issue pickup, multi-agent coordination, nested Docker, or VM-equivalent isolation.
- Preserve workspaces on failures. Inspect the exact task's status/logs; never run broad Docker prune or remove unrelated containers as a repair.
- A genuinely deleted worker container blocks its task and triggers credential cleanup; automatic container recreation is not implemented. Docker-daemon connection errors are not classified as missing containers. Resume clears prior finalization markers after successfully starting the same owned container with a fresh lease.
