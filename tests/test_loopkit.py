import json, sys
from datetime import datetime, timezone
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import loopkit
from loopkit import WorkItem, Outcome

LOOP_YAML = """name: demo-loop
version: 1
trigger: {type: schedule, schedule: "0 9 * * *"}
goal: {type: demo}
agent: {provider: claude, model: default}
actions: [read_issue]
stop_conditions: {max_iterations: 2, max_runtime_minutes: 5, max_cost_usd: 1, no_progress_iterations: 2}
retry: {enabled: true, max_attempts: 2}
"""


class Demo(loopkit.LoopPlugin):
    loop_name = "demo-loop"
    definition_dir = "demo"
    output_keys = ("verdict",)

    def __init__(self, items, answers, after=None):
        self.items, self.answers, self.after = items, list(answers), after
        self.prompts = []

    def discover(self, ctx): return list(self.items)
    def build_prompt(self, item, ctx): return f"judge {item.key}"
    def call_model(self, prompt, ctx):
        self.prompts.append(prompt)
        a = self.answers.pop(0)
        if isinstance(a, Exception): raise a
        return {"text": a, "cost_usd": 0.01}
    def after_item(self, item, answer, ctx):
        if self.after: return self.after(item, answer)
        return Outcome(item.key, "done", answer["verdict"])
    def digest(self, outcomes, ctx): return f"{len(outcomes)} done"


@pytest.fixture
def env(tmp_path):
    (tmp_path / "loops" / "demo").mkdir(parents=True)
    (tmp_path / "loops" / "demo" / "loop.yaml").write_text(LOOP_YAML)
    sent = []
    kw = dict(repo_root=tmp_path, results_dir=tmp_path / "results", events_dir=tmp_path / "events",
              state_dir=tmp_path / "state", history_dir=tmp_path / "history",
              loop_lookup=lambda n: {"name": n, "settings": {"k": "v"}},
              notifier=lambda loop, text: sent.append(text),
              now=datetime(2026, 10, 6, tzinfo=timezone.utc))
    return kw, sent


def test_happy_path_processes_items_and_digests(env):
    kw, sent = env
    p = Demo([WorkItem("a", "A"), WorkItem("b", "B")], ['{"verdict": "ok"}', '{"verdict": "ok"}'])
    out = loopkit.run_plugin(p, "run_1", **kw)
    assert [o.status for o in out] == ["done", "done"]
    assert sent == ["2 done"]


def test_seen_items_skipped_next_run(env):
    kw, _ = env
    loopkit.run_plugin(Demo([WorkItem("a", "A")], ['{"verdict": "ok"}']), "run_1", **kw)
    p = Demo([WorkItem("a", "A")], [])
    assert loopkit.run_plugin(p, "run_2", **kw) == [] and p.prompts == []


def test_contract_violation_retries_with_feedback(env):
    kw, _ = env
    p = Demo([WorkItem("a", "A")], ["not json", '{"verdict": "ok"}'])
    out = loopkit.run_plugin(p, "run_1", **kw)
    assert out[0].status == "done"
    assert "violated the output contract" in p.prompts[1]


def test_failed_item_not_marked_seen_and_run_continues(env):
    kw, sent = env
    p = Demo([WorkItem("a", "A"), WorkItem("b", "B")], [RuntimeError("boom"), RuntimeError("boom"), '{"verdict": "ok"}'])
    out = loopkit.run_plugin(p, "run_1", **kw)
    assert [o.status for o in out] == ["failed", "done"]
    p2 = Demo([WorkItem("a", "A")], ['{"verdict": "ok"}'])
    assert loopkit.run_plugin(p2, "run_2", **kw)[0].status == "done"   # 'a' retried
    assert any("failed" in s.lower() for s in sent)


def test_after_item_exception_becomes_failed_outcome(env):
    kw, _ = env
    def after(item, answer): raise ValueError("post failed")
    out = loopkit.run_plugin(Demo([WorkItem("a", "A")], ['{"verdict": "x"}'], after=after), "run_1", **kw)
    assert out[0].status == "failed" and "post failed" in out[0].summary


