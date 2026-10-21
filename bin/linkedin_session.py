#!/usr/bin/env python3
"""Where the LinkedIn browser profile (the logged-in session) lives:
<LOOP_ENGINEERING_HOME or ~/.loop-engineering>/linkedin/<account_id>/profile/,
mode 0700, outside the repo. Resolved at call time so a sandboxed run never
reaches the real profile. No password is ever stored - only the browser's own
session cookies, which Playwright's Chromium does not strongly encrypt at
rest; the 0700 mode and wipe() are the protection."""
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import connectors_config

MARKER = "connected"


def base_dir():
    home = os.environ.get("LOOP_ENGINEERING_HOME")
    return (Path(home) if home else Path.home() / ".loop-engineering") / "linkedin"


def _account_dir(account_id):
    if not connectors_config.is_valid_id(account_id):
        raise ValueError("invalid LinkedIn account id")
    return base_dir() / account_id


def profile_dir(account_id):
    return _account_dir(account_id) / "profile"


def ensure_profile(account_id):
    profile = profile_dir(account_id)
    profile.mkdir(parents=True, exist_ok=True)
    for directory in (base_dir(), _account_dir(account_id), profile):
        os.chmod(directory, 0o700)
    return profile


def mark_connected(account_id):
    account = _account_dir(account_id)
    account.mkdir(parents=True, exist_ok=True)
    os.chmod(account, 0o700)
    (account / MARKER).write_text(datetime.now(timezone.utc).isoformat())


def is_connected(account_id):
    return (_account_dir(account_id) / MARKER).is_file()


def wipe(account_id):
    shutil.rmtree(_account_dir(account_id), ignore_errors=True)


def rename(old_id, new_id):
    old, new = _account_dir(old_id), _account_dir(new_id)
    if old.is_dir() and not new.exists():
        shutil.move(str(old), str(new))
