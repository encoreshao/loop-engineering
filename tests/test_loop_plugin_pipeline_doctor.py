import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import mail_http
from loop_plugins import pipeline_doctor as pd

REPO = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)


class C:
    now = NOW; settings = {}; repo_root = None
    logs = []
    def log(self, m): self.logs.append(m)


def test_trace_tail_only():
    text = "\n".join(f"line {i}" for i in range(1000))
    tail = pd.trace_tail(text, n=300)
    assert tail.splitlines()[0] == "line 700" and len(tail.splitlines()) == 300


def test_trace_tail_strips_ansi_and_sections():
    raw = "\x1b[31mERROR\x1b[0m boom\nsection_start:1700000000:step_script\r\x1b[0Kok\n"
    assert pd.trace_tail(raw) == "ERROR boom\nok"


def test_recurring_after_three_in_window(tmp_path):
    store = pd.FingerprintStore(tmp_path / "fp.json", now_fn=lambda: NOW)
    for _ in range(2): assert store.record("abc") is False
    assert store.record("abc") is True


def test_old_occurrences_fall_out_of_window(tmp_path):
    path = tmp_path / "fp.json"
    old = (NOW - timedelta(days=8)).isoformat()
    path.write_text(json.dumps({"abc": [old, old, old]}))
    store = pd.FingerprintStore(path, now_fn=lambda: NOW)
    assert store.record("abc") is False
    assert len(json.loads(path.read_text())["abc"]) == 1


def test_corrupt_store_starts_empty(tmp_path):
    path = tmp_path / "fp.json"; path.write_text("{not json")
    assert pd.FingerprintStore(path, now_fn=lambda: NOW).record("x") is False


def test_fingerprint_is_stable_12_hex():
    fp = pd.fingerprint("rspec", "Timeout in x")
    assert fp == pd.fingerprint("rspec", "Timeout in x") and len(fp) == 12
    assert fp != pd.fingerprint("lint", "Timeout in x")


class G:
    def __init__(self): self.calls = []; self.texts = []
    def api(self, method, path, **kw):
        self.calls.append((method, path))
        if path == "/user": return {"id": 1, "username": "me"}
        if "/pipelines?status=failed" in path: return [{"id": 100, "web_url": "https://gl/p/100"}]
        if path.startswith("/merge_requests?"): return [
            {"iid": 7, "project_id": 9, "head_pipeline": {"id": 200, "status": "failed", "web_url": "https://gl/p/200"}},
            {"iid": 8, "project_id": 9, "head_pipeline": {"id": 100, "status": "failed", "web_url": "https://gl/p/100"}},
            {"iid": 9, "project_id": 9, "head_pipeline": {"id": 300, "status": "success"}}]
        if "/jobs?scope" in path: return [{"id": 5, "name": "rspec", "stage": "test", "web_url": "https://gl/j/5"}]
        raise AssertionError(path)
    def api_text(self, path):
        self.texts.append(path); return "boom\n"


def _plugin(g, projects=None):
    return pd.PipelineDoctor(
        projects_fn=lambda: projects or [{"alias": "web", "gitlab_instance": "work", "project_id": 9, "default_branch": "main"}],
        accounts_fn=lambda cap: [{"id": "work", "type": "gitlab"}], loader=lambda i: g, state_dir=None)


def test_discover_failed_default_branch_and_my_mr_pipelines():
    g = G()
    keys = sorted(i.key for i in _plugin(g).discover(C()))
    assert keys == ["pipe:work:9#100", "pipe:work:9#200"]


def test_discover_only_issues_gets_and_builds_payload():
    g = G()
    items = {i.key: i for i in _plugin(g).discover(C())}
    assert all(m == "GET" for m, _ in g.calls)
    assert any("ref=main" in p and "updated_after=" in p and "per_page=20" in p for _, p in g.calls)
    payload = items["pipe:work:9#100"].payload
    assert payload["project"] == "web" and payload["pipeline_id"] == 100
    assert payload["jobs"] == [{"name": "rspec", "stage": "test", "web_url": "https://gl/j/5", "trace_tail": "boom"}]
    assert items["pipe:work:9#100"].url == "https://gl/p/100"
    assert "/projects/9/jobs/5/trace" in g.texts


def test_discover_resolves_path_to_id_and_default_branch():
    class R(G):
        def api(self, method, path, **kw):
            if path == "/projects/loop%2Fweb": self.calls.append((method, path)); return {"id": 9, "default_branch": "trunk"}
            return super().api(method, path, **kw)
    g = R()
    items = _plugin(g, [{"alias": "web", "gitlab_instance": "work", "project_id": "loop/web"}]).discover(C())
    assert any("/projects/9/pipelines?status=failed&ref=trunk" in p for _, p in g.calls)
    assert {i.key for i in items} == {"pipe:work:9#100", "pipe:work:9#200"}


def test_jobs_capped_and_trace_capped_and_failures_isolated():
    class M(G):
        def api(self, method, path, **kw):
            if "/jobs?scope" in path:
                return [{"id": i, "name": f"j{i}", "stage": "t", "web_url": ""} for i in range(10)]
            return super().api(method, path, **kw)
        def api_text(self, path):
            if path.endswith("/jobs/1/trace"): raise mail_http.MailHTTPError(500, "", "")
            return "x" * 100_000
    item = _plugin(M()).discover(C())[0]
    jobs = item.payload["jobs"]
    assert len(jobs) == pd.MAX_JOBS
    assert jobs[1]["trace_tail"] == "" and len(jobs[0]["trace_tail"]) <= pd.MAX_TRACE_CHARS


