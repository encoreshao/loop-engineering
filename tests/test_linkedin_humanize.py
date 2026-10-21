# tests/test_linkedin_humanize.py
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import linkedin_humanize as lh


def human(seed=0, now=100.0):
    slept = []
    return lh.Humanizer(rng=random.Random(seed), sleep=slept.append, clock=lambda: now), slept


def test_dwell_is_bounded_and_grows_with_text():
    h, _ = human()
    short = [h.dwell_seconds(0) for _ in range(200)]
    long_ = [h.dwell_seconds(3000) for _ in range(200)]
    assert all(lh.DWELL_MIN_S <= d <= lh.DWELL_MAX_S for d in short + long_)
    assert sum(long_) / 200 > sum(short) / 200


def test_dwell_varies():
    h, _ = human()
    assert len({round(h.dwell_seconds(500), 2) for _ in range(20)}) > 10


def test_two_page_loads_are_never_closer_than_min_gap():
    h, _ = human(now=100.0)           # frozen clock: the second load comes "immediately"
    assert h.gap_before_load() == 0.0
    assert h.gap_before_load() >= lh.MIN_GAP_S


def test_sleep_never_negative():
    h, slept = human()
    h.sleep(-3)
    assert slept == [0.0]


def test_scroll_plan_reaches_target_unevenly_and_sometimes_goes_back():
    h, _ = human(seed=3)
    plans = [h.scroll_plan(4000) for _ in range(40)]
    for plan in plans:
        assert sum(d for d, _ in plan) >= 4000
        assert all(0.3 <= p <= 2.2 for _, p in plan)
    assert len({tuple(d for d, _ in p) for p in plans}) > 20           # not the same every time
    assert any(d < 0 for plan in plans for d, _ in plan)                # occasional scroll back


def test_scroll_plan_terminates_for_small_and_zero():
    h, _ = human()
    assert h.scroll_plan(0) == []
    assert sum(d for d, _ in h.scroll_plan(50)) >= 50


def test_mouse_path_is_curved_and_ends_on_target():
    h, _ = human(seed=5)
    path = h.mouse_path((0, 0), (600, 0))
    assert len(path) >= 14 and path[-1] == (600, 0)
    assert any(abs(y) > 1 for _, y in path)          # not a straight line


def test_wander_pages_is_a_small_random_subset():
    h, _ = human(seed=1)
    pages = ["a", "b", "c", "d", "e"]
    picks = [h.wander_pages(pages) for _ in range(30)]
    assert all(1 <= len(p) <= 3 and set(p) <= set(pages) and len(set(p)) == len(p) for p in picks)
    assert len({tuple(p) for p in picks}) > 5
    assert h.wander_pages([]) == []


def test_skip_today_is_occasional():
    h, _ = human(seed=2)
    skips = sum(h.skip_today() for _ in range(2000))
    assert 80 < skips < 330                          # about 10%


def test_start_delay_within_bounds():
    h, _ = human()
    assert all(0 <= h.start_delay_seconds() <= lh.START_DELAY_MAX_S for _ in range(100))


def test_parse_active_hours():
    assert lh.parse_active_hours("8-18") == (8, 18)
    assert lh.parse_active_hours(" 9 - 21 ") == (9, 21)
    for bad in ("", None, "x", "18-9", "9-25", "-3-5", "9"):
        assert lh.parse_active_hours(bad) == (9, 21)
    assert lh.parse_active_hours("bad", default=(1, 2)) == (1, 2)


def test_within_hours():
    assert lh.within_hours(9, (9, 21)) and lh.within_hours(20, (9, 21))
    assert not lh.within_hours(21, (9, 21)) and not lh.within_hours(8, (9, 21))


class FakeMouse:
    def __init__(self, events=None):
        self.moves, self.wheels = [], []
        self.events = [] if events is None else events
    def move(self, x, y):
        self.moves.append((x, y))
        self.events.append(("move", x, y))
    def wheel(self, dx, dy): self.wheels.append((dx, dy))


class FakePage:
    def __init__(self, events=None):
        self.events = [] if events is None else events
        self.mouse = FakeMouse(self.events)
        self.gotos = []
    def goto(self, url, **kw):
        self.gotos.append((url, kw))
        self.events.append(("goto", url))


def test_goto_waits_for_gap_navigates_then_moves_cursor():
    feed, jobs = "https://www.linkedin.com/feed/", "https://www.linkedin.com/jobs/"
    events = []
    page = FakePage(events)
    h = lh.Humanizer(rng=random.Random(0), sleep=lambda s: events.append(("sleep", s)), clock=lambda: 50.0)
    hp = lh.HumanPage(page, h)
    hp.goto(feed)
    hp.goto(jobs)
    gotos = [i for i, e in enumerate(events) if e[0] == "goto"]
    assert [events[i][1] for i in gotos] == [feed, jobs]
    first, second = gotos
    # the first load is not preceded by a gap sleep
    assert not any(e[0] == "sleep" and e[1] >= lh.MIN_GAP_S for e in events[:first])
    # the gap sleep sits right before the second load, after the first one
    assert first < second - 1 and events[second - 1][0] == "sleep" and events[second - 1][1] >= lh.MIN_GAP_S
    # the cursor moves right after each load, and at least once before the next load
    for g, nxt in ((first, second), (second, len(events))):
        assert events[g + 1][0] == "move"
        assert any(e[0] == "move" for e in events[g + 1:nxt])
    assert page.gotos[0][1]["wait_until"] == "domcontentloaded"


def test_scroll_uses_wheel_steps_and_pauses():
    page = FakePage()
    h, slept = human(seed=4)
    lh.HumanPage(page, h).scroll(2000)
    assert sum(dy for _, dy in page.mouse.wheels) >= 2000
    assert len(slept) == len(page.mouse.wheels)


def test_dwell_sleeps_once():
    page = FakePage()
    h, slept = human()
    lh.HumanPage(page, h).dwell(800)
    assert len(slept) == 1 and lh.DWELL_MIN_S <= slept[0] <= lh.DWELL_MAX_S
