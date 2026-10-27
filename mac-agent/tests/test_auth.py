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
            if method == "GET": return 200, b'{"id":456,"account":{"login":"owner"}}'
            return 201, json.dumps({"token": "fake-token", "expires_at": "2099-01-01T00:00:00Z", "permissions": {"contents": "write", "pull_requests": "write"}}).encode()
        result = auth.mint_installation_token(self.config["github_app"], "owner/repo", http_request=request, now=0, signer=lambda *a: "jwt")
        self.assertEqual(result["token"], "fake-token")
        self.assertEqual(calls[0][0], auth.API + "/repos/owner/repo/installation")
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
            return 200, b'{"id":456,"account":{"login":"other"}}'
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
        with patch.object(auth, '_start_broker'), patch.object(auth, '_request', side_effect=OSError('offline')), self.assertRaises(RuntimeError):
            auth.revoke_lease(self.config, self.root / 'state', task)
        self.assertFalse((directory / 'lease.json').exists())
        self.assertFalse((self.root / 'state' / 'broker' / (task + '.json')).exists())

    def test_failed_broker_start_rolls_back_lease_and_attempts_revoke(self):
        task = 'a1b2c3d4e5f6'
        with patch.object(auth, 'mint_installation_token', return_value={'token': 'secret', 'expires_at': '2099-01-01T00:00:00Z', 'repository': 'owner/repo'}), patch.object(auth, '_request', return_value=(204, b'')) as request:
            with self.assertRaisesRegex(RuntimeError, 'broker'):
                auth.ensure_lease(self.config, self.root / 'state', task, 'owner/repo', broker_start=lambda _: (_ for _ in ()).throw(OSError('no broker')))
        self.assertFalse((self.root / 'state' / 'leases' / task / 'lease.json').exists())
        self.assertFalse((self.root / 'state' / 'broker' / (task + '.json')).exists())
        request.assert_called_once()

    def test_mint_checks_exact_installation_id_before_token_request(self):
        calls = []
        def request(url, headers, body, timeout, method=None):
            calls.append(url)
            return 200, b'{"id":999,"account":{"login":"owner"}}'
        with self.assertRaises(PermissionError):
            auth.mint_installation_token(self.config['github_app'], 'owner/repo', http_request=request, now=0, signer=lambda *a: 'jwt')
        self.assertEqual(len(calls), 1)

    def test_signing_rejects_non_regular_key_and_pins_openssl(self):
        with patch.object(auth.os, 'stat', return_value=type('S', (), {'st_mode': 0o010600, 'st_uid': os.getuid()})()):
            with self.assertRaises(PermissionError):
                auth.mint_installation_token(self.config['github_app'], 'owner/repo', http_request=lambda *a, **k: (0,b''), signer=lambda *a: 'jwt')

    def test_offline_stops_retain_each_token_until_retry_succeeds(self):
        task = 'a1b2c3d4e5f6'
        state = self.root / 'state'
        for token in ('first-secret', 'second-secret'):
            with patch.object(auth, 'mint_installation_token', return_value={
                    'token': token, 'expires_at': '2099-01-01T00:00:00Z', 'repository': 'owner/repo'}):
                auth.ensure_lease(self.config, state, task, 'owner/repo', broker_start=lambda _: None)
            with patch.object(auth, '_start_broker'), \
                 patch.object(auth, '_request', side_effect=OSError('offline')), \
                 self.assertRaises(RuntimeError):
                auth.revoke_lease(self.config, state, task)
        pending = list((state / 'broker').glob(task + '*.revoke.json'))
        self.assertEqual(len(pending), 2)
        self.assertEqual({json.loads(p.read_text())['token'] for p in pending}, {'first-secret', 'second-secret'})
        with patch.object(auth, '_request', return_value=(204, b'')) as request:
            self.assertEqual(auth._refresh_once(state), 0)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(list((state / 'broker').glob(task + '*.revoke.json')), [])

    def test_failed_revoke_persists_private_retry_record(self):
        task='a1b2c3d4e5f6'
        with patch.object(auth, 'mint_installation_token', return_value={'token':'pending-secret','expires_at':'2099-01-01T00:00:00Z','repository':'owner/repo'}):
            lease=auth.ensure_lease(self.config,self.root/'state',task,'owner/repo',broker_start=lambda _:None)
        with patch.object(auth, '_start_broker'), patch.object(auth, '_request', side_effect=OSError('offline')), self.assertRaises(RuntimeError):
            auth.revoke_lease(self.config,self.root/'state',task)
        pending = list((self.root/'state'/'broker').glob(task + '*.revoke.json'))
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].stat().st_mode & 0o777, 0o600)
        self.assertFalse((lease/'lease.json').exists())

    def test_broker_refresh_scan_remains_idle_without_active_leases(self):
        with patch.object(auth, '_refresh_once', return_value=0) as scan, patch.object(auth.time, 'sleep'), patch.object(auth.signal, 'signal'):
            def stop_after_idle(_):
                # Stop after observing that an empty scan did not end the daemon.
                raise RuntimeError('idle daemon remains alive')
            with patch.object(auth, 'time') as clock:
                clock.sleep.side_effect=stop_after_idle
                with self.assertRaisesRegex(RuntimeError, 'remains alive'):
                    auth._serve(self.root/'state')
        scan.assert_called_once()

if __name__ == "__main__":
    unittest.main()
