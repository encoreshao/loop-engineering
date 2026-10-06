import json, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from loop_plugins import daily_digest as dd

REPO = Path(__file__).resolve().parent.parent


class FakeGitLab:
    capabilities = frozenset({"issues", "merge_requests"})
    type = "gitlab"
    def __init__(self, fail=False): self.fail = fail; self.paths = []
    def api(self, method, path, **kw):
        self.paths.append(path)
        if self.fail: raise RuntimeError("401 https://secret/token")
        if path == "/user": return {"username": "enc"}
        if path.startswith("/todos"): return [{"action_name": "mentioned", "target": {"title": "T", "web_url": "https://gl/t/1"}, "project": {"path_with_namespace": "g/p"}}]
        return []


def ctx():
    class C: now = datetime(2026, 10, 6, 9, 30, tzinfo=timezone.utc); settings = {}; log = staticmethod(lambda m: None)
    return C()


def kw(**over):
    base = dict(loop_x_fn=lambda c: {}, inbox_fn=lambda: {}, topics_fn=lambda: {}, calendar_accounts_fn=lambda: [])
    base.update(over)
    return base


def test_collect_queries_gitlab_endpoints():
    gl = FakeGitLab()
    data = dd.collect(ctx(), accounts_fn=lambda cap: [{"id": "work", "type": "gitlab"}], loader=lambda i: gl, **kw())
    assert "/user" in gl.paths and any(p.startswith("/todos") for p in gl.paths)
    assert data["accounts"][0]["todos"][0]["url"] == "https://gl/t/1"


def test_collect_one_failing_account_does_not_fail_digest():
    data = dd.collect(ctx(), accounts_fn=lambda cap: [{"id": "a", "type": "gitlab"}, {"id": "b", "type": "gitlab"}],
                      loader=lambda i: FakeGitLab(fail=(i == "a")), **kw())
    assert data["accounts"][0]["error"] == "RuntimeError" and "todos" in data["accounts"][1]
    assert "secret" not in json.dumps(data)


def test_collect_caps_payload_size():
    class Big(FakeGitLab):
        def api(self, method, path, **kw):
            return {"username": "u"} if path == "/user" else [{"title": "x" * 500, "web_url": f"https://gl/{i}"} for i in range(500)]
    data = dd.collect(ctx(), accounts_fn=lambda cap: [{"id": "a", "type": "gitlab"}], loader=lambda i: Big(), **kw())
    assert len(json.dumps(data)) <= 60_000 and data["truncated"] is True


def test_collect_github_search():
    class GH:
        paths = []
        def api(self, method, path, **kw):
            self.paths.append(path)
            if path == "/user": return {"login": "ghu"}
            return {"items": [{"title": "PR", "html_url": "https://gh/1", "repository_url": "https://api.github.com/repos/o/r", "updated_at": "z"}]}
    gh = GH()
    data = dd.collect(ctx(), accounts_fn=lambda cap: [{"id": "g", "type": "github"}], loader=lambda i: gh, **kw())
    assert any("involves:ghu" in p and "2026-10-05" in p for p in gh.paths)
    assert data["accounts"][0]["involved"][0]["url"] == "https://gh/1"


def test_collect_meetings_and_failing_calendar():
    class Cal:
        def list_events(self, a, b, max_results=50):
            self.rng = (a, b)
            return [{"summary": "Standup", "start": "s", "end": "e", "html_link": "https://cal/1", "attendees_count": 3}]
    class BadCal:
        def list_events(self, a, b, max_results=50): raise ValueError("token abc")
    cal = Cal()
    loaders = {"c1": cal, "c2": BadCal()}
    data = dd.collect(ctx(), accounts_fn=lambda cap: [], loader=lambda i: loaders[i],
                      **kw(calendar_accounts_fn=lambda: [{"id": "c1"}, {"id": "c2"}]))
    assert data["meetings"][0]["events"][0]["url"] == "https://cal/1"
    assert data["meetings"][1] == {"account": "c2", "error": "ValueError"}
    assert cal.rng[0] < cal.rng[1]


