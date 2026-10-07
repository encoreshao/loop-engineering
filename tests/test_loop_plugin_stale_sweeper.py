import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import loopkit
from loop_definition import LoopDefinition
from loop_plugins import stale_sweeper as ss

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
GL = "https://gitlab.example.com"


def row(iid, updated, title=None, ref=None, kind="issues"):
    return {"iid": iid, "title": title or f"Item {iid}", "updated_at": updated,
            "web_url": f"{GL}/g/web/-/{kind}/{iid}",
            "references": {"full": ref or f"g/web#{iid}"}}


class GitLab:
    def __init__(self, issues=(), mine=(), reviews=(), fail=()):
        self.data = {"issues": list(issues), "mine": list(mine), "reviews": list(reviews)}
        self.fail, self.calls = set(fail), []

    def api(self, method, path, **kw):
        self.calls.append(path)
        if path == "/user":
            return {"username": "me"}
        section = ("issues" if path.startswith("/issues") else
                   "reviews" if "reviewer_username" in path else "mine")
        if section in self.fail:
            raise RuntimeError("boom https://secret")
        return self.data[section]


class Ctx:
    def __init__(self, settings=None):
        self.now = NOW
        self.settings = settings or {}
        self.logs = []

    def log(self, message):
        self.logs.append(message)


def plugin(conns):
    return ss.StaleSweeper(accounts_fn=lambda cap: [{"id": aid, "type": "gitlab"} for aid in conns],
                           loader=lambda aid: conns[aid])


def test_queries_use_the_thresholds_and_my_username():
    gl = GitLab(issues=[row(1, "2026-09-01T00:00:00Z")])
    plugin({"gl": gl}).discover(Ctx({"stale_days": "10", "review_days": "2"}))
    issues, mine, reviews = gl.calls[1:]
    assert issues.startswith("/issues?") and "scope=assigned_to_me" in issues and "state=opened" in issues
    assert "updated_before=2026-09-27T09%3A00%3A00Z" in issues
    assert "scope=created_by_me" in mine and "updated_before=2026-09-27T09%3A00%3A00Z" in mine
    assert "reviewer_username=me" in reviews and "updated_before=2026-10-05T09%3A00%3A00Z" in reviews
    assert all("sort=asc" in c and "per_page=20" in c for c in (issues, mine, reviews))


def test_one_item_per_account_with_stale_work():
    items = plugin({"gl": GitLab(issues=[row(1, "2026-09-01T00:00:00Z")]), "empty": GitLab()}).discover(Ctx())
    assert [i.key for i in items] == ["stale:gl:2026-10-07"]
    sections = items[0].payload["sections"]
    assert [s["key"] for s in sections] == ["issues", "my_mrs", "reviews"]
    assert sections[0]["rows"][0] == {"ref": "g/web#1", "title": "Item 1", "idle_days": 36,
                                      "url": f"{GL}/g/web/-/issues/1"}


def test_failing_section_is_marked_and_logged_by_class_name():
    ctx = Ctx()
    items = plugin({"gl": GitLab(mine=[row(2, "2026-09-01T00:00:00Z", kind="merge_requests")],
                                 fail={"issues"})}).discover(ctx)
    sections = {s["key"]: s for s in items[0].payload["sections"]}
    assert sections["issues"]["error"] is True and sections["my_mrs"]["rows"]
    assert any("RuntimeError" in m for m in ctx.logs) and not any("secret" in m for m in ctx.logs)


def test_call_model_renders_the_list_without_a_model():
    item = plugin({"gl": GitLab(issues=[row(1, "2026-09-01T00:00:00Z", title="<!channel> Login broken")],
                                reviews=[row(7, "2026-10-01T00:00:00Z", kind="merge_requests")],
                                fail={"mine"})}).discover(Ctx())[0]
    p = plugin({})
    res = p.call_model(p.build_prompt(item, Ctx()), Ctx())
    assert res["cost_usd"] == 0
    text = res["text"]
    assert text.startswith("gl")
    assert "Assigned issues idle 14+ days (1):" in text
    assert f"- g/web#1 ‹!channel› Login broken (idle 36d) {GL}/g/web/-/issues/1" in text
    assert "My merge requests idle 14+ days: unavailable" in text
    assert "Reviews waiting on you 3+ days (1):" in text and "<!channel>" not in text


def test_after_item_and_digest():
    p = plugin({"gl": GitLab(issues=[row(1, "2026-09-01T00:00:00Z")])})
    item = p.discover(Ctx())[0]
    text = p.call_model(p.build_prompt(item, Ctx()), Ctx())["text"]
    out = p.after_item(item, text, Ctx())
    assert out.status == "done" and out.summary == "1 stale"
    digest = p.digest([out], Ctx())
    assert digest.startswith("Stale work - 2026-10-07") and "g/web#1" in digest
    assert p.digest([loopkit.Outcome("k", "failed", "x")], Ctx()) is None


def test_settings_parse_with_fallbacks():
    assert ss._days({"stale_days": "x"}, "stale_days", 14) == 14
    assert ss._days({"stale_days": "0"}, "stale_days", 14) == 1
    assert ss._days({"review_days": "999"}, "review_days", 3) == 365
    assert [f.key for f in ss.SETTINGS_FIELDS] == ["stale_days", "review_days"]
    assert ss.StaleSweeper.settings_fields == ss.SETTINGS_FIELDS


def test_runs_through_loopkit_without_a_model(tmp_path):
    import shutil
    shutil.copytree(REPO_ROOT / "loops" / "stale-sweeper", tmp_path / "loops" / "stale-sweeper")
    p = plugin({"gl": GitLab(issues=[row(1, "2026-09-01T00:00:00Z")])})
    sent = []
    outcomes = loopkit.run_plugin(p, "run_t", now=NOW, repo_root=tmp_path, results_dir=tmp_path / "r",
                                  events_dir=tmp_path / "e", state_dir=tmp_path / "s",
                                  history_dir=tmp_path / "h", lock_path=tmp_path / "lock",
                                  loop_lookup=lambda name: {"settings": {}},
                                  notifier=lambda loop, text: sent.append(text) or [("default", True, "sent")])
    assert [o.status for o in outcomes] == ["done"]
    assert sent and sent[0].startswith("Stale work - 2026-10-07")


def test_definition_loads_and_template_registers_the_loop():
    definition = LoopDefinition.from_yaml(REPO_ROOT / "loops" / "stale-sweeper" / "loop.yaml")
    assert definition.name == "stale-sweeper-loop"
    loops = json.loads((REPO_ROOT / "config" / "loops.json.template").read_text())
    entry = next(l for l in loops if l["name"] == "stale-sweeper-loop")
    assert entry["entry_point"] == "bin.loop_plugins.stale_sweeper" and entry["enabled"] is False
    assert entry["schedule"] == {"frequency": "weekly", "weekdays": [1], "hour": 9, "minute": 0}
    assert entry["requires"] == ["issues"] and entry["routes_notifications"] is True
