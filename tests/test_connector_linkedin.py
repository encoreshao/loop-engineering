import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import connectors
import linkedin_browser as lb
import linkedin_session as ls
from connectors import base


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOP_ENGINEERING_HOME", str(tmp_path))


def make():
    cls = connectors.get_type("linkedin")
    return cls({"id": "li", "type": "linkedin", "settings": {}}, secret=None)


class FakeSession:
    outcome = True
    seen = {}
    def __init__(self, account_id, headless=False, **kw): FakeSession.seen = {"id": account_id, "headless": headless}
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def session_ok(self):
        if isinstance(FakeSession.outcome, Exception): raise FakeSession.outcome
        return FakeSession.outcome


def test_type_metadata():
    cls = connectors.get_type("linkedin")
    assert cls.capabilities == frozenset({base.SOCIAL}) and base.SOCIAL == "social"
    assert cls.auth == "browser_session" and cls.secret_label is None and cls.fields == ()
    assert cls.validate({}, None, True) == []


def test_not_connected_is_reported_without_opening_a_browser(monkeypatch):
    monkeypatch.setattr(lb, "Session", lambda *a, **k: pytest.fail("must not open a browser"))
    ok, msg = make().test()
    assert ok is False and "Connect LinkedIn" in msg


def test_connected_and_signed_in(monkeypatch):
    ls.ensure_profile("li"); ls.mark_connected("li")
    monkeypatch.setattr(lb, "Session", FakeSession)
    FakeSession.outcome = True
    assert make().test() == (True, "Signed in to LinkedIn")
    assert FakeSession.seen == {"id": "li", "headless": True}        # the probe is always headless


@pytest.mark.parametrize("exc,needle", [
    (lb.SessionExpired(), "expired"),
    (lb.ChallengeError(), "security check"),
    (lb.BrowserUnavailable("x"), "Playwright"),
    (RuntimeError("secret detail"), "RuntimeError"),
])
def test_failures_are_plain_messages_and_never_raise(monkeypatch, exc, needle):
    ls.ensure_profile("li"); ls.mark_connected("li")
    monkeypatch.setattr(lb, "Session", FakeSession)
    FakeSession.outcome = exc
    ok, msg = make().test()
    assert ok is False and needle in msg and "secret detail" not in msg