def test_account_failure_logs_class_name_only_and_others_continue():
    class Bad:
        def api(self, *a, **k): raise mail_http.MailHTTPError(500, "tok-secret", "https://gl/secret")
    ok = G()
    plugin = pd.PipelineDoctor(
        projects_fn=lambda: [{"alias": "a", "gitlab_instance": "bad", "project_id": 1, "default_branch": "main"},
                             {"alias": "web", "gitlab_instance": "work", "project_id": 9, "default_branch": "main"}],
        accounts_fn=lambda cap: [{"id": "bad", "type": "gitlab"}, {"id": "work", "type": "gitlab"}],
        loader=lambda i: Bad() if i == "bad" else ok)
    ctx = C(); ctx.logs = []
    assert len(plugin.discover(ctx)) == 2
    assert ctx.logs and all("secret" not in m for m in ctx.logs) and any("MailHTTPError" in m for m in ctx.logs)


def test_projects_for_other_instances_skipped():
    g = G()
    items = _plugin(g, [{"alias": "x", "gitlab_instance": "other", "project_id": 9, "default_branch": "main"}]).discover(C())
    assert items == []


def test_default_projects_fn_maps_config(monkeypatch):
    import loop_config
    monkeypatch.setattr(loop_config, "load_config", lambda *a, **k: {
        "gitlab_instance": "loop", "projects": {
            "web": {"project_id": "loop/web", "default_branch": "main"},
            "h": {"project_id": "o/h", "instance": "other"}}})
    rows = pd.default_projects()
    assert rows == [{"alias": "web", "gitlab_instance": "loop", "project_id": "loop/web", "default_branch": "main"},
                    {"alias": "h", "gitlab_instance": "other", "project_id": "o/h", "default_branch": None}]


def _item():
    return pd.loopkit.WorkItem("pipe:work:9#100", "web #100", "https://gl/p/100", {
        "account": "work", "project": "web", "project_id": 9, "pipeline_id": 100,
        "jobs": [{"name": "rspec", "stage": "test", "web_url": "u", "trace_tail": "x"}]})


def test_after_item_records_fingerprint_and_flags_recurring(tmp_path):
    ctx = C(); ctx.repo_root = tmp_path
    plugin = pd.PipelineDoctor(state_dir=tmp_path / "state")
    answer = {"category": "flaky", "culprit": "spec/x_spec.rb", "explanation": "Timeout\nmore",
              "suggested_fix": "retry", "confidence": 0.8}
    outs = [plugin.after_item(_item(), dict(answer), ctx) for _ in range(3)]
    assert [o.data["recurring"] for o in outs] == [False, False, True]
    assert outs[0].status == "done" and outs[0].summary == "[flaky] web #100: spec/x_spec.rb"
    assert outs[0].url == "https://gl/p/100" and outs[0].data["category"] == "flaky"
    assert (tmp_path / "state" / "fingerprints.json").exists()


def test_after_item_default_store_under_outputs_loops(tmp_path):
    ctx = C(); ctx.repo_root = tmp_path
    pd.PipelineDoctor().after_item(_item(), {"category": "lint", "culprit": "", "explanation": "e",
                                              "suggested_fix": "f", "confidence": 1}, ctx)
    assert (tmp_path / "outputs" / "loops" / "pipeline-doctor-loop" / "fingerprints.json").exists()


def test_after_item_normalises_bad_answer(tmp_path):
    ctx = C(); ctx.repo_root = tmp_path
    out = pd.PipelineDoctor(state_dir=tmp_path).after_item(_item(), {
        "category": "wizardry", "culprit": 5, "explanation": "e" * 5000, "suggested_fix": "f" * 900,
        "confidence": 7}, ctx)
    assert out.data["category"] == "unknown" and out.data["confidence"] == 1.0
    assert len(out.data["suggested_fix"]) <= 200 and len(out.data["explanation"]) <= 1000


def test_digest_puts_recurring_first():
    outs = [pd.loopkit.Outcome("a", "done", "[lint] web #1: rubocop", url="https://gl/p/1",
                               data={"category": "lint", "recurring": False, "suggested_fix": "run rubocop -a"}),
            pd.loopkit.Outcome("b", "done", "[flaky] web #2: spec/x_spec.rb", url="https://gl/p/2",
                               data={"category": "flaky", "recurring": True, "suggested_fix": "quarantine"})]
    text = pd.PipelineDoctor().digest(outs, None)
    assert text.index("https://gl/p/2") < text.index("https://gl/p/1")
    assert "🔁" in text.split("https://gl/p/2")[0]


def test_digest_chat_safe_capped_and_empty_none():
    o = pd.loopkit.Outcome("a", "done", "[lint] web #1: <!channel>", url="https://gl/p/1",
                           data={"category": "lint", "recurring": False, "suggested_fix": "<@U1> " + "z" * 500})
    text = pd.PipelineDoctor().digest([o], None)
    assert "<" not in text and ">" not in text and len(text) < 600
    assert pd.PipelineDoctor().digest([], None) is None


def test_definition_files_and_template():
    from loop_definition import LoopDefinition
    assert LoopDefinition.from_yaml(REPO / "loops/pipeline-doctor/loop.yaml").name == "pipeline-doctor-loop"
    assert (REPO / "loops/pipeline-doctor/prompt.md").read_text().count("{{item_json}}") == 1
    assert "untrusted" in (REPO / "loops/pipeline-doctor/prompt.md").read_text()
    entry = next(e for e in json.loads((REPO / "config/loops.json.template").read_text()) if e["name"] == "pipeline-doctor-loop")
    assert entry["requires"] == ["pipelines"] and entry["routes_notifications"] is True
    assert entry["schedule"] == {"frequency": "hourly", "interval_hours": 1} and entry["timeout_seconds"] == 1800
    assert entry["enabled"] is False and entry["entry_point"] == "bin.loop_plugins.pipeline_doctor"
