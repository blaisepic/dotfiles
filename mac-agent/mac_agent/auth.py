"""Scoped GitHub App credentials and a file-based lease broker."""
import argparse
import base64
import json
import os
import re
import sys
import fcntl
import signal
import subprocess
import tempfile
import time
import stat
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.github.com"
OPENSSL = "/usr/bin/openssl"
TASK_RE = re.compile(r"^[a-f0-9]{12}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _private_dir(path):
    path = Path(path).expanduser().absolute()
    for ancestor in (path, *path.parents):
        if ancestor == Path("/") or ancestor == Path.home():
            break
        if ancestor.is_symlink():
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
    proc = subprocess.run([OPENSSL, "dgst", "-sha256", "-sign", str(key_path)], input=message, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=True)
    return header + "." + payload + "." + base64.urlsafe_b64encode(proc.stdout).rstrip(b"=").decode()


def _request(url, headers, body, timeout, method=None):
    req = urllib.request.Request(url, data=body, headers=headers, method=method or ("POST" if body else "GET"))
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
    st = os.stat(str(key), follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
        raise PermissionError("GitHub App key must be private")
    now = time.time() if now is None else now
    jwt = (signer or _sign)(app_id, key, now)
    body = json.dumps({"repositories": [repository.split("/", 1)[1]], "permissions": {"contents": "write", "pull_requests": "write"}}, separators=(",", ":")).encode()
    request = http_request or _request
    headers = {"Accept": "application/vnd.github+json", "Authorization": "Bearer " + jwt, "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"}
    owner, repo = repository.split("/", 1)
    status, raw = request(API + "/repos/" + owner + "/" + repo + "/installation", headers, None, 15, method="GET")
    if status != 200:
        raise RuntimeError("GitHub App installation check failed (HTTP {})".format(status))
    try:
        install = json.loads(raw.decode())
        if str(install["id"]) != str(install_id):
            raise PermissionError("installation id does not match configured installation")
        if install["account"]["login"].casefold() != repository.split("/", 1)[0].casefold():
            raise PermissionError("installation owner does not match requested repository")
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise RuntimeError("GitHub App returned an invalid installation response")
    status, raw = request(API + "/app/installations/" + str(install_id) + "/access_tokens", headers, body, 15, method="POST")
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
    registry = _private_dir(state / "broker")
    leases = _private_dir(state / "leases")
    task_dir = _private_dir(leases / task_id)
    cfg = config.get("github_app", {})
    key = Path(cfg.get("private_key_path", "")).expanduser().absolute()
    if key.is_symlink(): raise ValueError("unsafe App key path")
    tokenfile = task_dir / "lease.json"
    result = None
    try:
        with _lease_lock(state, task_id):
            result = mint_installation_token(cfg, repository)
            _atomic_json(tokenfile, {"token": result["token"], "expires_at": result["expires_at"], "repository": repository})
            _atomic_json(registry / (task_id + ".json"), {"task_id": task_id, "repository": repository, "config_path": config.get("_config_path"), "key_path": str(key), "app_id": cfg.get("app_id"), "installation_id": cfg.get("installation_id"), "lease_dir": str(task_dir)})
            (broker_start or _start_broker)(state)
    except Exception:
        revoke_ok = False
        try:
            if result is None: raise RuntimeError("token was not minted")
            revoke_status, _ = _request(API + "/installation/token", {"Accept":"application/vnd.github+json", "Authorization":"Bearer " + result["token"], "X-GitHub-Api-Version":"2022-11-28"}, None, 15, method="DELETE")
            revoke_ok = revoke_status in (204, 401)
        except Exception:
            pass
        if not revoke_ok:
            try:
                _atomic_json(registry / (task_id + "." + os.urandom(6).hex() + ".revoke.json"), {"task_id": task_id, "token": result["token"], "expires_at": result["expires_at"]})
            except Exception:
                raise RuntimeError("broker start failed; revocation could not be queued") from None
            try: _start_broker(state)
            except Exception: pass
        for path in (registry / (task_id + ".json"), tokenfile):
            try: path.unlink()
            except FileNotFoundError: pass
        raise RuntimeError("broker start failed; local lease rolled back") from None
    return task_dir


def _start_broker(state):
    broker = _private_dir(Path(state) / "broker")
    lock = open(broker / "broker.lock", "a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close(); return
    root = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ, PYTHONPATH=root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    log = open(broker / "broker.log", "a")
    os.chmod(broker / "broker.log", 0o600)
    command = [sys.executable, "-m", "mac_agent.auth", "serve", "--state-dir", str(state)]
    lock.close()
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, start_new_session=True, close_fds=True)
    log.close()


def revoke_lease(config, state_dir, task_id):
    if not isinstance(task_id, str) or not TASK_RE.fullmatch(task_id):
        raise ValueError("invalid task id")
    state = _private_dir(state_dir)
    failure = False
    with _lease_lock(state, task_id):
        reg = state / "broker" / (task_id + ".json")
        tokenfile = state / "leases" / task_id / "lease.json"
        if reg.is_symlink() or tokenfile.is_symlink():
            raise ValueError("unsafe lease path")
        try:
            if tokenfile.is_file():
                lease = json.loads(tokenfile.read_text())
                status, _ = _request(API + "/installation/token", {"Accept":"application/vnd.github+json", "Authorization":"Bearer " + lease["token"], "X-GitHub-Api-Version":"2022-11-28"}, None, 15, method="DELETE")
                failure = status not in (204, 401)
        except Exception:
            failure = True
        finally:
            if failure and tokenfile.is_file() and not tokenfile.is_symlink():
                try:
                    rec = json.loads(tokenfile.read_text())
                    _atomic_json(state / "broker" / (task_id + "." + os.urandom(6).hex() + ".revoke.json"), {"token": rec["token"], "expires_at": rec["expires_at"]})
                except Exception:
                    pass
            for path in (reg, tokenfile, state / "broker" / (task_id + ".error.json")):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
    if failure:
        try: _start_broker(state)
        except Exception: pass
        raise RuntimeError("Local lease removed; remote token revocation could not be verified")


def lease_status(state_dir, task_id):
    if not TASK_RE.fullmatch(task_id): raise ValueError("invalid task id")
    path = Path(state_dir).expanduser().absolute() / "leases" / task_id / "lease.json"
    if path.is_symlink(): return {"status": "unsafe"}
    try:
        data = json.loads(path.read_text())
        exp = _utc(data["expires_at"])
        result = {"status": "active" if exp > time.time() else "expired", "expires_at": data["expires_at"], "repository": data.get("repository")}
        error = Path(state_dir).expanduser().absolute() / "broker" / (task_id + ".error.json")
        if error.is_file() and not error.is_symlink():
            result["renewal_error"] = json.loads(error.read_text()).get("renewal_error", "unknown")
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "missing"}


from contextlib import contextmanager


@contextmanager
def _lease_lock(state, task_id):
    broker = _private_dir(Path(state) / "broker")
    path = broker / (task_id + ".lock")
    if path.is_symlink():
        raise ValueError("Unsafe lease lock")
    with open(path, "a+") as stream:
        os.chmod(path, 0o600)
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield


def _refresh_once(state_dir):
    state = _private_dir(state_dir)
    broker = _private_dir(state / "broker")
    for pending in broker.glob("[a-f0-9]" * 12 + "*.revoke.json"):
        task_id = pending.name[:12]
        with _lease_lock(state, task_id):
            if pending.is_symlink(): continue
            try:
                record = json.loads(pending.read_text())
                if _utc(record["expires_at"]) <= time.time(): pending.unlink(); continue
                status, _ = _request(API + "/installation/token", {"Accept":"application/vnd.github+json", "Authorization":"Bearer " + record["token"], "X-GitHub-Api-Version":"2022-11-28"}, None, 15, method="DELETE")
                if status in (204, 401): pending.unlink()
            except Exception: pass
    active = 0
    for reg in broker.glob("[a-f0-9]" * 12 + ".json"):
        task_id = reg.stem
        with _lease_lock(state, task_id):
            if not reg.exists() or reg.is_symlink():
                continue
            active += 1
            error = broker / (task_id + ".error.json")
            try:
                item = json.loads(reg.read_text())
                lease_dir = state / "leases" / task_id
                if item["task_id"] != task_id or item["lease_dir"] != str(lease_dir) or lease_dir.is_symlink():
                    raise ValueError("Lease identity mismatch")
                lease = lease_dir / "lease.json"
                if lease.is_symlink():
                    raise ValueError("Unsafe lease file")
                current = json.loads(lease.read_text())
                if current["repository"] != item["repository"]:
                    raise ValueError("Lease scope mismatch")
                if _utc(current["expires_at"]) > time.time() + 180:
                    continue
                cfg = {"app_id": item["app_id"], "installation_id": item["installation_id"], "private_key_path": item["key_path"]}
                fresh = mint_installation_token(cfg, item["repository"])
                _atomic_json(lease, fresh)
                if error.exists():
                    error.unlink()
            except Exception as exc:
                _atomic_json(error, {"renewal_error": type(exc).__name__, "at": datetime.now(timezone.utc).isoformat()})
    return active


def _serve(state_dir):
    state = _private_dir(state_dir)
    broker = _private_dir(state / "broker")
    pid_path = broker / "broker.pid"
    with open(broker / "broker.lock", "a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        os.chmod(broker / "broker.lock", 0o600)
        _atomic_json(pid_path, {"pid": os.getpid()})
        stop = False
        def halt(*_):
            nonlocal stop
            stop = True
        signal.signal(signal.SIGTERM, halt)
        signal.signal(signal.SIGINT, halt)
        try:
            while not stop:
                _refresh_once(state)
                for _ in range(15):
                    if stop:
                        break
                    time.sleep(1)
        finally:
            try:
                pid_path.unlink()
            except FileNotFoundError:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("serve", nargs="?")
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args()
    _serve(args.state_dir)

if __name__ == "__main__": main()
