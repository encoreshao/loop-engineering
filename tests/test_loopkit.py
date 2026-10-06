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
    assert "api down" in sent[0]


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


def test_max_items_per_run(env):
    kw, _ = env
    p = Demo([WorkItem(str(i), "") for i in range(5)], ['{"verdict": "ok"}'] * 5)
    p.max_items_per_run = 2
    assert len(loopkit.run_plugin(p, "run_1", **kw)) == 2


def test_parse_answer_strips_fences():
    assert loopkit.parse_answer('```json\n{"verdict": 1}\n```', ("verdict",)) == {"verdict": 1}


def test_parse_answer_free_text():
    assert loopkit.parse_answer("hello", ()) == "hello"
