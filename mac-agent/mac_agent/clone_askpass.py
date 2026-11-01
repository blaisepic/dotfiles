#!/usr/bin/env python3
"""Git askpass for host-side, repository-scoped clone preparation."""
import json
import os
import re
import sys
import time
from pathlib import Path


def main():
    prompt = " ".join(sys.argv[1:]).lower()
    from urllib.parse import urlsplit
    match = re.search(r"https://[^\s'\"<>]+", prompt)
    if not match:
        return 1
    try:
        url = urlsplit(match.group(0).rstrip(':'))
        if url.hostname != 'github.com' or url.port not in (None, 443):
            return 1
        if url.username not in (None, 'x-access-token'):
            return 1
    except ValueError:
        return 1
    lease_path = Path(os.environ.get("MAC_AGENT_CLONE_LEASE", ""))
    expected = os.environ.get("MAC_AGENT_CLONE_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", expected):
        return 1
    if not ("username" in prompt or "password" in prompt):
        return 1
    try:
        if lease_path.is_symlink() or not lease_path.is_file():
            return 1
        st = lease_path.stat()
        if st.st_mode & 0o077 or st.st_uid != os.getuid():
            return 1
        data = json.loads(lease_path.read_text())
        if data.get("repository") != expected:
            return 1
        expiry = data.get("expires_at")
        if not isinstance(expiry, str):
            return 1
        from datetime import datetime
        expires = datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
        if expires <= time.time() + 30:
            return 1
        if "username" in prompt:
            print("x-access-token")
        else:
            token = data.get("token")
            if not isinstance(token, str) or not token:
                return 1
            print(token)
        return 0
    except (OSError, ValueError, TypeError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
