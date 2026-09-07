import json
import os
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import gitlab_loop_runner as glr

import pytest

from conftest import SANITIZED_PATH

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFINITION_PATH = REPO_ROOT / "loops" / "gitlab-issue" / "loop.yaml"


@pytest.fixture(autouse=True)
def _no_real_ai_cli(sanitized_path):
    """Hard safety net for this whole module: applies tests/conftest.py's
    shared `sanitized_path` fixture (a PATH with bash/env/python3 but not
    the real `claude`/`codex`) to every test here. See that fixture's
    docstring for the incident this prevents - and copy this two-line
    autouse wrapper into any future test module that imports
    `gitlab_loop_runner`. `sanitized_path` is deliberately not autouse
    project-wide: it must not silently reshape unrelated test modules'
    environments."""


@pytest.fixture(autouse=True)
def slack_calls(monkeypatch):
    """Safe default for `slack_notify.post_message`, which
    `bin/gitlab_loop_runner.py` now calls in-process: unstubbed it reads the
    real ~/.slack/config.json and POSTs over the network, so a test that
    forgets to fake it would reach the real Slack - the same shape of
    accident as the real-`claude` invocation above.

    Returns the list of message texts the stub recorded, so a test can just
    request `slack_calls` to inspect them. Tests that want richer behavior
    keep doing their own `monkeypatch.setattr(glr.slack_notify,
    "post_message", ...)`, which simply overrides this default."""
    calls = []
    monkeypatch.setattr(glr.slack_notify, "post_message", lambda text, **kwargs: calls.append(text))
    return calls


def test_the_modules_safety_net_stubs_both_the_ai_cli_and_slack(slack_calls):
    """Guards the safety net itself. `_notify_slack_best_effort` calls
    slack_notify.post_message in-process, which reads the real
    ~/.slack/config.json and POSTs over the network - the same shape of
    accident as the real-`claude` invocation that hung this suite once. A
    test that forgets to fake Slack must hit the recording stub instead."""
    assert os.environ["PATH"] == SANITIZED_PATH
    assert glr._notify_slack_best_effort("a message nobody should receive") is True
    assert slack_calls == ["a message nobody should receive"]


def _write_fake_cli(bin_dir, name, output_json=None, raw_output=None, exit_code=0):
    """A fake `claude`/`codex` executable: echoes canned output, exits `exit_code`."""
    script_path = bin_dir / name
    if output_json is not None:
        body = f"print({json.dumps(json.dumps(output_json))})"
    else:
        body = f"print({json.dumps(raw_output or '')}, end='')"
    script_path.write_text(f"#!/usr/bin/env python3\nimport sys\n{body}\nsys.exit({exit_code})\n")
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)


def test_invoke_issue_agent_extracts_cost_for_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_cli(bin_dir, "claude", output_json={
        "result": "Fixed the issue.",
        "total_cost_usd": 0.42,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "modelUsage": {"claude-sonnet": {}},
    })

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(
        glr, "build_prompt",
        lambda alias=None, issue_iid=None, repo_root=None: "a prompt",
    )
    monkeypatch.setattr(glr.ai_cli_config, "get_selected_cli", lambda: "claude")
    monkeypatch.setattr(glr.loop_config, "get_worktree_root", lambda: str(tmp_path / "worktrees"))

    unified_log = tmp_path / "unified.log"
    result = glr.invoke_issue_agent("harbor", 42, repo_root=REPO_ROOT, unified_log_path=unified_log)

    assert result == {"changed": True, "cost_usd": 0.42}
    assert "Fixed the issue." in unified_log.read_text()


