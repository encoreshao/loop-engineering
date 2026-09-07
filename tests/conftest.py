"""Shared, opt-in fixtures for this suite.

Nothing here is autouse on purpose. A project-wide autouse fixture would
silently change the environment of every test module in `tests/`,
including ones whose behavior under it has never been verified - so a
module that wants one of these applies it itself with a one-line
module-local autouse fixture (see `_no_real_ai_cli` in
tests/test_gitlab_loop_runner.py).
"""
import pytest

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
