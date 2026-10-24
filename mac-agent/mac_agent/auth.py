"""Scoped GitHub App credentials and a file-based lease broker."""
import argparse
import base64
import json
import os
import re
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.github.com"
TASK_RE = re.compile(r"^[a-f0-9]{12}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _private_dir(path):
    path = Path(path).expanduser().absolute()
    if path.exists() and path.is_symlink():
        raise ValueError("unsafe symlink in state path")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(str(path), 0o700)
    return path


def _utc(value):
    if not isinstance(value, str):
        raise ValueError("invalid token expiry")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("invalid token expiry")
    return dt.timestamp()


def _sign(app_id, key_path, now):
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"iat": int(now) - 30, "exp": int(now) + 540, "iss": str(app_id)}, separators=(",", ":")).encode()).rstrip(b"=").decode()
    message = (header + "." + payload).encode()
    proc = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key_path)], input=message, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=True)
    return header + "." + payload + "." + base64.urlsafe_b64encode(proc.stdout).rstrip(b"=").decode()


def _request(url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def mint_installation_token(app_config, repository, http_request=None, now=None, signer=None):
    if not isinstance(repository, str) or not REPO_RE.fullmatch(repository):
        raise ValueError("invalid repository")
    app_id, install_id = app_config.get("app_id"), app_config.get("installation_id")
    key = Path(app_config.get("private_key_path", "")).expanduser().absolute()
    if not str(app_id).isdigit() or not str(install_id).isdigit() or not key.is_absolute() or key.is_symlink():
        raise ValueError("invalid GitHub App configuration")
    st = key.stat()
    if st.st_mode & 0o077:
        raise PermissionError("GitHub App key must be private")
    now = time.time() if now is None else now
    jwt = (signer or _sign)(app_id, key, now)
    body = json.dumps({"repositories": [repository.split("/", 1)[1]], "permissions": {"contents": "write", "pull_requests": "write"}}, separators=(",", ":")).encode()
    status, raw = (http_request or _request)(API + "/app/installations/" + str(install_id) + "/access_tokens", {"Accept": "application/vnd.github+json", "Authorization": "Bearer " + jwt, "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"}, body, 15)
    if status != 201:
        raise RuntimeError("GitHub App token request failed (HTTP {})".format(status))
    try:
        data = json.loads(raw.decode())
        expiry = data["expires_at"]
        if not data.get("token") or _utc(expiry) <= now + 30:
            raise ValueError()
        perms = data["permissions"]
        if perms.get("contents") != "write" or perms.get("pull_requests") != "write":
            raise ValueError()
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise RuntimeError("GitHub App returned an invalid scoped token response")
    return {"token": data["token"], "expires_at": expiry, "repository": repository}


def _repo_allowed(config, repository):
    if not isinstance(repository, str) or not REPO_RE.fullmatch(repository):
        raise ValueError("invalid repository")
    allowed = {v.get("github") for v in config.get("repositories", {}).values() if isinstance(v, dict)}
    if repository not in allowed:
        raise PermissionError("repository is not allowlisted")


def _atomic_json(path, value):
    fd, name = tempfile.mkstemp(prefix=".lease-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, separators=(",", ":")); f.flush(); os.fsync(f.fileno())
        os.replace(name, str(path))
    finally:
        if os.path.exists(name): os.unlink(name)


def ensure_lease(config, state_dir, task_id, repository, broker_start=None):
    if not TASK_RE.fullmatch(task_id): raise ValueError("invalid task id")
    _repo_allowed(config, repository)
    state = _private_dir(state_dir)
    leases = _private_dir(state / "leases")
    task_dir = _private_dir(leases / task_id)
    cfg = config.get("github_app", {})
    key = Path(cfg.get("private_key_path", "")).expanduser().absolute()
    if key.is_symlink(): raise ValueError("unsafe App key path")
    result = mint_installation_token(cfg, repository)
    _atomic_json(task_dir / "lease.json", {"token": result["token"], "expires_at": result["expires_at"], "repository": repository})
    registry = _private_dir(state / "broker")
    _atomic_json(registry / (task_id + ".json"), {"task_id": task_id, "repository": repository, "config_path": config.get("_config_path"), "key_path": str(key), "app_id": cfg.get("app_id"), "installation_id": cfg.get("installation_id"), "lease_dir": str(task_dir)})
    (broker_start or _start_broker)(state)
    return task_dir


def _start_broker(state):
    pidfile = state / "broker" / "broker.pid"
    try:
        pid = int(pidfile.read_text())
        os.kill(pid, 0)
        return
    except (OSError, ValueError):
        pass
    command = [os.environ.get("PYTHON", "python3"), "-m", "mac_agent.auth", "serve", "--state-dir", str(state)]
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def revoke_lease(config, state_dir, task_id):
    if not TASK_RE.fullmatch(task_id): raise ValueError("invalid task id")
    state = Path(state_dir).expanduser().absolute()
    reg = state / "broker" / (task_id + ".json")
    if reg.is_symlink(): raise ValueError("unsafe registry path")
    try:
        data = json.loads(reg.read_text())
        tokenfile = Path(data["lease_dir"]) / "lease.json"
        if tokenfile.is_file() and not tokenfile.is_symlink(): tokenfile.unlink()
        reg.unlink()
    except FileNotFoundError:
        return


def lease_status(state_dir, task_id):
    if not TASK_RE.fullmatch(task_id): raise ValueError("invalid task id")
    path = Path(state_dir).expanduser().absolute() / "leases" / task_id / "lease.json"
    if path.is_symlink(): return {"status": "unsafe"}
    try:
        data = json.loads(path.read_text())
        exp = _utc(data["expires_at"])
        return {"status": "active" if exp > time.time() else "expired", "expires_at": data["expires_at"], "repository": data.get("repository")}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "missing"}


def _serve(state_dir):
    state = _private_dir(state_dir)
    broker = _private_dir(state / "broker")
    pid_path = broker / "broker.pid"
    _atomic_json(pid_path, {"pid": os.getpid()})
    stop = False
    def halt(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, halt); signal.signal(signal.SIGINT, halt)
    while not stop:
        for reg in broker.glob("[a-f0-9]" * 12 + ".json"):
            try:
                if reg.is_symlink(): continue
                item = json.loads(reg.read_text())
                lease_dir = Path(item["lease_dir"])
                lease = lease_dir / "lease.json"
                current = json.loads(lease.read_text())
                if current["repository"] != item["repository"] or _utc(current["expires_at"]) > time.time() + 180:
                    continue
                cfg = {"app_id": item["app_id"], "installation_id": item["installation_id"], "private_key_path": item["key_path"]}
                fresh = mint_installation_token(cfg, item["repository"])
                _atomic_json(lease, fresh)
            except Exception:
                # Keep daemon alive; status exposes expiry rather than secret-bearing errors.
                continue
        time.sleep(15)
    try: pid_path.unlink()
    except OSError: pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("serve", nargs="?")
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args()
    _serve(args.state_dir)

if __name__ == "__main__": main()
