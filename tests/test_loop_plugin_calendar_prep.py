import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import loopkit
from loop_definition import LoopDefinition
from loop_plugins import calendar_prep as cp

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
GL = "https://gitlab.example.com"


def iso(minutes):
    return (NOW + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def event(eid="ev1", start=30, minutes=30, summary="Sync", description="", attendees=None,
          self_response="accepted", recurring="", join_url="https://meet.google.com/abc"):
    if attendees is None:
        attendees = [{"name": "", "email": "me@example.com", "response": self_response, "self": True},
                     {"name": "Ann", "email": "ann@example.com", "response": "accepted", "self": False}]
    return {"id": eid, "summary": summary, "start": iso(start), "end": iso(start + minutes),
            "html_link": f"https://calendar.google.com/{eid}", "attendees_count": len(attendees),
            "recurring_event_id": recurring, "description": description, "location": "",
            "join_url": join_url, "organizer": "boss@example.com", "attendees": attendees,
            "self_response": self_response}


class Calendar:
    def __init__(self, events, fail=False):
        self.events, self.fail, self.calls = events, fail, []

    def list_events(self, time_min, time_max, max_results=50):
        self.calls.append((time_min, time_max))
        if self.fail:
            raise RuntimeError("boom https://secret")
        return self.events


class GitLab:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def base_url(self):
        return GL

    def api(self, method, path, **kw):
        self.calls.append(path)
        if self.fail:
            raise RuntimeError("nope")
        if "/merge_requests/" in path:
            return {"title": "Fix login", "state": "opened", "updated_at": "2026-10-06T10:00:00Z",
                    "reviewers": [{"username": "me"}], "assignees": [], "detailed_merge_status": "mergeable",
                    "web_url": f"{GL}/g/p/-/merge_requests/7"}
        return {"title": "Login broken", "state": "opened", "updated_at": "2026-10-05T10:00:00Z",
                "assignees": [{"username": "ann"}], "web_url": f"{GL}/g/p/-/issues/3"}


class Ctx:
    def __init__(self, settings=None):
        self.now = NOW
        self.settings = settings or {}
        self.logs = []

    def log(self, message):
        self.logs.append(message)


def plugin(tmp_path, events=None, calendars=None, gitlab=None, mail_rows=None, mail_error=None, cli="claude"):
    calendars = calendars if calendars is not None else {"cal": Calendar(events if events is not None else [event()])}
    gitlab = gitlab or GitLab()
    mail_calls = []

    def mail_fn(addresses, days, limit):
        mail_calls.append((list(addresses), days, limit))
        if mail_error:
            raise RuntimeError(mail_error)
        return mail_rows or []

    def accounts_fn(capability):
        if capability == "calendar":
            return [{"id": cid} for cid in calendars]
        if capability == "issues":
            return [{"id": "gl", "type": "gitlab"}]
        return []

    def loader(aid):
        return calendars[aid] if aid in calendars else gitlab

    p = cp.CalendarPrep(accounts_fn=accounts_fn, loader=loader, mail_fn=mail_fn, cli_fn=lambda: cli,
                        series_path=tmp_path / "series.json", today_path=tmp_path / "today.json", briefs_path=tmp_path / "briefs.json")
    p.mail_calls = mail_calls
    return p


def test_discover_one_item_per_upcoming_meeting(tmp_path):
    items = plugin(tmp_path).discover(Ctx())
    assert len(items) == 1
    item = items[0]
    assert item.key == f"cal:cal:ev1:{iso(30)}"
    assert item.title == "Sync" and item.url == "https://calendar.google.com/ev1"
    ev = item.payload["event"]
    assert ev["title"] == "Sync" and ev["join_url"] == "https://meet.google.com/abc"
    assert ev["attendees"] == ["Ann"]


def test_discover_fetches_the_whole_local_day_in_one_call(tmp_path):
    cal = Calendar([event()])
    plugin(tmp_path, calendars={"cal": cal}).discover(Ctx({"lead_minutes": "60"}))
    assert len(cal.calls) == 1
    t_min, t_max = cal.calls[0]
    midnight = NOW.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    assert t_min == midnight.isoformat()
    assert t_max == (midnight + timedelta(days=1)).isoformat()


def test_discover_still_only_briefs_the_lead_window(tmp_path):
    events = [event("soon", start=50), event("later", start=61), event("earlier", start=-120)]
    keys = [i.key.split(":")[2] for i in plugin(tmp_path, events=events).discover(Ctx({"lead_minutes": "60"}))]
    assert keys == ["soon"]


def _today(tmp_path):
    return json.loads((tmp_path / "today.json").read_text())


def test_discover_records_todays_meetings_for_the_live_page(tmp_path):
    events = [event("past", start=-120), event("now", start=-10), event("soon", start=30),
              dict(event("allday"), start="2026-10-07", end="2026-10-08"),
              event("declined", self_response="declined")]
    plugin(tmp_path, events=events).discover(Ctx())
    data = _today(tmp_path)
    assert data["generated_at"] == NOW.isoformat()
    assert [m["key"].split(":")[2] for m in data["meetings"]] == ["past", "now", "soon"]
    soon = data["meetings"][2]
    assert soon["key"] == f"cal:cal:soon:{iso(30)}" and soon["title"] == "Sync"
    assert soon["start"] == iso(30) and soon["end"] == iso(60)
    assert soon["join_url"] == "https://meet.google.com/abc"
    assert soon["html_link"] == "https://calendar.google.com/soon"


def test_a_failing_calendar_keeps_the_previous_snapshot(tmp_path):
    plugin(tmp_path, events=[event("keep")]).discover(Ctx())
    plugin(tmp_path, calendars={"cal": Calendar([], fail=True)}).discover(Ctx())
    assert [m["key"].split(":")[2] for m in _today(tmp_path)["meetings"]] == ["keep"]


def test_discover_skips_all_day_started_declined_and_solo_events(tmp_path):
    solo = [{"name": "", "email": "me@example.com", "response": "accepted", "self": True}]
    events = [
        dict(event("allday"), start="2026-10-07", end="2026-10-08"),
        event("started", start=-5),
        event("declined", self_response="declined"),
        event("solo", attendees=solo),
        event("far", start=90),
        event("ok"),
    ]
    keys = [i.key.split(":")[2] for i in plugin(tmp_path, events=events).discover(Ctx())]
    assert keys == ["ok"]
    keys = [i.key.split(":")[2] for i in plugin(tmp_path, events=events).discover(Ctx({"include_solo": "yes"}))]
    assert keys == ["solo", "ok"]


def test_invalid_lead_minutes_falls_back_and_is_clamped(tmp_path):
    assert cp._lead_minutes({"lead_minutes": "soon"}) == 45
    assert cp._lead_minutes({"lead_minutes": "5"}) == 15
    assert cp._lead_minutes({"lead_minutes": "500"}) == 120
    assert cp._mail_days({"mail_days": "x"}) == 30 and cp._mail_days({"mail_days": "0"}) == 0


def test_calendar_account_setting_limits_the_calendars(tmp_path):
    a, b = Calendar([event("a")]), Calendar([event("b")])
    items = plugin(tmp_path, calendars={"a": a, "b": b}).discover(Ctx({"calendar_account": "b"}))
    assert [i.key.split(":")[1] for i in items] == ["b"] and a.calls == []


def test_one_failing_calendar_does_not_stop_the_others(tmp_path):
    ctx = Ctx()
    items = plugin(tmp_path, calendars={"a": Calendar([], fail=True), "b": Calendar([event()])}).discover(ctx)
    assert len(items) == 1
    assert any("RuntimeError" in m for m in ctx.logs) and not any("secret" in m for m in ctx.logs)


def test_linked_gitlab_work_is_fetched_for_matching_urls_only(tmp_path):
    desc = (f"Agenda: {GL}/g/p/-/issues/3 and {GL}/g/p/-/merge_requests/7 "
            "and https://other.example.com/x/-/issues/1")
    gl = GitLab()
    item = plugin(tmp_path, events=[event(description=desc)], gitlab=gl).discover(Ctx())[0]
    assert gl.calls == ["/projects/g%2Fp/issues/3", "/projects/g%2Fp/merge_requests/7"]
    rows = item.payload["gitlab"]
    assert [r["kind"] for r in rows] == ["issue", "merge_request"]
    assert rows[0]["url"] == f"{GL}/g/p/-/issues/3" and rows[0]["assignees"] == ["ann"]
    assert rows[1]["merge_status"] == "mergeable" and rows[1]["reviewers"] == ["me"]


def test_gitlab_links_are_capped_and_failures_noted(tmp_path):
    desc = " ".join(f"{GL}/g/p/-/issues/{n}" for n in range(1, 9))
    gl = GitLab(fail=True)
    item = plugin(tmp_path, events=[event(description=desc)], gitlab=gl).discover(Ctx())[0]
    assert len(gl.calls) == 5
    assert item.payload["gitlab"] == [] and item.payload["gitlab_error"] == "GitLab links unavailable"


def test_mail_with_attendees_excludes_me(tmp_path):
    rows = [{"subject": "Plan", "from": "Ann <ann@example.com>", "date": "2026-10-06T07:00:00+00:00",
             "snippet": "x" * 900}]
    p = plugin(tmp_path, mail_rows=rows)
    item = p.discover(Ctx())[0]
    assert p.mail_calls == [(["boss@example.com", "ann@example.com"], 30, 10)]
    assert item.payload["mail"][0]["subject"] == "Plan" and len(item.payload["mail"][0]["snippet"]) <= 300


def test_mail_is_skipped_without_claude_or_when_turned_off(tmp_path):
    p = plugin(tmp_path, cli="codex")
    item = p.discover(Ctx())[0]
    assert p.mail_calls == [] and item.payload["mail"] == [] and "mail_error" in item.payload
    p = plugin(tmp_path)
    item = p.discover(Ctx({"mail_days": "0"}))[0]
    assert p.mail_calls == [] and "mail_error" not in item.payload


def test_mail_failure_is_noted_not_fatal(tmp_path):
    item = plugin(tmp_path, mail_error="token https://x").discover(Ctx())[0]
    assert item.payload["mail"] == [] and item.payload["mail_error"] == "mail unavailable"


def test_description_is_trimmed(tmp_path):
    item = plugin(tmp_path, events=[event(description="d" * 9000)]).discover(Ctx())[0]
    assert len(item.payload["event"]["description"]) <= 4000


ANSWER = {"summary": "Decide the login fix.", "agenda": ["Review MR"],
          "open_items": [{"text": "MR 7 waits on you", "link": f"{GL}/g/p/-/merge_requests/7"},
                         {"text": "evil", "link": "https://evil.example.com"}],
          "talking_points": ["<!channel> ship it"] + [f"p{n}" for n in range(9)],
          "follow_ups": ["Ann owes the test plan"]}


def _item_with_links(tmp_path, recurring="series1"):
    desc = f"{GL}/g/p/-/merge_requests/7"
    p = plugin(tmp_path, events=[event(description=desc, recurring=recurring)])
    return p, p.discover(Ctx())[0]


def test_after_item_sanitizes_and_keeps_only_offered_links(tmp_path):
    p, item = _item_with_links(tmp_path)
    out = p.after_item(item, ANSWER, Ctx())
    assert out.status == "done"
    brief = out.data["brief"]
    assert [o["link"] for o in brief["open_items"]] == [f"{GL}/g/p/-/merge_requests/7", ""]
    assert len(brief["talking_points"]) == 5 and "<" not in brief["talking_points"][0]


def test_past_brief_is_offered_next_time(tmp_path):
    p, item = _item_with_links(tmp_path)
    p.after_item(item, ANSWER, Ctx())
    saved = json.loads((tmp_path / "series.json").read_text())
    assert saved["series1"]["summary"] == "Decide the login fix."
    nxt = plugin(tmp_path, events=[event("ev2", recurring="series1")]).discover(Ctx())[0]
    assert nxt.payload["last_time"]["follow_ups"] == ["Ann owes the test plan"]


def test_digest_renders_the_brief(tmp_path):
    p, item = _item_with_links(tmp_path)
    out = p.after_item(item, ANSWER, Ctx())
    text = p.digest([out], Ctx())
    assert text.startswith("*Prep: Sync*")
    assert "in 30 min" in text and "Join: https://meet.google.com/abc" in text
    assert "> Decide the login fix." in text
    assert "*Agenda*\n\u2022 Review MR" in text
    assert "*Open items*\n\u2022 MR 7 waits on you (" + f"{GL}/g/p/-/merge_requests/7)" in text
    assert "*Raise*\n\u2022 " in text and "*From last time*\n\u2022 Ann owes the test plan" in text
    assert "; " not in text
    assert "<!channel>" not in text
    assert p.digest([loopkit.Outcome("k", "failed", "x")], Ctx()) is None


def test_after_item_records_the_brief_for_the_live_page(tmp_path):
    p, item = _item_with_links(tmp_path)
    p.after_item(item, ANSWER, Ctx())
    saved = json.loads((tmp_path / "briefs.json").read_text())[item.key]
    assert saved["summary"] == "Decide the login fix." and saved["prepared_at"] == NOW.isoformat()
    assert saved["agenda"] == ["Review MR"]


def test_recorded_briefs_are_capped_to_the_newest(tmp_path):
    p, item = _item_with_links(tmp_path)
    old = {f"k{n}": {"summary": "s", "prepared_at": f"2026-01-{n % 28 + 1:02d}T00:00:00+00:00"} for n in range(150)}
    (tmp_path / "briefs.json").write_text(json.dumps(old))
    p.after_item(item, ANSWER, Ctx())
    saved = json.loads((tmp_path / "briefs.json").read_text())
    assert len(saved) == cp.MAX_BRIEFS and item.key in saved


def test_outcome_links_to_the_meeting_link_else_the_calendar_event(tmp_path):
    p, item = _item_with_links(tmp_path)
    assert p.after_item(item, ANSWER, Ctx()).url == "https://meet.google.com/abc"
    p = plugin(tmp_path, events=[event(join_url="")])
    item = p.discover(Ctx())[0]
    assert p.after_item(item, ANSWER, Ctx()).url == "https://calendar.google.com/ev1"


def test_settings_fields_declared():
    assert [f.key for f in cp.SETTINGS_FIELDS] == ["calendar_account", "lead_minutes", "mail_days", "include_solo"]
    assert all(not f.required for f in cp.SETTINGS_FIELDS)
    assert cp.CalendarPrep.settings_fields == cp.SETTINGS_FIELDS


def test_definition_and_prompt_load():
    definition = LoopDefinition.from_yaml(REPO_ROOT / "loops" / "calendar-prep" / "loop.yaml")
    assert definition.name == "meeting-prep-loop"
    prompt = (REPO_ROOT / "loops" / "calendar-prep" / "prompt.md").read_text()
    assert "{{item_json}}" in prompt and "untrusted" in prompt


def test_template_registers_the_loop_disabled_every_15_minutes():
    loops = json.loads((REPO_ROOT / "config" / "loops.json.template").read_text())
    loops = loops["loops"] if isinstance(loops, dict) else loops
    entry = next(l for l in loops if l["name"] == "meeting-prep-loop")
    assert entry["entry_point"] == "bin.loop_plugins.calendar_prep" and entry["enabled"] is False
    assert entry["schedule"] == {"frequency": "hourly", "interval_minutes": 15}
    assert entry["requires"] == ["calendar"] and entry["routes_notifications"] is True
