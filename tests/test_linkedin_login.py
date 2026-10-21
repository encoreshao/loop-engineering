# tests/test_linkedin_login.py
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import linkedin_browser as lb  # noqa: E402
import linkedin_login as ll  # noqa: E402
import linkedin_session as ls  # noqa: E402


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOP_ENGINEERING_HOME", str(tmp_path))


class Page:
    def __init__(self, reaches_feed=True): self.reaches_feed, self.urls, self.waited = reaches_feed, [], []
    def goto(self, url, **kw): self.urls.append(url)
    def wait_for_url(self, pattern, timeout=None):
        self.waited.append(pattern)
        if not self.reaches_feed: raise TimeoutError("never signed in")
    def wait_for_timeout(self, ms): pass


class Ctx:
    def __init__(self, page): self.pages, self.closed = [page], False
    def close(self): self.closed = True


class PW:
    stopped = False
    def stop(self): self.stopped = True


def launcher_for(page, seen):
    ctx, pw = Ctx(page), PW()
    def launcher(profile, headless):
        seen.update(profile=profile, headless=headless)
        return pw, ctx
    return launcher, ctx, pw


def test_login_opens_a_visible_window_and_marks_connected():
    page, seen = Page(), {}
    launcher, ctx, pw = launcher_for(page, seen)
    assert ll.login("li", launcher=launcher) is True
    assert seen["headless"] is False and page.urls == ["https://www.linkedin.com/login"]
    assert ls.is_connected("li") and ctx.closed and pw.stopped


def test_login_timeout_does_not_mark_connected():
    page, seen = Page(reaches_feed=False), {}
    launcher, ctx, pw = launcher_for(page, seen)
    assert ll.login("li", launcher=launcher) is False
    assert not ls.is_connected("li") and ctx.closed and pw.stopped


def test_login_reports_missing_chrome_without_a_traceback(capsys):
    def launcher(profile, headless):
        raise lb.BrowserUnavailable("x")
    assert ll.login("li", launcher=launcher) is False
    assert not ls.is_connected("li")
    assert "Could not start Chrome" in capsys.readouterr().err


def test_main_returns_1_when_chrome_is_missing(monkeypatch, capsys):
    def launcher(profile, headless):
        raise lb.BrowserUnavailable("x")
    monkeypatch.setattr(lb, "_playwright_launcher", launcher)
    assert ll.main(["li"]) == 1
    assert "Could not start Chrome" in capsys.readouterr().err


def test_main_rejects_bad_usage_and_bad_ids(capsys):
    assert ll.main([]) == 2
    assert ll.main(["../x"]) == 2
