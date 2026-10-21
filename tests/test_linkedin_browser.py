import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import linkedin_browser as lb
import linkedin_humanize as lh

NOTES = '<article class="nt-card"><a href="/x">Ada liked your post</a></article>'
FEED = '<div data-urn="urn:li:activity:5"><a href="/in/ada/">Ada</a> hello</div>'


class FakeMouse:
    def move(self, x, y): pass
    def wheel(self, dx, dy): pass


class FakePage:
    def __init__(self, pages, redirects=None):
        self.pages, self.redirects, self.url, self.visited = pages, redirects or {}, "about:blank", []
        self.mouse = FakeMouse()
    def goto(self, url, **kw):
        self.visited.append(url)
        self.url = self.redirects.get(url, url)
    def content(self): return self.pages.get(self.url, "")


class FakeCtx:
    def __init__(self, page): self.pages = [page]; self.closed = False
    def new_page(self): return self.pages[0]
    def close(self): self.closed = True


class FakePW:
    stopped = False
    def stop(self): self.stopped = True


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOP_ENGINEERING_HOME", str(tmp_path))


def session(page, headless=False):
    ctx, pw = FakeCtx(page), FakePW()
    seen = {}
    def launcher(profile, hl):
        seen["profile"], seen["headless"] = profile, hl
        return pw, ctx
    human = lh.Humanizer(rng=random.Random(0), sleep=lambda s: None)
    return lb.Session("li", headless=headless, human=human, launcher=launcher), ctx, pw, seen


def test_reads_pages_and_parses(tmp_path):
    page = FakePage({f"{lb.LINKEDIN}/notifications/": NOTES, f"{lb.LINKEDIN}/feed/": FEED})
    s, ctx, pw, seen = session(page, headless=True)
    with s:
        assert [r["text"] for r in s.notifications()] == ["Ada liked your post"]
        assert s.feed()[0]["id"] == "urn:li:activity:5"
        assert s.messages() == []
    assert ctx.closed and pw.stopped
    assert seen["headless"] is True and seen["profile"].endswith("linkedin/li/profile")


def test_login_wall_raises_session_expired():
    page = FakePage({}, redirects={f"{lb.LINKEDIN}/feed/": f"{lb.LINKEDIN}/login"})
    s, *_ = session(page)
    with s, pytest.raises(lb.SessionExpired):
        s.feed()


def test_checkpoint_raises_challenge_and_stops_before_more_pages():
    page = FakePage({}, redirects={f"{lb.LINKEDIN}/notifications/": f"{lb.LINKEDIN}/checkpoint/challenge/1"})
    s, *_ = session(page)
    with s, pytest.raises(lb.ChallengeError):
        s.notifications()
    assert page.visited == [f"{lb.LINKEDIN}/notifications/"]


def test_security_heading_in_page_raises_challenge():
    page = FakePage({f"{lb.LINKEDIN}/feed/": "<html><head><title>Security Verification | LinkedIn</title></head><body><h1>Let's do a quick security check</h1></body></html>"})
    s, *_ = session(page)
    with s, pytest.raises(lb.ChallengeError):
        s.feed()


def test_session_ok_true_on_feed_false_paths_raise():
    s, *_ = session(FakePage({f"{lb.LINKEDIN}/feed/": FEED}))
    with s:
        assert s.session_ok() is True


def test_wander_visits_between_one_and_three_extra_pages():
    page = FakePage({})
    s, *_ = session(page)
    with s:
        s.wander()
    assert 1 <= len(page.visited) <= 3 and set(page.visited) <= set(lb.WANDER_URLS)


def test_wander_stops_on_challenge():
    page = FakePage({}, redirects={u: f"{lb.LINKEDIN}/checkpoint/x" for u in lb.WANDER_URLS})
    s, *_ = session(page)
    with s, pytest.raises(lb.ChallengeError):
        s.wander()


def test_missing_playwright_is_browser_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(lb.BrowserUnavailable):
        lb._playwright_launcher("/tmp/x", True)


def test_close_errors_do_not_mask_the_real_error():
    page = FakePage({}, redirects={f"{lb.LINKEDIN}/feed/": f"{lb.LINKEDIN}/login"})
    s, ctx, pw, _ = session(page)
    ctx.close = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(lb.SessionExpired):
        with s:
            s.feed()
    assert pw.stopped


def test_browser_module_never_moves_the_page_itself():
    """Navigation, scrolling, clicking, typing and cursor moves must go through
    linkedin_humanize.HumanPage (self.hp) so they are all paced like a person.
    Reading self.page.url / self.page.content() is fine."""
    import re
    source = Path(lb.__file__).read_text()
    direct = re.compile(r"\bpage\.(goto|click|dblclick|fill|type|press|hover|tap|check|select_option|evaluate|"
                        r"reload|go_back|go_forward|set_content|wheel|mouse|keyboard|touchscreen)\b")
    assert direct.findall(source) == []
    assert ".mouse" not in source and ".keyboard" not in source
