"""Shared, opt-in fixtures for this suite.

Nothing here is autouse on purpose, with one exception
(`_no_real_keychain`, below). A project-wide autouse fixture would
silently change the environment of every test module in `tests/`,
including ones whose behavior under it has never been verified - so a
module that wants one of these applies it itself with a one-line
module-local autouse fixture (see `_no_real_ai_cli` in
tests/test_gitlab_loop_runner.py).
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

# bash/env/python3 live here; the real `claude` (~/.local/bin) and `codex`
# (/opt/homebrew/bin) deliberately do not.
SANITIZED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


@pytest.fixture
def sanitized_path(monkeypatch):
    """PATH with the basics but NOT the real `claude`/`codex` binaries.

    Any test module that imports `gitlab_loop_runner` should apply this to
    every one of its tests, like so:

        @pytest.fixture(autouse=True)
        def _no_real_ai_cli(sanitized_path):
            pass

    Every such test is *supposed* to fake the agent invocation, but this
    repo now has three invokers (`invoke_issue_agent`,
    `invoke_batch_issue_agent`, `invoke_batch_end_of_run_agent`) and a test
    that forgets one otherwise reaches the REAL AI CLI against the REAL
    ~/.loop-engineering config - which happened once during development and
    hung the suite on a live 30-minute agent session. With this fixture that
    mistake fails fast with FileNotFoundError instead.

    Tests that want a fake CLI still prepend their own tmp bin dir to
    os.environ["PATH"] as usual; returns the sanitized value so a test can
    assert on it.
    """
    monkeypatch.setenv("PATH", SANITIZED_PATH)
    return SANITIZED_PATH


@pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch):
    """The one suite-wide guard: mail_auth's `security` wrapper fails the
    test instead of reaching the real macOS Keychain. A suite run once
    recorded a real mailbox's "Authorized" probe result. A test that fakes
    the Keychain still works - either by monkeypatching `_security` itself
    (it replaces this guard) or `subprocess.run` (the guard then delegates
    to the real wrapper, which calls the fake).

    Production code often wraps the Keychain in `except Exception`, which
    would swallow the guard's AssertionError, so every hit is also recorded
    on `request`-independent list `keychain_hits` and teardown fails the
    test if any were recorded. Yields the list; a test that deliberately
    provokes a hit must clear it."""
    import mail_auth
    real_security = mail_auth._security
    real_run = subprocess.run
    hits = []

    def guarded(args, stdin=None):
        if mail_auth.subprocess.run is real_run:
            hits.append(list(args))
            raise AssertionError("test reached the real Keychain")
        return real_security(args, stdin=stdin)
    monkeypatch.setattr(mail_auth, "_security", guarded)
    yield hits
    if hits:
        pytest.fail(f"test reached the real Keychain: {hits}")


@pytest.fixture(autouse=True)
def _no_real_events_dir(monkeypatch, tmp_path_factory):
    """write_result now emits `loop.result` to events.DEFAULT_EVENTS_DIR
    (the real <repo>/outputs/events); point that at a scratch dir so no
    test can append to the live ledger. Tests that pass events_dir
    explicitly are unaffected."""
    import events
    monkeypatch.setattr(events, "DEFAULT_EVENTS_DIR", tmp_path_factory.mktemp("events"))
