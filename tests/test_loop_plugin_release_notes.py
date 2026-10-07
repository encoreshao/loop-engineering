import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import loopkit
from loop_definition import LoopDefinition
from loop_plugins import release_notes as rn

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
WEB = "https://gitlab.example.com/g/web"


def tag(name, date, message=""):
    return {"name": name, "message": message, "commit": {"committed_date": date}}


def mr(iid, merged_at, title=None):
    return {"iid": iid, "title": title or f"MR {iid}", "labels": ["bug"], "author": {"username": "ann"},
            "web_url": f"{WEB}/-/merge_requests/{iid}", "description": "d" * 3000, "merged_at": merged_at}


class GitLab:
    def __init__(self, tags, mrs=None, fail=False):
        self.tags, self.mrs, self.fail, self.calls = tags, mrs or [], fail, []

    def api(self, method, path, **kw):
        self.calls.append(path)
        if self.fail:
            raise RuntimeError("boom https://secret")
        if "/repository/tags" in path:
            return self.tags
        if "/merge_requests" in path:
            return self.mrs
        return {"id": 7, "default_branch": "main", "web_url": WEB}


class Ctx:
    def __init__(self, settings=None):
        self.now = NOW
        self.settings = settings or {}
        self.logs = []

    def log(self, message):
        self.logs.append(message)


PROJECTS = [{"alias": "web", "gitlab_instance": "gl", "project_id": 7, "default_branch": "main"}]


def plugin(tmp_path, conn, projects=PROJECTS):
    return rn.ReleaseNotes(projects_fn=lambda: projects,
                           accounts_fn=lambda cap: [{"id": "gl", "type": "gitlab"}],
                           loader=lambda aid: conn, state_dir=tmp_path)


def baseline(tmp_path, tag_name="v1.0"):
    (tmp_path / "tags.json").write_text(json.dumps({"gl:7": tag_name}))


TAGS = [tag("v1.1", "2026-10-06T12:00:00Z", "Release 1.1"), tag("v1.0", "2026-10-01T12:00:00Z")]
MRS = [mr(3, "2026-10-07T08:00:00Z"), mr(2, "2026-10-05T10:00:00Z"), mr(1, "2026-10-02T10:00:00Z"),
       mr(0, "2026-09-30T10:00:00Z")]


def test_first_sight_records_a_baseline_and_writes_nothing(tmp_path):
    conn = GitLab(TAGS, MRS)
    assert plugin(tmp_path, conn).discover(Ctx()) == []
    assert json.loads((tmp_path / "tags.json").read_text()) == {"gl:7": "v1.1"}
    assert not any("/merge_requests" in c for c in conn.calls)


def test_no_item_when_the_newest_tag_is_already_handled(tmp_path):
    baseline(tmp_path, "v1.1")
    assert plugin(tmp_path, GitLab(TAGS, MRS)).discover(Ctx()) == []


def test_new_tag_yields_one_item_with_the_mrs_between_the_tags(tmp_path):
    baseline(tmp_path)
    conn = GitLab(TAGS, MRS)
    items = plugin(tmp_path, conn).discover(Ctx())
    assert len(items) == 1
    item = items[0]
    assert item.key == "rel:gl:7:v1.1" and item.title == "web v1.1"
    p = item.payload
    assert p["project"] == "web" and p["tag"] == "v1.1" and p["previous_tag"] == "v1.0"
    assert p["tag_message"] == "Release 1.1"
    assert [m["iid"] for m in p["mrs"]] == [1, 2]
    assert len(p["mrs"][0]["description"]) <= 1000 and p["mrs"][0]["author"] == "ann"
    mr_call = next(c for c in conn.calls if "/merge_requests" in c)
    assert "state=merged" in mr_call and "target_branch=main" in mr_call
    assert "updated_after=2026-10-01T12%3A00%3A00Z" in mr_call