def test_discover_failure_notifies_and_raises(env):
    kw, sent = env
    class Broken(Demo):
        def discover(self, ctx): raise RuntimeError("api down")
    with pytest.raises(RuntimeError):
        loopkit.run_plugin(Broken([], []), "run_1", **kw)
    assert "RuntimeError" in sent[0] and "api down" not in sent[0]


def test_second_run_skips_when_locked(env, tmp_path):
    kw, _ = env
    lock = tmp_path / "lock"
    with loopkit.exclusive_run_lock(lock) as got:
        assert got
        p = Demo([WorkItem("a", "A")], ['{"verdict": "ok"}'])
        assert loopkit.run_plugin(p, "run_1", lock_path=lock, **kw) == [] and p.prompts == []


def test_writes_history_and_last_run(env, tmp_path):
    kw, _ = env
    loopkit.run_plugin(Demo([WorkItem("a", "A", url="https://x/a")], ['{"verdict": "ok"}']), "run_1", **kw)
    last = json.loads((tmp_path / "outputs" / "loops" / "demo-loop" / "last-run.json").read_text())
    assert last["counts"] == {"done": 1, "skipped": 0, "failed": 0}
    assert "https://x/a" in next((tmp_path / "history").glob("*.md")).read_text()


def test_outcome_label_names_the_item_in_reports(env, tmp_path):
    kw, _ = env
    p = Demo([WorkItem("cal:1", "A", url="https://x/a")], ['{"verdict": "ok"}'])
    orig = p.after_item
    p.after_item = lambda item, answer, ctx: (lambda o: (o.data.update(label="Team sync"), o)[1])(orig(item, answer, ctx))
    loopkit.run_plugin(p, "run_1", **kw)
    last = json.loads((tmp_path / "outputs" / "loops" / "demo-loop" / "last-run.json").read_text())
    assert last["outcomes"][0]["label"] == "Team sync"
    assert "Team sync" in next((tmp_path / "history").glob("*.md")).read_text()


def test_max_items_per_run(env):
    kw, _ = env
    p = Demo([WorkItem(str(i), "") for i in range(5)], ['{"verdict": "ok"}'] * 5)
    p.max_items_per_run = 2
    assert len(loopkit.run_plugin(p, "run_1", **kw)) == 2


def test_parse_answer_strips_fences():
    assert loopkit.parse_answer('```json\n{"verdict": 1}\n```', ("verdict",)) == {"verdict": 1}


def test_parse_answer_free_text():
    assert loopkit.parse_answer("hello", ()) == "hello"


def test_crash_in_item_two_keeps_item_one_seen_and_continues(env, monkeypatch):
    kw, _ = env
    real = loopkit.write_result
    calls = []
    def flaky(result, results_dir=None, events_dir=None):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(result, results_dir, events_dir=events_dir)
    monkeypatch.setattr(loopkit, "write_result", flaky)
    p = Demo([WorkItem("a", "A"), WorkItem("b", "B"), WorkItem("c", "C")], ['{"verdict": "ok"}'] * 3)
    out = loopkit.run_plugin(p, "run_1", **kw)
    assert [o.status for o in out] == ["done", "failed", "done"]
    assert "OSError" in out[1].summary
    monkeypatch.setattr(loopkit, "write_result", real)
    p2 = Demo([WorkItem("a", "A"), WorkItem("b", "B"), WorkItem("c", "C")], ['{"verdict": "ok"}'])
    assert [o.item_key for o in loopkit.run_plugin(p2, "run_2", **kw)] == ["b"]


def test_systemexit_in_item_two_persists_item_one_and_propagates(env):
    kw, _ = env
    def after(item, answer):
        if item.key == "b":
            raise SystemExit(143)
        return Outcome(item.key, "done", "ok")
    p = Demo([WorkItem("a", "A"), WorkItem("b", "B")], ['{"verdict": "ok"}'] * 2, after=after)
    with pytest.raises(SystemExit):
        loopkit.run_plugin(p, "run_1", **kw)
    p2 = Demo([WorkItem("a", "A"), WorkItem("b", "B")], ['{"verdict": "ok"}'])
    assert [o.item_key for o in loopkit.run_plugin(p2, "run_2", **kw)] == ["b"]


def test_model_crash_summary_has_real_cause(env):
    kw, _ = env
    p = Demo([WorkItem("a", "A")], [RuntimeError("boom"), RuntimeError("boom")])
    out = loopkit.run_plugin(p, "run_1", **kw)
    assert "boom" in out[0].summary


