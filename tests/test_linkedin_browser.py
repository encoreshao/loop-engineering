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
    def click(self, x, y): pass


class FakePage:
    def __init__(self, pages, redirects=None):
        self.pages, self.redirects, self.url, self.visited = pages, redirects or {}, "about:blank", []
        self.mouse = FakeMouse()
        self.dumps = {}
    def goto(self, url, **kw):
        self.visited.append(url)
        self.url = self.redirects.get(url, url)
    def content(self): return self.pages.get(self.url, "")
    def evaluate(self, js):
        if "cards" in js:
            return self.dumps.get(self.url, {"text": "", "cards": [], "links": []})
        return 1000


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


def session(page, headless=False, extractor=None):
    ctx, pw = FakeCtx(page), FakePW()
    seen = {}
    def launcher(profile, hl):
        seen["profile"], seen["headless"] = profile, hl
        return pw, ctx
    human = lh.Humanizer(rng=random.Random(0), sleep=lambda s: None)
    return lb.Session("li", headless=headless, human=human, launcher=launcher, extractor=extractor), ctx, pw, seen


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


def test_session_ok_true_on_a_healthy_feed():
    s, *_ = session(FakePage({f"{lb.LINKEDIN}/feed/": FEED}))
    with s:
        assert s.session_ok() is True


def test_session_ok_login_redirect_raises_session_expired():
    page = FakePage({}, redirects={f"{lb.LINKEDIN}/feed/": f"{lb.LINKEDIN}/login"})
    s, *_ = session(page)
    with s, pytest.raises(lb.SessionExpired):
        s.session_ok()


def test_session_ok_checkpoint_raises_challenge():
    page = FakePage({}, redirects={f"{lb.LINKEDIN}/feed/": f"{lb.LINKEDIN}/checkpoint/challenge/1"})
    s, *_ = session(page)
    with s, pytest.raises(lb.ChallengeError):
        s.session_ok()


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
    assert len(page.visited) == 1


def test_missing_playwright_is_browser_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(lb.BrowserUnavailable):
        lb._playwright_launcher("/tmp/x", True)


def test_chrome_launch_failure_is_browser_unavailable_without_leaking_details(monkeypatch):
    import types
    pw = FakePW()

    class Chromium:
        def launch_persistent_context(self, *a, **kw):
            raise RuntimeError("secret")
    pw.chromium = Chromium()
    api = types.ModuleType("playwright.sync_api")
    api.sync_playwright = lambda: types.SimpleNamespace(start=lambda: pw)
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
    with pytest.raises(lb.BrowserUnavailable) as info:
        lb._playwright_launcher("/tmp/x", True)
    assert pw.stopped and "secret" not in str(info.value)


def test_close_errors_do_not_mask_the_real_error():
    page = FakePage({}, redirects={f"{lb.LINKEDIN}/feed/": f"{lb.LINKEDIN}/login"})
    s, ctx, pw, _ = session(page)
    ctx.close = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(lb.SessionExpired):
        with s:
            s.feed()
    assert pw.stopped


_DRIVE_VERBS = {"goto", "click", "dblclick", "fill", "type", "press", "hover", "tap", "check",
                "select_option", "evaluate", "reload", "go_back", "go_forward", "set_content", "wheel"}
_DRIVE_ATTRS = {"mouse", "keyboard", "touchscreen"}


def _violations(source):
    import ast
    hp = ast.dump(ast.parse("self.hp", mode="eval").body)
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in _DRIVE_ATTRS:
            found.append(f"line {node.lineno}: .{node.attr}")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _DRIVE_VERBS and ast.dump(node.func.value) != hp):
            found.append(f"line {node.lineno}: .{node.func.attr}() not on self.hp")
    return found


def test_violation_checker_catches_aliased_and_chained_driving():
    snippet = ("p = self.page\np.goto(u)\nself._ctx.new_page().click('x')\n"
               "self.page.mouse.move(1, 2)\nself.hp.goto(u)\n")
    found = _violations(snippet)
    assert len(found) == 3 and not any("line 5" in f for f in found)
    assert _violations("self.hp.goto(u)\nx = self.page.url\nself.page.content()\n") == []


def test_browser_module_never_moves_the_page_itself():
    """Navigation, scrolling, clicking, typing and cursor moves must go through
    linkedin_humanize.HumanPage (self.hp) so they are all paced like a person.
    Reading self.page.url / self.page.content() is fine."""
    assert _violations(Path(lb.__file__).read_text()) == []


def test_enter_closes_chrome_when_page_setup_fails():
    class BadCtx(FakeCtx):
        def __init__(self): self.pages = []; self.closed = False
        def new_page(self): raise RuntimeError("no page")
    ctx, pw = BadCtx(), FakePW()
    human = lh.Humanizer(rng=random.Random(0), sleep=lambda s: None)
    s = lb.Session("li", human=human, launcher=lambda p, h: (pw, ctx))
    with pytest.raises(RuntimeError):
        s.__enter__()
    assert ctx.closed and pw.stopped


