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


@pytest.fixture(autouse=True)
def reset_fake():
    FakeSession.instances, FakeSession.feed_rows = [], FEED
    FakeSession.note_rows, FakeSession.msg_rows, FakeSession.error = NOTES, MSGS, None


def plugin(tmp_path, human=None):
    return la.LinkedInAutopilot(
        accounts_fn=lambda cap: [{"id": "li", "type": "linkedin"}, {"id": "other", "type": "rss"}],
        session_factory=FakeSession,
        human=human or lh.Humanizer(rng=random.Random(0), sleep=lambda s: None),
        seen=seen_store.SeenStore("linkedin-autopilot-loop-items", state_dir=tmp_path, now_fn=lambda: NOW))


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
    FakeSession.feed_rows = FakeSession.note_rows = FakeSession.msg_rows = []
    assert plugin(tmp_path).discover(ctx()) == []


@pytest.mark.parametrize("error,problem", [(lb.SessionExpired(), "session_expired"),
                                           (lb.ChallengeError(), "challenge"),
                                           (lb.BrowserUnavailable("x"), "browser_unavailable"),
                                           (RuntimeError("secret token"), "error")])
def test_problems_become_failed_outcomes_without_calling_the_model(tmp_path, error, problem):
    FakeSession.error = error
    p = plugin(tmp_path)
    [item] = p.discover(ctx())
    assert item.payload == {"problem": problem}
    prompt = p.build_prompt(item, ctx(repo_root=ROOT))
    reply = p.call_model(prompt, ctx(repo_root=ROOT))
    assert reply["cost_usd"] == 0.0 and json.loads(reply["text"])["summary"] == ""
    out = p.after_item(item, loopkit.parse_answer(reply["text"], p.output_keys), ctx())
    assert out.status == "failed" and out.data["problem"] == problem and "secret token" not in out.summary


def test_challenge_stops_before_reading_other_pages(tmp_path):
    FakeSession.error = lb.ChallengeError()
    plugin(tmp_path).discover(ctx())
    assert FakeSession.instances[0].calls == ["feed"]


def test_outside_active_hours_or_skip_day_discovers_nothing_unless_forced(tmp_path):
    p = plugin(tmp_path)
    assert p.discover(ctx(force=False, settings={"active_hours": "13-18"})) == []        # 10:00 UTC is outside
    assert FakeSession.instances == []
    skipper = lh.Humanizer(rng=random.Random(0), sleep=lambda s: None)
    skipper.skip_today = lambda: True
    assert plugin(tmp_path / "s", human=skipper).discover(ctx(force=False, settings={"active_hours": "9-21"})) == []
    assert len(plugin(tmp_path / "f", human=skipper).discover(ctx(force=True))) == 1


def test_random_start_delay_is_slept_unless_forced(tmp_path):
    slept = []
    human = lh.Humanizer(rng=random.Random(0), sleep=slept.append)
    human.skip_today = lambda: False
    plugin(tmp_path / "a", human=human).discover(ctx(force=False, settings={"active_hours": "9-21"}))
    assert slept and max(slept) > 0
    slept.clear()
    plugin(tmp_path / "b", human=human).discover(ctx(force=True))
    assert not any(s > 60 for s in slept)


def test_digest_lists_summary_links_and_problems(tmp_path):
    p = plugin(tmp_path)
    [item] = p.discover(ctx())
    out = p.after_item(item, {"summary": "Quiet day", "topics": ["rails"],
                              "worth_reading": [{"id": "urn:li:activity:2", "why": "useful"}],
                              "people": [{"id": "urn:li:activity:2", "why": "ships Rails"}]}, ctx())
    text = p.digest([out], ctx())
    assert "LinkedIn Autopilot - 2026-10-06" in text and "Quiet day" in text
    assert "30 notifications, 20 messages, 30 feed posts" in text
    assert "https://www.linkedin.com/feed/update/urn:li:activity:2/" in text and "useful" in text
    failed = loopkit.Outcome("k", "failed", "LinkedIn session expired - reconnect it on the Connectors page",
                             data={"problem": "session_expired"})
    assert "needs attention" in p.digest([failed], ctx()).lower()
    assert p.digest([], ctx()) is None


def test_definition_and_prompt_load():
    from loop_definition import LoopDefinition
    d = LoopDefinition.from_yaml(ROOT / "loops" / "linkedin-autopilot" / "loop.yaml")
    assert d.name == "linkedin-autopilot-loop"
    prompt = (ROOT / "loops" / "linkedin-autopilot" / "prompt.md").read_text()
    assert "{{item_json}}" in prompt and "{{settings_json}}" in prompt and "untrusted" in prompt
    assert "worth_reading" in prompt and "people" in prompt


def test_settings_fields_are_declared():
    keys = [f.key for f in la.SETTINGS_FIELDS]
    assert keys == ["headless", "active_hours", "topics"]
    headless = la.SETTINGS_FIELDS[0]
    assert headless.default == "no" and headless.options == ("no", "yes")


BAD_IDS = [["x"], {"a": 1}, 3, None, True]


def test_wrong_typed_ids_are_ignored(tmp_path):
    p = plugin(tmp_path)
    [item] = p.discover(ctx())
    answer = {"summary": "s", "topics": [], "worth_reading": [{"id": i, "why": "w"} for i in BAD_IDS],
              "people": [{"id": i, "why": "w"} for i in BAD_IDS]}
    out = p.after_item(item, answer, ctx())
    assert out.status == "done" and out.data["worth_reading"] == [] and out.data["people"] == []


@pytest.mark.parametrize("bad", [5, True, "rails", {"a": 1}])
def test_wrong_typed_lists_become_empty(tmp_path, bad):
    p = plugin(tmp_path)
    [item] = p.discover(ctx())
    out = p.after_item(item, {"summary": "s", "topics": bad, "worth_reading": bad, "people": bad}, ctx())
    assert out.status == "done"
    assert out.data["topics"] == [] and out.data["worth_reading"] == [] and out.data["people"] == []


def test_rows_are_marked_seen_even_if_validation_raises(tmp_path):
    p = plugin(tmp_path)
    [item] = p.discover(ctx())
    def boom(*a, **k): raise RuntimeError("unforeseen")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loopkit, "chat_text", boom)
        with pytest.raises(RuntimeError):
            p.after_item(item, {"summary": "s", "topics": ["a"], "worth_reading": [], "people": []}, ctx())
    [again] = plugin(tmp_path).discover(ctx())
    assert {r["id"] for r in again.payload["feed"]}.isdisjoint({r["id"] for r in item.payload["feed"]})


def test_digest_reports_a_crashed_run_without_problem(tmp_path):
    p = plugin(tmp_path)
    crashed = loopkit.Outcome("k", "failed", "TypeError: secret", data={})
    text = p.digest([crashed], ctx())
    assert "LinkedIn needs attention: the run failed - check the loop log" in text
    assert "secret" not in text