def test_definition_verifier_failure_gets_generic_feedback(env, tmp_path):
    kw, _ = env
    y = LOOP_YAML.replace("retry:", 'verifiers:\n  - {name: nope, type: command, command: "false"}\nretry:')
    (tmp_path / "loops" / "demo" / "loop.yaml").write_text(y)
    p = Demo([WorkItem("a", "A")], ['{"verdict": "ok"}'] * 2)
    loopkit.run_plugin(p, "run_1", **kw)
    assert "failed verification" in p.prompts[1] and "output contract" not in p.prompts[1]


def test_chat_text_neutralises_slack_control_sequences():
    for evil in ("x|y> <!channel>", "<https://evil|click>", "<@U123> hi"):
        out = loopkit.chat_text(evil)
        assert "<" not in out and ">" not in out
    assert loopkit.chat_text("a\x00b\n\n  c\t") == "a b c"
    assert loopkit.chat_text("x" * 400, 10) == "x" * 9 + "\u2026"
    assert loopkit.chat_text(None) == "None"


def test_chat_url_rejects_unsafe():
    assert loopkit.chat_url("https://gl/x?a=1") == "https://gl/x?a=1"
    for bad in ("javascript:alert(1)", "https://a b", "https://a|b", "https://a>b", "https://a<b", 'https://a"b', None, 5, ["https://a"], ""):
        assert loopkit.chat_url(bad) == ""


def test_chat_link_plain_text():
    assert loopkit.chat_link("Review", "https://gl/1") == "Review (https://gl/1)"
    assert loopkit.chat_link("<!channel>", "https://evil|x") == "\u2039!channel\u203a"


def _failing_notifier(sent):
    def n(loop, text):
        sent.append(text)
        return [("slack-default", False, "HTTP 500"), ("mail", False, "Boom")]
    return n


def test_notify_results_recorded_status_only(env, tmp_path):
    kw, sent = env
    kw["notifier"] = lambda loop, text: [("slack-default", True, "sent"), ("mail", False, "secret-url")]
    loopkit.run_plugin(Demo([WorkItem("a", "A")], ['{"verdict": "ok"}']), "run_1", **kw)
    raw = (tmp_path / "outputs" / "loops" / "demo-loop" / "last-run.json").read_text()
    last = json.loads(raw)
    assert last["notified"] is True
    assert last["notify_targets"] == [{"id": "slack-default", "ok": True}, {"id": "mail", "ok": False}]
    assert "secret-url" not in raw
    log = (tmp_path / "logs" / "loop-engineering.log").read_text()
    assert "mail" in log and "secret-url" not in log


def test_all_notify_targets_failing_sets_report_and_exit_code(env, tmp_path, monkeypatch):
    kw, sent = env
    kw["notifier"] = _failing_notifier(sent)
    report = {}
    out = loopkit.run_plugin(Demo([WorkItem("a", "A")], ['{"verdict": "ok"}']), "run_1", report=report, **kw)
    assert out[0].status == "done" and report["notify_failed"] is True
    last = json.loads((tmp_path / "outputs" / "loops" / "demo-loop" / "last-run.json").read_text())
    assert last["notified"] is False
    # items stay seen
    assert loopkit.run_plugin(Demo([WorkItem("a", "A")], []), "run_2", **kw) == []
    # main() turns it into exit code 1
    monkeypatch.setattr(loopkit, "run_plugin", lambda plugin, run_id, force=False, report=None: (
        report.update(notify_failed=True), out)[1])
    assert loopkit.main(Demo([], []), ["run_3"]) == 1


def test_partial_notify_failure_keeps_exit_zero(env, monkeypatch):
    kw, _ = env
    kw["notifier"] = lambda loop, text: [("a", True, "sent"), ("b", False, "x")]
    report = {}
    loopkit.run_plugin(Demo([WorkItem("a", "A")], ['{"verdict": "ok"}']), "run_1", report=report, **kw)
    assert not report.get("notify_failed")


def test_no_history_file_when_no_outcomes_but_last_run_written(env, tmp_path):
    kw, _ = env
    loopkit.run_plugin(Demo([], []), "run_1", **kw)
    assert not list((tmp_path / "history").glob("*.md")) if (tmp_path / "history").exists() else True
    assert (tmp_path / "outputs" / "loops" / "demo-loop" / "last-run.json").exists()