def test_first_ever_tag_takes_every_mr_up_to_it(tmp_path):
    baseline(tmp_path, "none-yet")
    items = plugin(tmp_path, GitLab([tag("v0.1", "2026-10-06T12:00:00Z")], MRS)).discover(Ctx())
    assert [m["iid"] for m in items[0].payload["mrs"]] == [0, 1, 2]
    assert items[0].payload["previous_tag"] == ""


def test_projects_setting_limits_the_projects(tmp_path):
    other = PROJECTS + [{"alias": "api", "gitlab_instance": "gl", "project_id": 8, "default_branch": "main"}]
    conn = GitLab(TAGS, MRS)
    plugin(tmp_path, conn, other).discover(Ctx({"projects": "api"}))
    assert all("/projects/8/" in c for c in conn.calls)


def test_a_failing_project_is_logged_by_class_name(tmp_path):
    ctx = Ctx()
    assert plugin(tmp_path, GitLab(TAGS, fail=True)).discover(ctx) == []
    assert any("RuntimeError" in m for m in ctx.logs) and not any("secret" in m for m in ctx.logs)


ANSWER = {"highlights": [{"text": "Faster search", "mr": 2}, {"text": "<!channel> fake", "mr": 99}],
          "fixes": [{"text": f"fix {n}", "mr": 1} for n in range(15)],
          "internal": []}


def _item(tmp_path):
    baseline(tmp_path)
    p = plugin(tmp_path, GitLab(TAGS, MRS))
    return p, p.discover(Ctx())[0]


def test_after_item_writes_markdown_and_advances_the_baseline(tmp_path):
    p, item = _item(tmp_path)
    out = p.after_item(item, ANSWER, Ctx())
    assert out.status == "done"
    md = (tmp_path / "web-v1.1.md").read_text()
    assert md.startswith("# web v1.1")
    assert f"- Faster search ([!2]({WEB}/-/merge_requests/2))" in md
    assert "!99" not in md and "<!channel>" not in md
    assert md.count("- fix") == 10
    assert json.loads((tmp_path / "tags.json").read_text()) == {"gl:7": "v1.1"}
    assert out.data["path"] == str(tmp_path / "web-v1.1.md")


def test_markdown_file_name_is_safe(tmp_path):
    assert rn._file_name("web/x", "release 1.0/../../x") == "web_x-release_1.0_.._.._x.md"


def test_digest_summarises_each_tag(tmp_path):
    p, item = _item(tmp_path)
    out = p.after_item(item, ANSWER, Ctx())
    text = p.digest([out], Ctx())
    assert text.startswith("Release notes: web v1.1 - 2 highlights, 10 fixes, 0 internal")
    assert f"Faster search ({WEB}/-/merge_requests/2)" in text
    assert f"{WEB}/-/tags/v1.1" in text and "<!channel>" not in text
    assert p.digest([loopkit.Outcome("k", "failed", "x")], Ctx()) is None


def test_settings_fields_declared():
    assert [f.key for f in rn.SETTINGS_FIELDS] == ["projects"]
    assert rn.ReleaseNotes.settings_fields == rn.SETTINGS_FIELDS


def test_definition_and_prompt_load():
    definition = LoopDefinition.from_yaml(REPO_ROOT / "loops" / "release-notes" / "loop.yaml")
    assert definition.name == "release-notes-loop"
    prompt = (REPO_ROOT / "loops" / "release-notes" / "prompt.md").read_text()
    assert "{{item_json}}" in prompt and "untrusted" in prompt


def test_template_registers_the_loop_disabled_hourly():
    loops = json.loads((REPO_ROOT / "config" / "loops.json.template").read_text())
    entry = next(l for l in loops if l["name"] == "release-notes-loop")
    assert entry["entry_point"] == "bin.loop_plugins.release_notes" and entry["enabled"] is False
    assert entry["schedule"] == {"frequency": "hourly", "interval_hours": 1}
    assert entry["requires"] == ["merge_requests"] and entry["routes_notifications"] is True
