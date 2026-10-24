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
        def request(url, headers, body, timeout):
            calls.append((url, json.loads(body), timeout))
            return 201, json.dumps({"token": "fake-token", "expires_at": "2099-01-01T00:00:00Z", "permissions": {"contents": "write", "pull_requests": "write"}}).encode()
        result = auth.mint_installation_token(self.config["github_app"], "owner/repo", http_request=request, now=0, signer=lambda *a: "jwt")
        self.assertEqual(result["token"], "fake-token")
        self.assertEqual(calls[0][1], {"repositories": ["repo"], "permissions": {"contents": "write", "pull_requests": "write"}})

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


if __name__ == "__main__":
    unittest.main()
