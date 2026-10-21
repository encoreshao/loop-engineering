# tests/test_loop_plugin_linkedin_autopilot.py
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import linkedin_browser as lb
import linkedin_humanize as lh
import loopkit
import seen_store
from loop_plugins import linkedin_autopilot as la

NOW = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


class C:
    now = NOW
    force = True
    settings = {"topics": "rails, ai agents"}
    log = staticmethod(lambda m: None)


def ctx(**kw):
    return type("Ctx", (C,), kw)()


FEED = [{"id": f"urn:li:activity:{i}", "text": f"post {i}", "url": f"https://www.linkedin.com/feed/update/urn:li:activity:{i}/",
         "profile": f"https://www.linkedin.com/in/p{i}/" if i % 2 == 0 else ""} for i in range(40)]
NOTES = [{"id": f"notification:{i}", "text": f"note {i}", "url": ""} for i in range(35)]
MSGS = [{"id": f"message:{i}", "text": f"dm {i}", "url": ""} for i in range(25)]


class FakeSession:
    instances = []
    feed_rows, note_rows, msg_rows, error = FEED, NOTES, MSGS, None
    def __init__(self, account_id, headless=False, human=None, **kw):
        self.args = {"account": account_id, "headless": headless}
        FakeSession.instances.append(self)
        self.calls = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def _go(self, name, rows):
        self.calls.append(name)
        if FakeSession.error: raise FakeSession.error
        return rows
    def feed(self): return self._go("feed", FakeSession.feed_rows)
    def notifications(self): return self._go("notifications", FakeSession.note_rows)
    def messages(self): return self._go("messages", FakeSession.msg_rows)
    def wander(self): self.calls.append("wander")


DISCONNECTS = []


@pytest.fixture(autouse=True)
def reset_fake():
    DISCONNECTS.clear()
    FakeSession.instances, FakeSession.feed_rows = [], FEED
    FakeSession.note_rows, FakeSession.msg_rows, FakeSession.error = NOTES, MSGS, None


def plugin(tmp_path, human=None, **kw):
    kw.setdefault("connected_fn", lambda aid: True)
    kw.setdefault("disconnect_fn", DISCONNECTS.append)
    kw.setdefault("loop_seen", seen_store.SeenStore("linkedin-autopilot-loop", state_dir=tmp_path / "loopseen", now_fn=lambda: NOW))
    kw.setdefault("schedule_hour_fn", lambda: None)
    return la.LinkedInAutopilot(
        accounts_fn=lambda cap: [{"id": "li", "type": "linkedin"}, {"id": "other", "type": "rss"}],
        session_factory=FakeSession,
        human=human or lh.Humanizer(rng=random.Random(0), sleep=lambda s: None),
        seen=seen_store.SeenStore("linkedin-autopilot-loop-items", state_dir=tmp_path, now_fn=lambda: NOW), **kw)


def test_discover_caps_and_sanitizes(tmp_path):
    FakeSession.feed_rows = [{"id": "urn:li:activity:1", "text": "<!channel> hi\n<@U1> " + "x" * 900,
                              "url": "https://www.linkedin.com/feed/update/urn:li:activity:1/", "profile": "javascript:alert(1)"}] + FEED[1:]
    [item] = plugin(tmp_path).discover(ctx())
    p = item.payload
    assert item.key == "linkedin:li:2026-10-06" and item.title == "li"
    assert len(p["feed"]) == 30 and len(p["notifications"]) == 30 and len(p["messages"]) == 20
    first = p["feed"][0]
    assert "<" not in first["text"] and ">" not in first["text"] and "\n" not in first["text"] and len(first["text"]) <= 300
    assert first["profile"] == ""
    assert p["topics"] == "rails, ai agents"
    assert [s.args["account"] for s in FakeSession.instances] == ["li"]          # only linkedin-type accounts


def test_headless_setting_reaches_the_session(tmp_path):
    plugin(tmp_path).discover(ctx(settings={"headless": "yes"}))
    plugin(tmp_path).discover(ctx(settings={}))
    assert [s.args["headless"] for s in FakeSession.instances] == [True, False]


def test_reads_feed_first_then_the_rest_in_varying_order(tmp_path):
    orders = set()
    for seed in range(12):
        FakeSession.instances = []
        plugin(tmp_path / str(seed), human=lh.Humanizer(rng=random.Random(seed), sleep=lambda s: None)).discover(ctx())
        calls = FakeSession.instances[0].calls
        assert calls[0] == "feed" and sorted(calls[1:]) == ["messages", "notifications", "wander"]
        orders.add(tuple(calls))
    assert len(orders) > 1