def test_loop_result_goes_to_callers_events_dir(env):
    import json as _json
    kw, _ = env
    p = Demo([WorkItem("a", "A")], ['{"verdict": "ok"}'])
    loopkit.run_plugin(p, "run_1", **kw)
    rows = [_json.loads(line) for f in (kw["events_dir"]).glob("*.jsonl") for line in f.read_text().splitlines()]
    assert [r["event_type"] for r in rows].count("loop.result") == 1


class BudgetDemo(Demo):
    def __init__(self, items, answers, costs):
        super().__init__(items, answers)
        self.costs, self.caps = list(costs), []

    def call_model(self, prompt, ctx):
        self.caps.append(ctx.call_budget_usd)
        a = self.answers.pop(0)
        return {"text": a, "cost_usd": self.costs.pop(0)}


def test_a_retry_gets_only_the_remaining_budget(env):
    kw, _ = env
    p = BudgetDemo([WorkItem("a", "A")], ["not json", '{"verdict": "ok"}'], [0.3, 0.1])
    assert loopkit.run_plugin(p, "run_1", **kw)[0].status == "done"
    assert p.caps == [1, pytest.approx(0.7)]


def test_an_unknown_cost_is_booked_at_the_cap(env):
    kw, _ = env
    p = BudgetDemo([WorkItem("a", "A")], ["not json", '{"verdict": "ok"}'], [None, 0.01])
    loopkit.run_plugin(p, "run_1", **kw)
    assert p.caps == [1, 0.05]


def test_each_item_starts_with_the_full_budget(env):
    kw, _ = env
    p = BudgetDemo([WorkItem("a", "A"), WorkItem("b", "B")], ['{"verdict": "ok"}'] * 2, [0.9, 0.9])
    loopkit.run_plugin(p, "run_1", **kw)
    assert p.caps == [1, 1]


def test_base_call_model_passes_the_call_budget(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(loopkit.sealed, "sealed_call",
                        lambda prompt, timeout, log=None, max_budget_usd=None: seen.update(cap=max_budget_usd)
                        or {"text": "x", "cost_usd": 0})
    import dataclasses
    from loop_definition import LoopDefinition
    kw, _ = env
    definition = LoopDefinition.from_yaml(kw["repo_root"] / "loops" / "demo" / "loop.yaml")
    ctx = loopkit.LoopContext(loop_name="demo-loop", run_id="r", now=kw["now"], definition=definition,
                              settings={}, history_dir=kw["history_dir"], log=lambda m: None)
    loopkit.LoopPlugin().call_model("p", dataclasses.replace(ctx, call_budget_usd=0.4))
    assert seen["cap"] == 0.4
    loopkit.LoopPlugin().call_model("p", ctx)
    assert seen["cap"] == 1


def test_a_report_write_failure_never_masks_the_original_exception(env, monkeypatch):
    kw, _ = env
    def after(item, answer):
        raise SystemExit(143)
    monkeypatch.setattr(loopkit, "_write_reports", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    p = Demo([WorkItem("a", "A")], ['{"verdict": "ok"}'], after=after)
    with pytest.raises(SystemExit):
        loopkit.run_plugin(p, "run_1", **kw)


def test_a_crashed_item_summary_is_length_capped(env, monkeypatch):
    kw, _ = env
    def after(item, answer):
        raise RuntimeError("x" * 5000)
    p = Demo([WorkItem("a", "A")], ['{"verdict": "ok"}'], after=after)
    out = loopkit.run_plugin(p, "run_1", **kw)
    assert out[0].status == "failed" and out[0].summary.startswith("RuntimeError: ")
    assert len(out[0].summary) <= 300


def test_slack_link_sanitizes_label_and_rejects_bad_urls():
    assert loopkit.slack_link("https://x.test/a?b=1&c=2", "Fix <!channel> | now") == \
        "<https://x.test/a?b=1&c=2|Fix \u2039!channel\u203a now>"
    assert loopkit.slack_link("javascript:alert(1)", "Fix") == "Fix"
    assert loopkit.slack_link("https://x.test/a>b", "Fix") == "Fix"
