import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mac_agent import auth


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.key = self.root / "app.pem"
        self.key.write_text("test-only-key")
        self.key.chmod(0o600)
        self.config = {
            "state_dir": str(self.root / "state"),
            "repositories": {"dotfiles": {"github": "owner/repo"}},
            "github_app": {"app_id": "123", "installation_id": "456", "private_key_path": str(self.key)},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_mint_scopes_installation_token_and_returns_response(self):
        calls = []
        def request(url, headers, body, timeout, method=None):
            calls.append((url, json.loads(body) if body else None, timeout, method))
            if method == "GET": return 200, b'{"account":{"login":"owner"}}'
            return 201, json.dumps({"token": "fake-token", "expires_at": "2099-01-01T00:00:00Z", "permissions": {"contents": "write", "pull_requests": "write"}}).encode()
        result = auth.mint_installation_token(self.config["github_app"], "owner/repo", http_request=request, now=0, signer=lambda *a: "jwt")
        self.assertEqual(result["token"], "fake-token")
        self.assertEqual(calls[1][1], {"repositories": ["repo"], "permissions": {"contents": "write", "pull_requests": "write"}})

    def test_lease_file_is_private_and_status_never_exposes_token(self):
        with patch.object(auth, "mint_installation_token", return_value={"token": "sentinel-secret", "expires_at": "2099-01-01T00:00:00Z", "repository": "owner/repo"}):
            lease = auth.ensure_lease(self.config, self.root / "state", "a1b2c3d4e5f6", "owner/repo", broker_start=lambda _: None)
        record = json.loads((lease / "lease.json").read_text())
        self.assertEqual(record["token"], "sentinel-secret")
        self.assertEqual((lease / "lease.json").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("sentinel-secret", repr(auth.lease_status(self.root / "state", "a1b2c3d4e5f6")))

    def test_rejects_repository_not_in_exact_allowlist(self):
        with self.assertRaises((ValueError, PermissionError)):
            auth.ensure_lease(self.config, self.root / "state", "a1b2c3d4e5f6", "owner/other")

    def test_mint_verifies_installation_owner_before_minting(self):
        calls = []
        def request(url, headers, body, timeout, method=None):
            calls.append((url, method))
            if "/access_tokens" in url:
                return 201, b'{"token":"fake","expires_at":"2099-01-01T00:00:00Z","permissions":{"contents":"write","pull_requests":"write"}}'
            return 200, b'{"account":{"login":"other"}}'
        with self.assertRaises(PermissionError):
            auth.mint_installation_token(self.config["github_app"], "owner/repo", http_request=request, now=0, signer=lambda *a: "jwt")
        self.assertEqual(len(calls), 1)

    def test_revoke_deletes_remote_token(self):
        task = "a1b2c3d4e5f6"
        lease = self.root / "state" / "leases" / task
        lease.mkdir(parents=True)
        (lease / "lease.json").write_text(json.dumps({"token": "secret", "expires_at": "2099-01-01T00:00:00Z", "repository": "owner/repo"}))
        broker = self.root / "state" / "broker"; broker.mkdir(parents=True)
        (broker / (task + ".json")).write_text(json.dumps({"lease_dir": str(lease), "token": "secret"}))
        calls = []
        with patch.object(auth, "_request", side_effect=lambda url, headers, body, timeout, method=None: (calls.append((url, method)) or (204, b""))):
            auth.revoke_lease(self.config, self.root / "state", task)
        self.assertTrue(any(method == "DELETE" for _, method in calls))
        self.assertFalse((lease / "lease.json").exists())

    def test_broker_singleton_uses_exclusive_lock_and_python_executable(self):
        state = self.root / "state"; (state / "broker").mkdir(parents=True)
        with patch.object(auth.subprocess, "Popen") as popen:
            auth._start_broker(state)
        args, kwargs = popen.call_args
        self.assertEqual(args[0][0], auth.sys.executable)
        self.assertIn("PYTHONPATH", kwargs.get("env", {}))
        self.assertTrue((state / "broker" / "broker.lock").exists())

    def test_private_dir_rejects_symlinked_ancestor(self):
        target = self.root / "real"; target.mkdir()
        link = self.root / "link"; link.symlink_to(target)
        with self.assertRaises(ValueError):
            auth._private_dir(link / "child")

    def test_mint_rejects_key_not_owned_by_current_user(self):
        with patch.object(auth.os, "stat") as stat:
            stat.return_value = type("S", (), {"st_mode": 0o100600, "st_uid": -1})()
            with self.assertRaises(PermissionError):
                auth.mint_installation_token(self.config["github_app"], "owner/repo", http_request=lambda *a, **k: (0,b""), signer=lambda *a: "jwt")

    def test_refresh_renews_expiring_token_and_records_errors_without_secret(self):
        task = 'a1b2c3d4e5f6'
        expired = {'token': 'old-sentinel', 'expires_at': '2001-01-01T00:00:00Z', 'repository': 'owner/repo'}
        with patch.object(auth, 'mint_installation_token', return_value=expired):
            directory = auth.ensure_lease(self.config, self.root / 'state', task, 'owner/repo', broker_start=lambda _: None)
        fresh = dict(expired, token='new-sentinel', expires_at='2099-01-01T00:00:00Z')
        with patch.object(auth, 'mint_installation_token', return_value=fresh):
            auth._refresh_once(self.root / 'state')
        self.assertEqual(json.loads((directory / 'lease.json').read_text())['token'], 'new-sentinel')
        (directory / 'lease.json').write_text(json.dumps(expired))
        with patch.object(auth, 'mint_installation_token', side_effect=RuntimeError('secret-must-not-escape')):
            auth._refresh_once(self.root / 'state')
        safe = auth.lease_status(self.root / 'state', task)
        self.assertEqual(safe['status'], 'expired')
        self.assertIn('renewal_error', safe)
        self.assertNotIn('secret-must-not-escape', str(safe))

    def test_revoke_removes_local_lease_even_when_remote_is_unavailable(self):
        task = 'a1b2c3d4e5f6'
        with patch.object(auth, 'mint_installation_token', return_value={'token': 'secret', 'expires_at': '2099-01-01T00:00:00Z', 'repository': 'owner/repo'}):
            directory = auth.ensure_lease(self.config, self.root / 'state', task, 'owner/repo', broker_start=lambda _: None)
        with patch.object(auth, '_request', side_effect=OSError('offline')), self.assertRaises(RuntimeError):
            auth.revoke_lease(self.config, self.root / 'state', task)
        self.assertFalse((directory / 'lease.json').exists())
        self.assertFalse((self.root / 'state' / 'broker' / (task + '.json')).exists())

if __name__ == "__main__":
    unittest.main()