def test_invoke_issue_agent_raises_on_nonzero_exit(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_cli(bin_dir, "claude", raw_output="boom", exit_code=1)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(
        glr, "build_prompt",
        lambda alias=None, issue_iid=None, repo_root=None: "a prompt",
    )
    monkeypatch.setattr(glr.ai_cli_config, "get_selected_cli", lambda: "claude")
    monkeypatch.setattr(glr.loop_config, "get_worktree_root", lambda: str(tmp_path))

    import subprocess
    try:
        glr.invoke_issue_agent("harbor", 42, repo_root=REPO_ROOT, unified_log_path=tmp_path / "unified.log")
        assert False, "expected CalledProcessError"
    except subprocess.CalledProcessError:
        pass


def test_run_all_issues_writes_one_result_per_issue(tmp_path, monkeypatch):
    calls = []

    def fake_invoke(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None):
        calls.append((alias, issue_iid))
        return {"changed": True, "cost_usd": 0.1}

    # The batch path invokes `invoke_batch_issue_agent` (the no-End-of-run
    # prompt) per issue and `invoke_batch_end_of_run_agent` once. BOTH must
    # be faked - anything left real reaches the actual `claude`/`codex`
    # binary and the real ~/.loop-engineering config.
    monkeypatch.setattr(glr, "invoke_batch_issue_agent", fake_invoke)
    monkeypatch.setattr(
        glr, "invoke_batch_end_of_run_agent",
        lambda repo_root=None, timeout_seconds=900, unified_log_path=None: {"changed": True, "cost_usd": 0.0},
    )

    def fake_list_assigned_issues(aliases, username):
        return {"harbor": [{"iid": 1}], "orchard": [{"iid": 9}]}

    monkeypatch.setattr(glr, "list_assigned_issues", fake_list_assigned_issues)

    results_dir = tmp_path / "loop-runs"
    results = glr.run_all_issues(
        "run_20260907_100000", results_dir=results_dir,
        definition_path=REPO_ROOT / "loops" / "gitlab-issue" / "loop.yaml",
        aliases=["harbor", "orchard"], username="encore",
        events_dir=tmp_path / "events",
    )

    assert calls == [("harbor", 1), ("orchard", 9)]
    assert [r.final_state.value for r in results] == ["completed", "completed"]
    assert sorted(p.name for p in results_dir.iterdir()) == [
        "run_20260907_100000_harbor_1",
        "run_20260907_100000_orchard_9",
    ]


def test_run_all_issues_continues_after_one_issue_fails(tmp_path, monkeypatch):
    def fake_invoke(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None):
        if issue_iid == 1:
            raise RuntimeError("agent crashed")
        return {"changed": True, "cost_usd": 0.1}

    monkeypatch.setattr(glr, "invoke_batch_issue_agent", fake_invoke)
    monkeypatch.setattr(
        glr, "invoke_batch_end_of_run_agent",
        lambda repo_root=None, timeout_seconds=900, unified_log_path=None: {"changed": True, "cost_usd": 0.0},
    )
    monkeypatch.setattr(
        glr, "list_assigned_issues",
        lambda aliases, username: {"harbor": [{"iid": 1}, {"iid": 2}]},
    )

    results_dir = tmp_path / "loop-runs"
    results = glr.run_all_issues(
        "run_20260907_110000", results_dir=results_dir,
        definition_path=REPO_ROOT / "loops" / "gitlab-issue" / "loop.yaml",
        aliases=["harbor"], username="encore",
        events_dir=tmp_path / "events",
    )

    assert [r.final_state.value for r in results] == ["failed", "completed"]


def test_run_single_issue_writes_its_own_result(tmp_path, monkeypatch):
    monkeypatch.setattr(
        glr, "invoke_issue_agent",
        lambda alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None: {
            "changed": True, "cost_usd": 0.2,
        },
    )

    results_dir = tmp_path / "loop-runs"
    result = glr.run_single_issue(
        "run_20260907_120000", "harbor", 7, results_dir=results_dir,
        definition_path=REPO_ROOT / "loops" / "gitlab-issue" / "loop.yaml",
    )

    assert result.final_state.value == "completed"
    assert (results_dir / "run_20260907_120000_harbor_7" / "result.json").exists()


# --- The batch prompt modes and the unconditional batch wrap-up ---------------
#
# The scheduled batch used to be one agent session that did discovery, every
# issue, and "End of run" (daily-review.md/PROGRESS.md/the one Slack digest)
# once. Now discovery is Python's job and each issue gets its own session, so
# "End of run" has to be its own separate, UNCONDITIONAL call - otherwise a
# morning with zero assigned issues produces no digest at all, breaking
# LOOPX_INSTRUCTIONS.md's "a quiet morning is still reported" guarantee.


def _must_not_be_called(*args, **kwargs):
    raise AssertionError("this invoker must not be called on this path")


def _today():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read_events(events_dir):
    path = Path(events_dir) / f"{_today()}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _fake_per_issue_invoker(calls, failing_iids=()):
    def fake_invoke(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None):
        calls.append(("issue", alias, issue_iid, timeout_seconds))
        if issue_iid in failing_iids:
            raise RuntimeError(f"agent crashed on {issue_iid}")
        return {"changed": True, "cost_usd": 0.25}

    return fake_invoke


def _fake_wrapup_invoker(calls, exc=None):
    def fake_wrapup(repo_root=None, timeout_seconds=900, unified_log_path=None):
        calls.append(("wrapup",))
        if exc is not None:
            raise exc
        return {"changed": True, "cost_usd": 0.05}

    return fake_wrapup


def test_run_all_issues_uses_the_batch_issue_invoker_then_wraps_up_once(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(glr, "invoke_batch_issue_agent", _fake_per_issue_invoker(calls))
    monkeypatch.setattr(glr, "invoke_batch_end_of_run_agent", _fake_wrapup_invoker(calls))
    # The dashboard's own invoker must not be used by the batch path at all.
    monkeypatch.setattr(glr, "invoke_issue_agent", _must_not_be_called)
    monkeypatch.setattr(
        glr, "list_assigned_issues",
        lambda aliases, username: {"harbor": [{"iid": 1}, {"iid": 2}], "orchard": [{"iid": 9}]},
    )

    glr.run_all_issues(
        "run_20260907_130000", results_dir=tmp_path / "loop-runs",
        definition_path=DEFINITION_PATH, aliases=["harbor", "orchard"],
        username="encore", events_dir=tmp_path / "events",
    )

    assert calls == [
        ("issue", "harbor", 1, 1800),
        ("issue", "harbor", 2, 1800),
        ("issue", "orchard", 9, 1800),
        ("wrapup",),
    ]


def test_run_all_issues_wraps_up_even_when_nothing_is_assigned(tmp_path, monkeypatch):
    """The empty-morning case: no issues at all, but the digest/daily-review
    must still be produced exactly once."""
    calls = []
    monkeypatch.setattr(glr, "invoke_batch_issue_agent", _fake_per_issue_invoker(calls))
    monkeypatch.setattr(glr, "invoke_batch_end_of_run_agent", _fake_wrapup_invoker(calls))
    monkeypatch.setattr(glr, "list_assigned_issues", lambda aliases, username: {"harbor": [], "orchard": []})

    results = glr.run_all_issues(
        "run_20260907_140000", results_dir=tmp_path / "loop-runs",
        definition_path=DEFINITION_PATH, aliases=["harbor", "orchard"],
        username="encore", events_dir=tmp_path / "events",
    )

    assert results == []
    assert calls == [("wrapup",)]


def test_run_all_issues_wraps_up_after_an_issue_fails(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(glr, "invoke_batch_issue_agent", _fake_per_issue_invoker(calls, failing_iids=(1,)))
    monkeypatch.setattr(glr, "invoke_batch_end_of_run_agent", _fake_wrapup_invoker(calls))
    monkeypatch.setattr(
        glr, "list_assigned_issues",
        lambda aliases, username: {"harbor": [{"iid": 1}, {"iid": 2}]},
    )

    results = glr.run_all_issues(
        "run_20260907_141500", results_dir=tmp_path / "loop-runs",
        definition_path=DEFINITION_PATH, aliases=["harbor"], username="encore",
        events_dir=tmp_path / "events",
    )

    assert [r.final_state.value for r in results] == ["failed", "completed"]
    assert calls[-1] == ("wrapup",)


def test_run_all_issues_alerts_slack_when_the_wrap_up_itself_fails(tmp_path, monkeypatch):
    """The wrap-up failing means the daily digest never got sent - at least as
    important to surface as a single issue failing."""
    sent = []
    monkeypatch.setattr(glr, "invoke_batch_issue_agent", _fake_per_issue_invoker([]))
    monkeypatch.setattr(
        glr, "invoke_batch_end_of_run_agent",
        _fake_wrapup_invoker([], exc=RuntimeError("wrap-up blew up")),
    )
    monkeypatch.setattr(glr, "list_assigned_issues", lambda aliases, username: {})
    monkeypatch.setattr(glr.slack_notify, "post_message", lambda text, **kwargs: sent.append(text))

    unified_log = tmp_path / "unified.log"
    events_dir = tmp_path / "events"
    glr.run_all_issues(
        "run_20260907_142500", results_dir=tmp_path / "loop-runs",
        definition_path=DEFINITION_PATH, aliases=[], username="encore",
        events_dir=events_dir, unified_log_path=unified_log,
    )

    assert len(sent) == 1
    assert "end-of-run" in sent[0].lower() or "digest" in sent[0].lower()
    assert "wrap-up blew up" in sent[0]
    assert "wrap-up blew up" in unified_log.read_text()
    assert any(e["event_type"] == "run.wrapup_failed" for e in _read_events(events_dir))


def test_run_single_issue_still_uses_the_dashboard_invoker(tmp_path, monkeypatch):
    """The dashboard's on-demand path is explicitly out of scope: it keeps
    using the 2-arg prompt, which does its own full End of run."""
    calls = []
    monkeypatch.setattr(glr, "invoke_issue_agent", _fake_per_issue_invoker(calls))
    monkeypatch.setattr(glr, "invoke_batch_issue_agent", _must_not_be_called)

    result = glr.run_single_issue(
        "run_20260907_150000", "harbor", 7, results_dir=tmp_path / "loop-runs",
        definition_path=DEFINITION_PATH, events_dir=tmp_path / "events",
    )

    assert result.final_state.value == "completed"
    assert calls == [("issue", "harbor", 7, 1800)]


def test_run_one_issue_derives_its_timeout_from_max_runtime_minutes(tmp_path):
    from loop_definition import LoopDefinition

    definition = LoopDefinition.from_yaml(DEFINITION_PATH)
    captured = []

    def fake_invoke(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None):
        captured.append(timeout_seconds)
        return {"changed": True, "cost_usd": 0.0}

    glr._run_one_issue(
        "run_x", "harbor", 3, definition, tmp_path / "loop-runs", REPO_ROOT,
        agent_invoker=fake_invoke, events_dir=tmp_path / "events",
    )

    assert captured == [definition.stop_conditions.max_runtime_minutes * 60]


# --- Batch prompt builders (real subprocess against build_run_prompt.sh) ------


def test_build_batch_issue_prompt_forbids_end_of_run():
    prompt = glr.build_batch_issue_prompt("harbor", 482, repo_root=REPO_ROOT)
    assert "project alias 'harbor'" in prompt
    assert "issue IID 482" in prompt
    assert "--batch-end-of-run" in prompt
    assert "on-demand single-issue run" not in prompt


def test_build_batch_end_of_run_prompt_points_at_the_event_log():
    prompt = glr.build_batch_end_of_run_prompt(repo_root=REPO_ROOT)
    assert "End of run" in prompt
    assert "outputs/events/" in prompt
    assert "$LOOP_RUN_ID" in prompt


# --- main(): run.completed with aggregated cost, argv validation, alerting ----


def test_main_emits_run_completed_with_aggregated_cost(tmp_path, monkeypatch):
    """bin/cost.py's cost report reads run.completed's data.cost_usd. Nothing
    emitted that any more once run-loop.sh stopped extracting per-batch cost,
    so main() now owns this event and carries the summed per-issue cost."""
    monkeypatch.setattr(glr, "invoke_batch_issue_agent", _fake_per_issue_invoker([]))
    monkeypatch.setattr(glr, "invoke_batch_end_of_run_agent", _fake_wrapup_invoker([]))
    monkeypatch.setattr(
        glr, "list_assigned_issues",
        lambda aliases, username: {"harbor": [{"iid": 1}, {"iid": 2}]},
    )

    events_dir = tmp_path / "events"
    exit_code = glr.main(
        argv=["run_20260907_160000"], results_dir=tmp_path / "loop-runs",
        definition_path=DEFINITION_PATH, events_dir=events_dir,
        aliases=["harbor"], username="encore",
    )

    assert exit_code == 0
    completed = [e for e in _read_events(events_dir) if e["event_type"] == "run.completed"]
    assert len(completed) == 1
    # Two issues at 0.25 each (the fake invoker's per-call cost).
    assert completed[0]["data"]["cost_usd"] == 0.5
    assert completed[0]["data"]["issues"] == 2
    assert completed[0]["run_id"] == "run_20260907_160000"


def _fake_unpriced_invoker(calls):
    """Every issue comes back with `cost_usd: None` - the Codex path (which
    reports no cost at all), or a Claude run whose cost extraction failed."""
    def fake_invoke(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None):
        calls.append(("issue", alias, issue_iid, timeout_seconds))
        return {"changed": True, "cost_usd": None}

    return fake_invoke


def test_main_omits_cost_usd_entirely_when_nothing_was_actually_priced(tmp_path, monkeypatch):
    """bin/cost.py's `_priced_run_ids` treats a run.completed whose data
    carries a non-None cost_usd as "this run was priced" and folds its
    issues into cost_per_issue/cost_per_resolution's denominators. Emitting
    0.0 for a Codex-only run would therefore dilute those metrics with
    issues that were never priced - so the key must be absent, exactly as
    it was before this event moved into Python."""
    monkeypatch.setattr(glr, "invoke_batch_issue_agent", _fake_unpriced_invoker([]))
    monkeypatch.setattr(glr, "invoke_batch_end_of_run_agent", _fake_wrapup_invoker([]))
    monkeypatch.setattr(
        glr, "list_assigned_issues",
        lambda aliases, username: {"harbor": [{"iid": 1}, {"iid": 2}]},
    )

    events_dir = tmp_path / "events"
    glr.main(
        argv=["run_20260907_180000"], results_dir=tmp_path / "loop-runs",
        definition_path=DEFINITION_PATH, events_dir=events_dir,
        aliases=["harbor"], username="encore",
    )

    completed = [e for e in _read_events(events_dir) if e["event_type"] == "run.completed"]
    assert len(completed) == 1
    assert "cost_usd" not in completed[0]["data"]
    assert completed[0]["data"]["issues"] == 2


def test_main_omits_cost_usd_when_nothing_was_assigned(tmp_path, monkeypatch):
    """A zero-issue morning prices nothing either."""
    monkeypatch.setattr(glr, "invoke_batch_issue_agent", _must_not_be_called)
    monkeypatch.setattr(glr, "invoke_batch_end_of_run_agent", _fake_wrapup_invoker([]))
    monkeypatch.setattr(glr, "list_assigned_issues", lambda aliases, username: {})

    events_dir = tmp_path / "events"
    glr.main(
        argv=["run_20260907_181500"], results_dir=tmp_path / "loop-runs",
        definition_path=DEFINITION_PATH, events_dir=events_dir,
        aliases=["harbor"], username="encore",
    )

    completed = [e for e in _read_events(events_dir) if e["event_type"] == "run.completed"]
    assert len(completed) == 1
    assert "cost_usd" not in completed[0]["data"]
    assert completed[0]["data"]["issues"] == 0


def test_an_unpriced_runs_issues_stay_out_of_cost_pers_denominators(tmp_path, monkeypatch):
    """End-to-end against bin/cost.py itself, not just the event's shape:
    the run.completed main() actually emits for an unpriced run must leave
    that run's issues out of cost_per_issue/cost_per_resolution."""
    monkeypatch.setattr(glr, "invoke_batch_issue_agent", _fake_unpriced_invoker([]))
    monkeypatch.setattr(glr, "invoke_batch_end_of_run_agent", _fake_wrapup_invoker([]))
    monkeypatch.setattr(
        glr, "list_assigned_issues",
        lambda aliases, username: {"harbor": [{"iid": 1}, {"iid": 2}]},
    )

    events_dir = tmp_path / "events"
    unpriced_run = "run_20260907_183000"
    glr.main(
        argv=[unpriced_run], results_dir=tmp_path / "loop-runs",
        definition_path=DEFINITION_PATH, events_dir=events_dir,
        aliases=["harbor"], username="encore",
    )

    # The issue.* events a real --batch-issue session emits for itself
    # (LOOPX_INSTRUCTIONS.md Step 2), which main() does not emit.
    for iid in (1, 2):
        for event_type in ("issue.started", "issue.completed"):
            glr.events_module.emit(
                event_type, unpriced_run, issue_run_id=f"{unpriced_run}_harbor_{iid}",
                project="harbor", issue_iid=iid, events_dir=events_dir,
            )
    # A second, genuinely priced run: one issue, $1.00.
    priced_run = "run_20260907_190000"
    glr.events_module.emit(
        "run.completed", priced_run, data={"cost_usd": 1.0, "issues": 1}, events_dir=events_dir,
    )
    for event_type in ("issue.started", "issue.completed"):
        glr.events_module.emit(
            event_type, priced_run, issue_run_id=f"{priced_run}_harbor_7",
            project="harbor", issue_iid=7, events_dir=events_dir,
        )

    metrics = glr.cost_module.compute_cost_metrics(_read_events(events_dir))

    assert metrics["total_cost_usd"] == 1.0
    # Only the priced run's single issue counts - not 1.0 / 3.
    assert metrics["cost_per_issue"] == 1.0
    assert metrics["cost_per_resolution"] == 1.0


def test_main_alerts_slack_when_an_issue_does_not_complete(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(glr, "invoke_batch_issue_agent", _fake_per_issue_invoker([], failing_iids=(2,)))
    monkeypatch.setattr(glr, "invoke_batch_end_of_run_agent", _fake_wrapup_invoker([]))
    monkeypatch.setattr(
        glr, "list_assigned_issues",
        lambda aliases, username: {"harbor": [{"iid": 1}, {"iid": 2}]},
    )
    monkeypatch.setattr(glr.slack_notify, "post_message", lambda text, **kwargs: sent.append(text))

    glr.main(
        argv=["run_20260907_170000"], results_dir=tmp_path / "loop-runs",
        definition_path=DEFINITION_PATH, events_dir=tmp_path / "events",
        aliases=["harbor"], username="encore",
    )

    assert len(sent) == 1
    assert "run_20260907_170000_harbor_2" in sent[0]
    assert "failed" in sent[0]
    # The issue that succeeded must not be named as a failure.
    assert "run_20260907_170000_harbor_1" not in sent[0]


def test_main_does_not_alert_slack_when_everything_completes(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(glr, "invoke_batch_issue_agent", _fake_per_issue_invoker([]))
    monkeypatch.setattr(glr, "invoke_batch_end_of_run_agent", _fake_wrapup_invoker([]))
    monkeypatch.setattr(glr, "list_assigned_issues", lambda aliases, username: {"harbor": [{"iid": 1}]})
    monkeypatch.setattr(glr.slack_notify, "post_message", lambda text, **kwargs: sent.append(text))

    glr.main(
        argv=["run_20260907_171500"], results_dir=tmp_path / "loop-runs",
        definition_path=DEFINITION_PATH, events_dir=tmp_path / "events",
        aliases=["harbor"], username="encore",
    )

    assert sent == []


def _stub_result():
    from loop_result import LoopResult
    from loop_state import LoopState
    return LoopResult(
        loop_id="loop_stub", run_id="run_b_harbor_42", definition_name="gitlab-issue-loop",
        final_state=LoopState.COMPLETED, iterations=[], stop_reason="completed",
    )


def test_main_branches_on_argv_length(monkeypatch):
    seen = []
    monkeypatch.setattr(glr, "run_all_issues", lambda run_id, **kwargs: seen.append(("all", run_id)) or [])
    monkeypatch.setattr(
        glr, "run_single_issue",
        lambda run_id, alias, issue_iid, **kwargs: (
            seen.append(("single", run_id, alias, issue_iid)) or _stub_result()
        ),
    )
    monkeypatch.setattr(glr, "_emit_run_completed", lambda *a, **k: None)

    assert glr.main(argv=["run_a"]) == 0
    assert glr.main(argv=["run_b", "harbor", "42"]) == 0
    assert seen == [("all", "run_a"), ("single", "run_b", "harbor", "42")]


def test_main_rejects_an_argv_length_it_does_not_understand(monkeypatch, capsys):
    """A typo like `run-loop.sh harbor` (missing the IID) used to fall through
    to run_all_issues and process every assigned issue across every project."""
    called = []
    monkeypatch.setattr(glr, "run_all_issues", lambda *a, **k: called.append("all") or [])
    monkeypatch.setattr(glr, "run_single_issue", lambda *a, **k: called.append("single"))

    for argv in ([], ["run_a", "harbor"], ["run_a", "harbor", "42", "extra"]):
        assert glr.main(argv=argv) == 2
    assert called == []
    assert "Usage" in capsys.readouterr().err


# --- Failure visibility inside the one subprocess boundary --------------------


def test_invoke_cli_with_prompt_logs_stderr_on_the_success_path(tmp_path, monkeypatch):
    """run-loop.sh let the CLI's stderr flow to the per-run dated log
    regardless of exit code; that diagnostic capability must not be lost."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(
        "#!/usr/bin/env python3\nimport json, sys\n"
        "print('a warning from the CLI', file=sys.stderr)\n"
        "print(json.dumps({'result': 'Done.', 'total_cost_usd': 0.1, 'usage': {}}))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(glr.ai_cli_config, "get_selected_cli", lambda: "claude")
    monkeypatch.setattr(glr.loop_config, "get_worktree_root", lambda: str(tmp_path))

    unified_log = tmp_path / "unified.log"
    result = glr._invoke_cli_with_prompt(
        "a prompt", repo_root=REPO_ROOT, unified_log_path=unified_log,
    )

    assert result == {"changed": True, "cost_usd": 0.1}
    logged = unified_log.read_text()
    assert "Done." in logged
    assert "a warning from the CLI" in logged


def test_invoke_cli_with_prompt_logs_and_emits_on_failure(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        "print('traceback: everything is on fire', file=sys.stderr)\nsys.exit(3)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("LOOP_RUN_ID", "run_20260907_180000")
    monkeypatch.setattr(glr.ai_cli_config, "get_selected_cli", lambda: "claude")
    monkeypatch.setattr(glr.loop_config, "get_worktree_root", lambda: str(tmp_path))

    unified_log = tmp_path / "unified.log"
    events_dir = tmp_path / "events"
    try:
        glr._invoke_cli_with_prompt(
            "a prompt", repo_root=REPO_ROOT, unified_log_path=unified_log,
            alias="harbor", issue_iid=42, events_dir=events_dir,
        )
        assert False, "expected CalledProcessError"
    except subprocess.CalledProcessError:
        pass

    logged = unified_log.read_text()
    assert "traceback: everything is on fire" in logged
    failures = [e for e in _read_events(events_dir) if e["event_type"] == "issue.agent_failed"]
    assert len(failures) == 1
    assert failures[0]["project"] == "harbor"
    assert failures[0]["issue_iid"] == 42
    assert failures[0]["issue_run_id"] == "run_20260907_180000_harbor_42"
    assert "everything is on fire" in failures[0]["data"]["stderr_excerpt"]


# --- Safety-critical tool permission strings ---------------------------------


def test_allowed_tools_scopes_git_push_to_issue_branches_only():
    """These strings ARE the safety boundary (docs/tasks/gitlab-issue-loop.md):
    they moved out of run-loop.sh into Python, so they need the same rigor as
    tests/test_dashboard_server.py's own chat-command tool-list tests."""
    allowed = glr._allowed_tools(REPO_ROOT)

    assert "Bash(git push origin loop/issue-*)" in allowed
    # A bare, unscoped push would let the agent push straight to a target
    # branch - the single most important thing this list prevents.
    assert "Bash(git push*)" not in allowed
    assert "Bash(git push)" not in allowed
    # git is enumerated per-subcommand, never as a blanket `git *`.
    assert "Bash(git *)" not in allowed
    for expected in ("Bash(git status*)", "Bash(git diff*)", "Bash(git add*)", "Bash(git commit*)"):
        assert expected in allowed, f"expected {expected!r} in allowedTools"
    # No merge/checkout/reset/clean on the allow side at all.
    for forbidden in ("git merge", "git checkout", "git reset", "git clean", "git worktree"):
        assert forbidden not in allowed, f"{forbidden!r} must not be allowlisted"
    # bin/ scripts in both relative and absolute form, one pattern per
    # subdirectory (a glob's `*` doesn't cross a `/`).
    for expected in (
        "Bash(python3 bin/*.py*)", "Bash(python3 bin/web/*.py*)", "Bash(bash bin/scripts/*.sh*)",
        f"Bash(python3 {REPO_ROOT}/bin/*.py*)",
        f"Bash(python3 {REPO_ROOT}/bin/web/*.py*)",
        f"Bash(bash {REPO_ROOT}/bin/scripts/*.sh*)",
    ):
        assert expected in allowed, f"expected {expected!r} in allowedTools"
    assert "Read Edit Write" in allowed


def test_disallowed_tools_blocks_merging_force_pushing_and_secrets():
    disallowed = glr._DISALLOWED_TOOLS

    for expected in (
        "Bash(git merge*)",
        "Bash(git push --force*)", "Bash(git push -f*)",
        "Bash(git checkout*)", "Bash(git reset*)", "Bash(git clean*)",
        "Read(**/.env*)", "Read(**/*.key)", "Read(**/id_rsa*)",
    ):
        assert expected in disallowed, f"expected {expected!r} in disallowedTools"


# --- The codex branch --------------------------------------------------------


def test_cli_command_codex_branch_passes_the_sandbox_overrides():
    cmd = glr._cli_command("codex", "a prompt", Path("/loop"), "/worktrees")

    assert cmd[:5] == ["codex", "exec", "--sandbox", "workspace-write", "-c"]
    assert cmd[-1] == "a prompt"
    assert "approval_policy=never" in cmd
    assert "sandbox_workspace_write.network_access=true" in cmd
    # Byte-identical to the JSON string run-loop.sh used to build in bash -
    # no spaces after the comma or colon.
    assert 'sandbox_workspace_write.writable_roots=["/loop","/worktrees"]' in cmd
    # Claude-only flags must not leak into the codex command line.
    for flag in ("--allowedTools", "--disallowedTools", "--add-dir", "--permission-mode"):
        assert flag not in cmd


def test_invoke_batch_issue_agent_reports_no_cost_on_the_codex_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_cli(bin_dir, "codex", raw_output="codex did the thing")

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(glr.ai_cli_config, "get_selected_cli", lambda: "codex")
    monkeypatch.setattr(glr.loop_config, "get_worktree_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        glr, "build_batch_issue_prompt",
        lambda alias, issue_iid, repo_root=None: "a batch-issue prompt",
    )

    unified_log = tmp_path / "unified.log"
    result = glr.invoke_batch_issue_agent(
        "harbor", 42, repo_root=REPO_ROOT, unified_log_path=unified_log,
    )

    assert result == {"changed": True, "cost_usd": None}
    assert "codex did the thing" in unified_log.read_text()