def test_known_urls_collects_from_every_list():
    urls = dd._known_urls({"accounts": [{"todos": [{"url": "https://a"}]}], "meetings": [{"events": [{"url": "https://m"}]}],
                           "loop_x": {"completed": [{"url": "https://x"}]}})
    assert {"https://a", "https://m", "https://x"} <= urls


def test_after_item_drops_unknown_urls():
    item = dd.loopkit.WorkItem("digest:2026-10-06", "d", payload={"accounts": [{"todos": [{"url": "https://gl/t/1"}]}]})
    answer = {"needs_you": [{"text": "a", "url": "https://gl/t/1"}, {"text": "b", "url": "https://evil/x"}],
              "waiting_on_others": [], "loop_x_did": [], "fyi": [], "meetings": []}
    out = dd.DailyDigest().after_item(item, answer, ctx())
    assert [b["url"] for b in out.data["needs_you"]] == ["https://gl/t/1", ""]
    assert out.status == "done" and out.summary == "2 items"


def test_format_digest_sections_and_empty_message():
    text = dd.format_digest({"needs_you": [{"text": "Review !7", "url": "https://gl/m/7"}], "waiting_on_others": [], "loop_x_did": [],
                             "fyi": [], "meetings": [{"text": "Standup 10:00", "url": ""}]}, "2026-10-06")
    assert "Needs you" in text and "<https://gl/m/7|Review !7>" in text
    assert "Today's meetings" in text and "Standup 10:00" in text
    assert "Nothing" in dd.format_digest({"needs_you": [], "waiting_on_others": [], "loop_x_did": [], "fyi": [], "meetings": []}, "2026-10-06")


def test_output_keys_include_meetings():
    assert set(dd.DailyDigest.output_keys) == {"needs_you", "waiting_on_others", "loop_x_did", "fyi", "meetings"}


def test_discover_single_item_keyed_by_date(monkeypatch):
    monkeypatch.setattr(dd, "collect", lambda c, **kw: {"accounts": []})
    items = dd.DailyDigest().discover(ctx())
    assert [i.key for i in items] == ["digest:2026-10-06"]


def test_loop_x_yesterday_counts(tmp_path):
    ev = tmp_path / "2026-10-05.jsonl"
    ev.write_text("\n".join(json.dumps(e) for e in [
        {"event_type": "issue.completed", "project": "p", "issue_iid": 3, "data": {}},
        {"event_type": "issue.escalated", "project": "p", "issue_iid": 4, "data": {}},
        {"event_type": "issue.started", "project": "p", "issue_iid": 5, "data": {}}]) + "\n")
    out = dd._default_loop_x(ctx(), events_dir=tmp_path)
    assert out["completed"] == [{"project": "p", "issue_iid": 3}] and out["escalated"] == [{"project": "p", "issue_iid": 4}]


def test_topic_headlines(tmp_path):
    (tmp_path / "2026-10-04-news.md").write_text("# T\n\nold headline\n")
    (tmp_path / "2026-10-05-news.md").write_text("# T\n\nNew headline here\n\n## More\n")
    (tmp_path / "2026-10-05-ai-news.md").write_text("Other\n")
    out = dd._default_topics(history_dir=tmp_path)
    assert out == {"news": "New headline here", "ai-news": "Other"}


def test_definition_and_prompt():
    from loop_definition import LoopDefinition
    from loop_policy import PolicyEngine
    d = LoopDefinition.from_yaml(REPO / "loops" / "daily-digest" / "loop.yaml")
    assert d.actions == ["fetch_connector_data", "summarize", "send_notification"] and PolicyEngine().validate_definition(d) == []
    assert "{{item_json}}" in (REPO / "loops" / "daily-digest" / "prompt.md").read_text()


def test_template_entry_registered():
    entries = json.loads((REPO / "config" / "loops.json.template").read_text())
    entry = next(e for e in entries if e["name"] == "daily-digest-loop")
    assert entry["enabled"] is False and entry["requires"] == ["issues"] and entry["routes_notifications"] is True
    assert entry["entry_point"] == "bin.loop_plugins.daily_digest" and entry["description"]