def test_after_item_validates_ids_and_marks_seen(tmp_path):
    p = plugin(tmp_path)
    [item] = p.discover(ctx())
    answer = {"summary": "A busy day <@U1>", "topics": ["rails", 5, "x" * 99],
              "worth_reading": [{"id": "urn:li:activity:2", "why": "w" * 400},
                                {"id": "urn:li:activity:2", "why": "dup"},
                                {"id": "urn:li:activity:99", "why": "never offered"}, "junk", {"id": 3}],
              "people": [{"id": "urn:li:activity:2", "why": "ships Rails"},
                         {"id": "urn:li:activity:1", "why": "no profile url offered"},
                         {"id": "evil", "why": "x"}]}
    out = p.after_item(item, answer, ctx())
    d = out.data
    assert out.status == "done" and d["counts"] == {"notifications": 30, "messages": 20, "feed": 30}
    assert [w["id"] for w in d["worth_reading"]] == ["urn:li:activity:2"] and len(d["worth_reading"][0]["why"]) <= 140
    assert [x["id"] for x in d["people"]] == ["urn:li:activity:2"]
    assert d["people"][0]["profile"] == "https://www.linkedin.com/in/p2/"
    assert "<" not in d["summary"] and d["topics"] == ["rails", "x" * 39 + "\u2026"]


def test_offered_rows_are_not_offered_again(tmp_path):
    p = plugin(tmp_path)
    [item] = p.discover(ctx())
    p.after_item(item, {"summary": "", "topics": [], "worth_reading": [], "people": []}, ctx())
    [again] = plugin(tmp_path).discover(ctx())
    ids = {r["id"] for r in again.payload["feed"]}
    assert ids.isdisjoint({r["id"] for r in item.payload["feed"]})       # only the rows beyond the cap remain


def test_nothing_new_means_no_item(tmp_path):
    p = plugin(tmp_path)
    [item] = p.discover(ctx())
    p.after_item(item, {"summary": "", "topics": [], "worth_reading": [], "people": []}, ctx())
    FakeSession.feed_rows, FakeSession.note_rows, FakeSession.msg_rows = FEED[:30], NOTES[:30], MSGS[:20]
    assert plugin(tmp_path).discover(ctx()) == []


def test_empty_feed_means_page_changed(tmp_path):
    FakeSession.feed_rows = []
    p = plugin(tmp_path)
    [item] = p.discover(ctx())
    assert item.payload == {"problem": "page_changed"}
    reply = p.call_model(p.build_prompt(item, ctx(repo_root=ROOT)), ctx(repo_root=ROOT))
    assert reply["cost_usd"] == 0.0
    out = p.after_item(item, loopkit.parse_answer(reply["text"], p.output_keys), ctx())
    assert out.status == "failed" and "layout was not recognised" in out.summary


def test_not_connected_is_a_problem_and_never_browses(tmp_path):
    p = plugin(tmp_path, connected_fn=lambda aid: False)
    [item] = p.discover(ctx())
    assert item.payload == {"problem": "not_connected"} and FakeSession.instances == []
    out = p.after_item(item, {}, ctx())
    assert "not connected" in out.summary


@pytest.mark.parametrize("error", [lb.SessionExpired(), lb.ChallengeError()])
def test_expired_or_challenged_session_is_disconnected(tmp_path, error):
    FakeSession.error = error
    plugin(tmp_path).discover(ctx())
    assert DISCONNECTS == ["li"]


def test_disconnect_failure_never_raises(tmp_path):
    FakeSession.error = lb.ChallengeError()
    def boom(aid): raise RuntimeError("x")
    [item] = plugin(tmp_path, disconnect_fn=boom).discover(ctx())
    assert item.payload == {"problem": "challenge"}


def test_next_run_after_disconnect_does_not_browse(tmp_path):
    state = {"on": True}
    FakeSession.error = lb.ChallengeError()
    p = plugin(tmp_path, connected_fn=lambda aid: state["on"], disconnect_fn=lambda aid: state.update(on=False))
    p.discover(ctx())
    FakeSession.instances = []
    [item] = p.discover(ctx())
    assert item.payload == {"problem": "not_connected"} and FakeSession.instances == []


def test_repeat_same_day_run_is_skipped_unless_forced(tmp_path):
    loop_seen = seen_store.SeenStore("linkedin-autopilot-loop", state_dir=tmp_path / "ls", now_fn=lambda: NOW)
    loop_seen.add("linkedin:li:2026-10-06")
    p = plugin(tmp_path, loop_seen=loop_seen)
    assert p.discover(ctx(force=False, settings={"active_hours": "9-21"})) == [] and FakeSession.instances == []
    assert len(p.discover(ctx(force=True))) == 1


def test_schedule_hour_outside_active_hours_is_reported(tmp_path):
    p = plugin(tmp_path, schedule_hour_fn=lambda: 7)
    [item] = p.discover(ctx(force=False, settings={"active_hours": "9-21"}))
    assert item.key == "linkedin:schedule:2026-10-06" and item.title == "schedule"
    assert item.payload == {"problem": "schedule_outside_hours"} and FakeSession.instances == []
    out = p.after_item(item, {}, ctx())
    assert out.status == "failed" and "schedule hour is outside its active hours" in out.summary
    assert plugin(tmp_path / "f", schedule_hour_fn=lambda: 7).discover(ctx(force=True, settings={"active_hours": "9-21"}))[0].payload.get("problem") is None


def test_manual_run_at_night_with_schedule_inside_window_is_silent(tmp_path):
    p = plugin(tmp_path, schedule_hour_fn=lambda: 10)
    night = datetime(2026, 10, 6, 3, 0, tzinfo=timezone.utc)
    assert p.discover(ctx(now=night, force=False, settings={"active_hours": "9-21"})) == []
    assert FakeSession.instances == []