NEW_LAYOUT = "<html><body><div class='x9'>no data-urn anywhere</div></body></html>"


def test_feed_falls_back_to_ai_extraction_when_the_markup_is_not_recognised():
    page = FakePage({f"{lb.LINKEDIN}/feed/": NEW_LAYOUT})
    page.dumps[f"{lb.LINKEDIN}/feed/"] = {"text": "Ada posted hello", "cards": [], "links": []}
    asked = []
    def extractor(kind, dump):
        asked.append((kind, dump["text"]))
        return [{"id": "feed:1", "text": "hello", "url": "", "profile": ""}]
    s, *_ = session(page, extractor=extractor)
    with s:
        assert [r["id"] for r in s.feed()] == ["feed:1"]
    assert asked == [("feed", "Ada posted hello")]


def test_recognised_markup_never_calls_the_model():
    page = FakePage({f"{lb.LINKEDIN}/feed/": FEED})
    s, *_ = session(page, extractor=lambda k, d: (_ for _ in ()).throw(AssertionError("model called")))
    with s:
        assert s.feed()[0]["id"] == "urn:li:activity:5"


def test_a_page_that_never_renders_raises_page_timeout():
    page = FakePage({f"{lb.LINKEDIN}/feed/": ""})
    page.evaluate = lambda js: 0
    t = [0.0]
    human = lh.Humanizer(rng=random.Random(0), sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    s = lb.Session("li", human=human, launcher=lambda p, h: (FakePW(), FakeCtx(page)))
    with s, pytest.raises(lb.PageTimeout):
        s.feed()


class SearchPage(FakePage):
    """Header search box present or absent; each results page offers 10 people."""
    def __init__(self, has_box=True):
        super().__init__({})
        self.has_box, self.typed, self.clicked, self.page_no = has_box, [], [], 1
        self.keyboard = self
    def locator(self, sel):
        page = self
        class Loc:
            first = None
            def wait_for(self, **kw):
                if not (page.has_box if "Search" in sel or "search" in sel else "People" in sel and page.has_people):
                    raise RuntimeError("absent")
            def bounding_box(self): return {"x": 10, "y": 10, "width": 100, "height": 20}
        Loc.first = Loc()
        page.clicked.append(sel)
        return Loc
    has_people = False
    def type(self, ch): self.typed.append(ch)
    def press(self, key):
        if key == "Enter":
            self.url = f"{lb.LINKEDIN}/search/results/all/?keywords=x"
    def goto(self, url, **kw):
        super().goto(url, **kw)
        if "page=" in url:
            self.page_no = int(url.split("page=")[1].split("&")[0])
        elif "/search/results/people" in url:
            self.page_no = 1
    def evaluate(self, js):
        if "cards" in js:
            base = (self.page_no - 1) * 10
            return {"text": "results", "links": [], "cards": [{"url": f"{lb.LINKEDIN}/in/p{base + i}", "avatar": "", "text": f"P{base + i}"} for i in range(10)]}
        return 1000


def people_extractor(kind, dump):
    assert kind == "people"
    return [{"name": c["text"], "url": c["url"], "avatar": "", "company": "", "position": ""} for c in dump["cards"]]


def test_search_people_types_into_the_header_search_and_pages_until_enough_new_people():
    page = SearchPage()
    s, *_ = session(page, extractor=people_extractor)
    known = {f"{lb.LINKEDIN}/in/p{i}" for i in range(5)}
    with s:
        rows = s.search_people("AI builder", want=20, known=known)
    assert "".join(page.typed) == "AI builder"
    assert len(rows) == 20 and not known & {r["url"] for r in rows}
    assert any("page=2" in u for u in page.visited) and not any("page=4" in u for u in page.visited)


def test_search_people_without_a_search_box_opens_the_people_results_url():
    page = SearchPage(has_box=False)
    s, *_ = session(page, extractor=people_extractor)
    with s:
        rows = s.search_people("AI builder", want=20)
    assert len(rows) == 20
    assert any("/search/results/people/?keywords=AI%20builder" in u for u in page.visited)


def test_search_people_stops_at_the_page_cap_and_when_a_page_has_no_people():
    page = SearchPage()
    s, *_ = session(page, extractor=lambda k, d: [])
    with s:
        assert s.search_people("x", want=50) == []
    assert not any("page=3" in u for u in page.visited)


def test_search_people_login_wall_raises_session_expired():
    page = SearchPage()
    page.redirects = {f"{lb.LINKEDIN}/feed/": f"{lb.LINKEDIN}/login"}
    s, *_ = session(page, extractor=people_extractor)
    with s, pytest.raises(lb.SessionExpired):
        s.search_people("x")
