import json
import os
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import gitlab_loop_runner as glr
import loop_verifiers as lv

import pytest

from conftest import SANITIZED_PATH

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFINITION_PATH = REPO_ROOT / "loops" / "gitlab-issue" / "loop.yaml"


class FakeResult:
    def __init__(self, name, passed, output, evidence=None):
        self.name, self.passed, self.output, self.evidence = name, passed, output, evidence or {}
        self.exit_code, self.duration_ms = (0 if passed else 1), 1


class FakeIteration:
    def __init__(self, verification_results):
        self.verification_results = verification_results


class FakeVerifier(lv.Verifier):
    def __init__(self, passed, observed_passed=None):
        self.passed, self.observed = passed, observed_passed

    def verify(self, context):
        ev = {} if self.observed is None else {"observed_passed": self.observed, "mode": "observe"}
        return lv.VerificationResult("project_commands", self.passed, 0, 1, "out", ev)


class SequenceVerifier(lv.Verifier):
    def __init__(self, seq, output="out"):
        self.seq, self.output = seq, output

    def verify(self, context):
        p = next(self.seq)
        return lv.VerificationResult("project_commands", p, 0 if p else 1, 1, self.output, {})


_DEFINITION_YAML = """
name: test-loop
version: 1
trigger: {{type: schedule, schedule: "0 10 * * 1-5"}}
goal: {{type: issue_resolution}}
actions: [modify_code]
verification: {{required: [project_commands], mode: {mode}}}
verifiers:
  - {{name: project_commands, type: project_commands}}
stop_conditions: {{max_iterations: {max_iterations}, max_runtime_minutes: 30, max_cost_usd: 3, no_progress_iterations: 2}}
human_gates: [merge]
retry: {{enabled: true, max_attempts: {max_attempts}}}
"""


def definition(tmp_path, mode="observe", max_iterations=2, max_attempts=2):
    path = tmp_path / "loop.yaml"
    path.write_text(_DEFINITION_YAML.format(mode=mode, max_iterations=max_iterations, max_attempts=max_attempts))
    return glr.LoopDefinition.from_yaml(path)


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
def _no_real_memory_root(monkeypatch, tmp_path):
    """run_all_issues ends with learning.apply_outcomes, which writes memory
    frontmatter under memory_store.DEFAULT_MEMORY_ROOT - point it at tmp."""
    import memory_store
    monkeypatch.setattr(memory_store, "DEFAULT_MEMORY_ROOT", tmp_path / "memory-root")


def _stub_batch(monkeypatch):
    monkeypatch.setattr(glr, "invoke_batch_issue_agent",
                        lambda *a, **k: {"changed": True, "cost_usd": 0.0})
    monkeypatch.setattr(glr, "invoke_batch_end_of_run_agent",
                        lambda *a, **k: {"changed": True, "cost_usd": 0.0})
    monkeypatch.setattr(glr, "list_assigned_issues", lambda aliases, username: {})


def test_run_all_issues_applies_memory_outcomes_under_repo_root(tmp_path, monkeypatch):
    _stub_batch(monkeypatch)
    seen = {}
    monkeypatch.setattr(glr.learning, "apply_outcomes",
                        lambda evs, **kw: seen.update(kw) or [])
    repo = tmp_path / "repo"
    repo.mkdir()
    glr.run_all_issues(
        "run_20260907_100000", results_dir=tmp_path / "r",
        definition_path=REPO_ROOT / "loops" / "gitlab-issue" / "loop.yaml",
        aliases=[], username="u", events_dir=tmp_path / "events",
        repo_root=repo, unified_log_path=tmp_path / "u.log",
    )
    assert seen["applied_path"] == repo / "outputs" / "learning" / "applied.json"


def test_run_all_issues_swallows_and_logs_apply_outcomes_failure(tmp_path, monkeypatch):
    _stub_batch(monkeypatch)

    def boom(evs, **kw):
        raise RuntimeError("scoring broke")

    monkeypatch.setattr(glr.learning, "apply_outcomes", boom)
    repo = tmp_path / "repo"
    repo.mkdir()
    log = tmp_path / "u.log"
    glr.run_all_issues(
        "run_20260907_100000", results_dir=tmp_path / "r",
        definition_path=REPO_ROOT / "loops" / "gitlab-issue" / "loop.yaml",
        aliases=[], username="u", events_dir=tmp_path / "events",
        repo_root=repo, unified_log_path=log,
    )
    assert "memory down-weighting FAILED: RuntimeError: scoring broke" in log.read_text()


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


@pytest.fixture(autouse=True)
def _no_real_project_config(monkeypatch, tmp_path):
    """Safe default for the loop_config lookups the external
    verifier (ProjectCommandsVerifier) makes: a project with
    no test_cmd/lint_cmd and a worktree root that will never have a
    matching directory created under it, so the external verifier is a
    guaranteed no-op unless a test explicitly overrides these two - same
    shape of safety net as _no_real_ai_cli/slack_calls above, for the same
    reason (CLAUDE.md's development-mode rule: never reach real
    ~/.loop-engineering from a test)."""
    monkeypatch.setattr(glr.loop_config, "get_project", lambda alias: {"local_path": f"/nonexistent/{alias}"})
    monkeypatch.setattr(glr.loop_config, "get_worktree_root", lambda: str(tmp_path / "nonexistent-worktree-root"))


@pytest.fixture(autouse=True)
def _all_issues_enabled_by_default(monkeypatch):
    """Safe default for the issue_tracking_config lookup run_all_issues now
    makes before each issue: every issue enabled unless a test explicitly
    overrides this - same shape of safety net as _no_real_project_config
    above, so a test that doesn't care about the toggle never depends on
    this machine's real ~/.loop-engineering/issue_tracking.json."""
    monkeypatch.setattr(glr.issue_tracking_config, "is_issue_enabled", lambda alias, issue_iid: True)


@pytest.fixture(autouse=True)
def _no_real_block_templates(monkeypatch):
    """Safe default for slack_notify.resolve_blocks, which
    _notify_slack_best_effort now calls in-process whenever a caller
    passes notification_key: unstubbed it reads the real
    ~/.slack/config.json from disk - the same shape of accident the
    slack_calls fixture above already prevents for post_message itself.
    Returns None (no template bound) unless a test explicitly overrides
    this, matching a fresh install's real behavior."""
    monkeypatch.setattr(glr.slack_notify, "resolve_blocks", lambda *a, **k: None)


def test_the_modules_safety_net_stubs_both_the_ai_cli_and_slack(slack_calls):
    """Guards the safety net itself. `_notify_slack_best_effort` calls
    slack_notify.post_message in-process, which reads the real
    ~/.slack/config.json and POSTs over the network - the same shape of
    accident as the real-`claude` invocation that hung this suite once. A
    test that forgets to fake Slack must hit the recording stub instead."""
    assert os.environ["PATH"] == SANITIZED_PATH
    assert glr._notify_slack_best_effort("a message nobody should receive") is True
    assert slack_calls == ["a message nobody should receive"]


def test_notify_slack_best_effort_passes_resolved_blocks_to_post_message(monkeypatch, slack_calls):
    captured = {}
    monkeypatch.setattr(
        glr.slack_notify, "resolve_blocks",
        lambda notification_key, message, **kwargs: [{"type": "divider"}] if notification_key == "gitlab_wrapup_failed" else None,
    )

    def fake_post_message(text, **kwargs):
        captured["text"] = text
        captured["blocks"] = kwargs.get("blocks")

    monkeypatch.setattr(glr.slack_notify, "post_message", fake_post_message)

    assert glr._notify_slack_best_effort("wrap-up failed", notification_key="gitlab_wrapup_failed") is True

    assert captured == {"text": "wrap-up failed", "blocks": [{"type": "divider"}]}


def test_notify_slack_best_effort_sends_no_blocks_when_no_notification_key(slack_calls):
    assert glr._notify_slack_best_effort("plain alert") is True
    assert slack_calls == ["plain alert"]


def test_notify_slack_best_effort_retries_without_blocks_when_post_message_rejects_blocks(monkeypatch):
    monkeypatch.setattr(
        glr.slack_notify, "resolve_blocks",
        lambda notification_key, message, **kwargs: [{"type": "divider"}],
    )
    calls = []

    def fake_post_message(text, **kwargs):
        calls.append((text, kwargs.get("blocks")))
        if kwargs.get("blocks"):
            raise RuntimeError("Slack rejected malformed blocks")

    monkeypatch.setattr(glr.slack_notify, "post_message", fake_post_message)

    assert glr._notify_slack_best_effort("wrap-up failed", notification_key="gitlab_wrapup_failed") is True

    assert calls == [
        ("wrap-up failed", [{"type": "divider"}]),
        ("wrap-up failed", None),
    ]


def test_notify_slack_best_effort_does_not_retry_when_there_were_no_blocks_to_blame(monkeypatch):
    monkeypatch.setattr(glr.slack_notify, "resolve_blocks", lambda *a, **k: None)
    calls = []

    def fake_post_message(text, **kwargs):
        calls.append((text, kwargs.get("blocks")))
        raise RuntimeError("webhook unreachable")

    monkeypatch.setattr(glr.slack_notify, "post_message", fake_post_message)

    assert glr._notify_slack_best_effort("plain alert") is False

    assert calls == [("plain alert", None)]


def test_alert_on_incomplete_results_passes_gitlab_issues_incomplete_key(monkeypatch):
    from loop_result import LoopResult
    captured = {}
    monkeypatch.setattr(
        glr, "_notify_slack_best_effort",
        lambda message, notification_key=None: captured.update(message=message, notification_key=notification_key) or True,
    )
    result = LoopResult(
        loop_id="loop_stub", run_id="r_a_1", definition_name="gitlab-issue-loop",
        final_state=glr.LoopState.FAILED, iterations=[], stop_reason="boom",
    )

    glr._alert_on_incomplete_results([result])

    assert captured["notification_key"] == "gitlab_issues_incomplete"
    assert "r_a_1" in captured["message"]


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

    assert result["changed"] is True
    assert result["cost_usd"] == 0.42
    # The token/cache breakdown must survive alongside cost_usd - this is
    # the data compute_cost_metrics needs to report cache-hit visibility
    # (see bin/cost.py), previously dropped between here and
    # _emit_run_completed.
    assert result["usage"]["input_tokens"] == 100
    assert result["usage"]["output_tokens"] == 50
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

    def fake_invoke(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None,
                    feedback=None, gate=False, run_id=None, max_budget_usd=None):
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
    def fake_invoke(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None,
                    feedback=None, gate=False, run_id=None, max_budget_usd=None):
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


def test_run_all_issues_skips_issues_disabled_in_issue_tracking_config(tmp_path, monkeypatch):
    calls = []

    def fake_invoke(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None,
                    feedback=None, gate=False, run_id=None, max_budget_usd=None):
        calls.append((alias, issue_iid))
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
    monkeypatch.setattr(
        glr.issue_tracking_config, "is_issue_enabled",
        lambda alias, issue_iid: issue_iid != 1,
    )

    results_dir = tmp_path / "loop-runs"
    results = glr.run_all_issues(
        "run_20260916_090000", results_dir=results_dir,
        definition_path=REPO_ROOT / "loops" / "gitlab-issue" / "loop.yaml",
        aliases=["harbor"], username="encore",
        events_dir=tmp_path / "events",
    )

    assert calls == [("harbor", 2)]
    assert [r.final_state.value for r in results] == ["completed"]


def test_run_single_issue_writes_its_own_result(tmp_path, monkeypatch):
    monkeypatch.setattr(
        glr, "invoke_issue_agent",
        lambda alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None, feedback=None, gate=False, run_id=None, max_budget_usd=None: {
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
    def fake_invoke(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None,
                    feedback=None, gate=False, run_id=None, max_budget_usd=None):
        calls.append(("issue", alias, issue_iid, timeout_seconds))
        if issue_iid in failing_iids:
            raise RuntimeError(f"agent crashed on {issue_iid}")
        return {"changed": True, "cost_usd": 0.25}

    return fake_invoke


def _fake_wrapup_invoker(calls, exc=None):
    def fake_wrapup(repo_root=None, timeout_seconds=900, unified_log_path=None,
                    feedback=None, gate=False, run_id=None, max_budget_usd=None):
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

    def fake_invoke(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None,
                    feedback=None, gate=False, run_id=None, max_budget_usd=None):
        captured.append(timeout_seconds)
        return {"changed": True, "cost_usd": 0.0}

    glr._run_one_issue(
        "run_x", "harbor", 3, definition, tmp_path / "loop-runs", REPO_ROOT,
        agent_invoker=fake_invoke, events_dir=tmp_path / "events",
    )

    assert captured == [definition.stop_conditions.max_runtime_minutes * 60]


def _fake_invoke(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None,
                 feedback=None, gate=False, run_id=None, max_budget_usd=None):
    return {"changed": True, "cost_usd": 0.0}


def test_external_verifier_runs_inside_runtime_in_observe_mode(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(glr, "invoke_batch_issue_agent", lambda *a, **k: calls.append(k) or {"cost_usd": 0.1})
    monkeypatch.setattr(glr, "build_verifiers", lambda specs, cwd=None, issue=None, mode="observe":
                        [FakeVerifier(passed=True, observed_passed=False)])
    result = glr._run_one_issue("run", "web", 7, definition(tmp_path, mode="observe"), tmp_path, tmp_path,
                                agent_invoker=glr.invoke_batch_issue_agent, events_dir=tmp_path)
    assert result.final_state.value == "completed" and len(calls) == 1


def test_observe_mode_failing_checks_do_not_change_outcome_but_are_recorded(tmp_path, monkeypatch):
    worktree_root = tmp_path / "worktrees"
    (worktree_root / "harbor-issue-3").mkdir(parents=True)
    monkeypatch.setattr(glr.loop_config, "get_project", lambda alias: {
        "local_path": "/x/harbor", "test_cmd": "false",  # deliberately failing
    })
    monkeypatch.setattr(glr.loop_config, "get_worktree_root", lambda: str(worktree_root))

    result = glr._run_one_issue(
        "run_x", "harbor", 3, definition(tmp_path), tmp_path / "loop-runs", REPO_ROOT,
        agent_invoker=_fake_invoke, events_dir=tmp_path / "events",
    )

    # A failing external check must NOT flip an already-completed issue
    # in observe mode, nor trigger a retry.
    assert result.final_state.value == "completed"
    assert len(result.iterations) == 1
    recorded = result.iterations[-1].verification_results
    assert [v.name for v in recorded] == ["project_commands"]
    assert recorded[0].passed is True and recorded[0].evidence["observed_passed"] is False


def test_no_worktree_is_vacuous_and_emits_external_skipped(tmp_path, monkeypatch):
    import events as events_module

    # _no_real_project_config's default worktree root never has a matching
    # directory, so this is the "no worktree" path.
    monkeypatch.setattr(glr.loop_config, "get_project", lambda alias: {"local_path": "/x/harbor", "test_cmd": "true"})
    monkeypatch.setattr(glr, "finalize_gated_issue", lambda *a, **k: "answered")
    events_dir = tmp_path / "events"
    result = glr._run_one_issue(
        "run_x", "harbor", 1, definition(tmp_path, mode="gate"), tmp_path / "loop-runs", REPO_ROOT,
        agent_invoker=_fake_invoke, events_dir=events_dir,
    )

    assert result.final_state.value == "completed"
    recorded = list(events_module.iter_events(events_dir=events_dir))
    skipped = [e for e in recorded if e["event_type"] == "verification.external_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["project"] == "harbor" and skipped[0]["issue_iid"] == 1
    assert not [e for e in recorded if e["event_type"] == "verification.external_completed"]


def test_external_completed_event_carries_mode_and_observed_result(tmp_path, monkeypatch):
    import events as events_module

    worktree_root = tmp_path / "worktrees"
    (worktree_root / "harbor-issue-5").mkdir(parents=True)
    monkeypatch.setattr(glr.loop_config, "get_project", lambda alias: {
        "local_path": "/x/harbor", "test_cmd": "true", "lint_cmd": "false",
    })
    monkeypatch.setattr(glr.loop_config, "get_worktree_root", lambda: str(worktree_root))

    events_dir = tmp_path / "events"
    glr._run_one_issue(
        "run_x", "harbor", 5, definition(tmp_path), tmp_path / "loop-runs", REPO_ROOT,
        agent_invoker=_fake_invoke, events_dir=events_dir,
    )

    recorded = list(events_module.iter_events(events_dir=events_dir))
    external = [e for e in recorded if e["event_type"] == "verification.external_completed"]
    assert len(external) == 1
    assert external[0]["data"] == {
        "verifier": "project_commands", "passed": True, "observed_passed": False,
        "mode": "observe", "iteration": 1,
    }


def test_misconfigured_project_never_crashes_a_batch(tmp_path, monkeypatch):
    def _boom(alias):
        raise RuntimeError("config exploded")

    monkeypatch.setattr(glr.loop_config, "get_project", _boom)
    result = glr._run_one_issue(
        "run_x", "harbor", 1, definition(tmp_path), tmp_path / "loop-runs", REPO_ROOT,
        agent_invoker=_fake_invoke, events_dir=tmp_path / "events",
    )
    assert result.final_state.value == "completed"
    assert result.iterations[-1].verification_results[0].evidence["observed_passed"] is False


def test_stored_verifier_output_is_bounded(tmp_path, monkeypatch):
    worktree_root = tmp_path / "worktrees"
    (worktree_root / "repo-issue-42").mkdir(parents=True)
    monkeypatch.setattr(glr.loop_config, "get_project", lambda alias: {
        "local_path": "/some/repo", "test_cmd": "python3 -c \"print('x' * 50000)\"",
    })
    monkeypatch.setattr(glr.loop_config, "get_worktree_root", lambda: str(worktree_root))
    result = glr._run_one_issue(
        "run_x", "harbor", 42, definition(tmp_path), tmp_path / "loop-runs", REPO_ROOT,
        agent_invoker=_fake_invoke, events_dir=tmp_path / "events",
    )
    assert len(result.iterations[-1].verification_results[0].output) <= 4200


def test_gate_failure_retries_with_feedback(monkeypatch, tmp_path):
    monkeypatch.setattr(glr, "finalize_gated_issue", lambda *a, **k: "mr_opened")
    calls = []
    monkeypatch.setattr(glr, "invoke_batch_issue_agent", lambda *a, **k: calls.append(k) or {"cost_usd": 0.1})
    seq = iter([False, True])
    monkeypatch.setattr(glr, "build_verifiers", lambda specs, cwd=None, issue=None, mode="observe":
                        [SequenceVerifier(seq, output="$ bundle exec rspec\n1 example, 1 failure")])
    result = glr._run_one_issue("run", "web", 7, definition(tmp_path, mode="gate"), tmp_path, tmp_path,
                                agent_invoker=glr.invoke_batch_issue_agent, events_dir=tmp_path)
    assert len(calls) == 2
    assert calls[0].get("feedback") is None
    assert "1 example, 1 failure" in calls[1]["feedback"]
    assert calls[0]["gate"] is True
    assert result.final_state.value == "completed"


def test_feedback_is_bounded():
    prev = FakeIteration(verification_results=[FakeResult("pc", False, "x" * 50_000)])
    assert len(glr.format_feedback(prev)) <= 6000


def test_feedback_has_header_and_failing_output_tail():
    prev = FakeIteration(verification_results=[FakeResult("pc", False, "$ rspec\nlots\nFAILED")])
    text = glr.format_feedback(prev)
    assert text.startswith("## Previous attempt failed external verification")
    assert "$ rspec" in text and "FAILED" in text


def test_same_failure_twice_stops_no_progress(monkeypatch, tmp_path):
    monkeypatch.setattr(glr, "invoke_batch_issue_agent", lambda *a, **k: {"cost_usd": 0.1})
    monkeypatch.setattr(glr, "build_verifiers", lambda *a, **k: [SequenceVerifier(iter([False, False]), output="same")])
    result = glr._run_one_issue("run", "web", 7, definition(tmp_path, mode="gate", max_iterations=3, max_attempts=3),
                                tmp_path, tmp_path, agent_invoker=glr.invoke_batch_issue_agent, events_dir=tmp_path)
    assert result.final_state.value in ("blocked", "escalated", "stopped")
    assert "progress" in (result.stop_reason or "")


def test_invokers_append_feedback_to_the_prompt(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(glr, "build_batch_issue_prompt", lambda alias, iid, repo_root=None: "BASE")
    monkeypatch.setattr(glr, "build_prompt", lambda alias=None, issue_iid=None, repo_root=None: "SINGLE")
    monkeypatch.setattr(glr, "_invoke_cli_with_prompt", lambda prompt, **k: seen.append(prompt) or {})
    glr.invoke_batch_issue_agent("web", 7, repo_root=tmp_path, feedback="FIX IT")
    glr.invoke_issue_agent("web", 7, repo_root=tmp_path, feedback="FIX IT")
    glr.invoke_batch_issue_agent("web", 7, repo_root=tmp_path)
    assert seen[0].startswith("BASE") and seen[0].endswith("FIX IT")
    assert seen[1].startswith("SINGLE") and seen[1].endswith("FIX IT")
    assert seen[2] == "BASE"


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


def _fake_per_issue_invoker_with_usage(calls, usages):
    """Like _fake_per_issue_invoker, but returns a real usage/cache-token
    payload per issue_iid (from `usages`), so the aggregation path from
    _invoke_cli_with_prompt's return value through to run.completed's
    emitted data can be exercised end-to-end."""
    def fake_invoke(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None,
                    feedback=None, gate=False, run_id=None, max_budget_usd=None):
        calls.append(("issue", alias, issue_iid, timeout_seconds))
        usage = usages[issue_iid]
        return {"changed": True, "cost_usd": usage["cost_usd"], "usage": usage}

    return fake_invoke


def test_main_aggregates_cache_token_usage_across_issues(tmp_path, monkeypatch):
    """The whole point of threading `usage` through _invoke_cli_with_prompt:
    run.completed's data must carry summed input/output/cache tokens so
    bin/cost.py's compute_cost_metrics can report real cache-hit numbers -
    previously this data was silently dropped and total_tokens was always
    0 for every real run."""
    usages = {
        1: {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 300, "cache_write_tokens": 10, "cost_usd": 0.2},
        2: {"input_tokens": 50, "output_tokens": 10, "cache_read_tokens": 150, "cache_write_tokens": 0, "cost_usd": 0.1},
    }
    monkeypatch.setattr(glr, "invoke_batch_issue_agent", _fake_per_issue_invoker_with_usage([], usages))
    monkeypatch.setattr(glr, "invoke_batch_end_of_run_agent", _fake_wrapup_invoker([]))
    monkeypatch.setattr(
        glr, "list_assigned_issues",
        lambda aliases, username: {"harbor": [{"iid": 1}, {"iid": 2}]},
    )

    events_dir = tmp_path / "events"
    glr.main(
        argv=["run_20260915_100000"], results_dir=tmp_path / "loop-runs",
        definition_path=DEFINITION_PATH, events_dir=events_dir,
        aliases=["harbor"], username="encore",
    )

    completed = [e for e in _read_events(events_dir) if e["event_type"] == "run.completed"]
    assert len(completed) == 1
    data = completed[0]["data"]
    assert data["input_tokens"] == 150
    assert data["output_tokens"] == 30
    assert data["cache_read_tokens"] == 450
    assert data["cache_write_tokens"] == 10


def test_main_omits_token_fields_when_nothing_was_actually_priced(tmp_path, monkeypatch):
    """Mirrors cost_usd's own omission rule: a Codex-only run (or a failed
    Claude cost extraction) reports no usage data at all, so the token
    fields must be absent, not zero - a zero would falsely look like "we
    know this run used 0 tokens" rather than "we never measured it"."""
    monkeypatch.setattr(glr, "invoke_batch_issue_agent", _fake_unpriced_invoker([]))
    monkeypatch.setattr(glr, "invoke_batch_end_of_run_agent", _fake_wrapup_invoker([]))
    monkeypatch.setattr(
        glr, "list_assigned_issues",
        lambda aliases, username: {"harbor": [{"iid": 1}, {"iid": 2}]},
    )

    events_dir = tmp_path / "events"
    glr.main(
        argv=["run_20260915_110000"], results_dir=tmp_path / "loop-runs",
        definition_path=DEFINITION_PATH, events_dir=events_dir,
        aliases=["harbor"], username="encore",
    )

    completed = [e for e in _read_events(events_dir) if e["event_type"] == "run.completed"]
    assert len(completed) == 1
    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        assert key not in completed[0]["data"]


def _fake_unpriced_invoker(calls):
    """Every issue comes back with `cost_usd: None` - the Codex path (which
    reports no cost at all), or a Claude run whose cost extraction failed."""
    def fake_invoke(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None,
                    feedback=None, gate=False, run_id=None, max_budget_usd=None):
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

    assert result["changed"] is True
    assert result["cost_usd"] == 0.1
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
        "Bash(python3 bin/*.py*)", "Bash(python3 bin/web/*.py*)",
        "Bash(bash bin/scripts/new_worktree.sh*)", "Bash(bash bin/scripts/open_merge_request.sh*)",
        f"Bash(python3 {REPO_ROOT}/bin/*.py*)",
        f"Bash(python3 {REPO_ROOT}/bin/web/*.py*)",
        f"Bash(bash {REPO_ROOT}/bin/scripts/new_worktree.sh*)",
        f"Bash(bash {REPO_ROOT}/bin/scripts/open_merge_request.sh*)",
    ):
        assert expected in allowed, f"expected {expected!r} in allowedTools"
    assert "Read Edit Write" in allowed
    # No blanket script glob: it could not exclude open_merge_request.sh in gate mode.
    assert "bin/scripts/*.sh*" not in allowed


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

    assert result == {"changed": True, "cost_usd": None, "usage": None}
    assert "codex did the thing" in unified_log.read_text()


def test_allowed_tools_cover_loop_plugins(tmp_path):
    tools = glr._allowed_tools(tmp_path)
    assert "Bash(python3 bin/loop_plugins/*.py*)" in tools
    assert f"Bash(python3 {tmp_path}/bin/loop_plugins/*.py*)" in tools


def test_feedback_keeps_failing_test_command_when_lint_output_is_long(tmp_path):
    (tmp_path / "wt" / "repo-issue-7").mkdir(parents=True)
    project = {
        "local_path": str(tmp_path / "repo"),
        "test_cmd": "python3 -c \"import sys; print('T' * 3000 + 'TESTTAIL'); sys.exit(1)\"",
        "lint_cmd": "python3 -c \"import sys; print('L' * 3000); sys.exit(1)\"",
    }
    v = lv.ProjectCommandsVerifier("project_commands", "web", 7, 30, project_fn=lambda a: project,
                                   worktree_root_fn=lambda: tmp_path / "wt")
    text = glr.format_feedback(FakeIteration([v.verify({})]))
    assert "$ python3 -c \"import sys; print('T'" in text and "TESTTAIL" in text and "L" * 100 in text
    assert len(text) <= 6000


def test_feedback_omits_passing_commands(tmp_path):
    (tmp_path / "wt" / "repo-issue-7").mkdir(parents=True)
    project = {"local_path": str(tmp_path / "repo"), "test_cmd": "echo GOODOUTPUT",
               "lint_cmd": "python3 -c \"import sys; print('BADLINT'); sys.exit(1)\""}
    v = lv.ProjectCommandsVerifier("project_commands", "web", 7, 30, project_fn=lambda a: project,
                                   worktree_root_fn=lambda: tmp_path / "wt")
    text = glr.format_feedback(FakeIteration([v.verify({})]))
    assert "BADLINT" in text and "GOODOUTPUT" not in text


def test_gate_mode_allowed_tools_exclude_open_mr_script_and_push(tmp_path):
    ungated = glr._allowed_tools(tmp_path, gate=False).split()
    assert any("open_merge_request.sh" in t for t in ungated)
    assert "Bash(git push origin loop/issue-*)" in " ".join(ungated)
    gated = glr._allowed_tools(tmp_path, gate=True).split()
    assert not any("open_merge_request.sh" in t for t in gated)
    assert not any(t.startswith("push") or "git push" in t for t in gated)
    assert "Bash(git push origin loop/issue-*)" not in glr._allowed_tools(tmp_path, gate=True)
    assert any("new_worktree.sh" in t for t in gated)
    assert f"Write({tmp_path}/outputs/handoffs/**)" in glr._allowed_tools(tmp_path, gate=True)


def test_gate_mode_disallows_open_mr_script_and_push(tmp_path):
    cmd = glr._cli_command("claude", "p", tmp_path, "/wt", gate=True)
    disallowed = cmd[cmd.index("--disallowedTools") + 1]
    assert "open_merge_request.sh" in disallowed and "Bash(git push origin loop/issue-*)" in disallowed
    assert "Bash(git merge*)" in disallowed
    cmd = glr._cli_command("claude", "p", tmp_path, "/wt")
    assert "open_merge_request.sh" not in cmd[cmd.index("--disallowedTools") + 1]


def test_gate_prompt_appends_override(monkeypatch, tmp_path):
    monkeypatch.setattr(glr, "_run_build_run_prompt", lambda args, repo_root: "BASE PROMPT")
    captured = {}
    monkeypatch.setattr(glr, "_invoke_cli_with_prompt", lambda prompt, **kw: captured.update(prompt=prompt, **kw) or {"cost_usd": 0})
    glr.invoke_batch_issue_agent("web", 7, repo_root=tmp_path, timeout_seconds=5, gate=True, run_id="run_x",
                                 feedback="FEEDBACK")
    handoff = str(glr.handoff_path("run_x", "web", 7, repo_root=tmp_path))
    assert captured["prompt"].startswith("BASE PROMPT")
    assert captured["prompt"].endswith(glr.GATE_OVERRIDE.replace("<handoff_path>", handoff))
    assert handoff in captured["prompt"] and "<handoff_path>" not in captured["prompt"]
    assert captured["prompt"].index("FEEDBACK") < captured["prompt"].index("Harness gate is ON")
    assert captured["env"]["LOOP_HANDOFF_PATH"].endswith("outputs/handoffs/run_x/web-7.json")
    assert captured["gate"] is True


def test_gate_mode_single_issue_invoker_also_overrides(monkeypatch, tmp_path):
    monkeypatch.setattr(glr, "_run_build_run_prompt", lambda args, repo_root: "BASE PROMPT")
    captured = {}
    monkeypatch.setattr(glr, "_invoke_cli_with_prompt", lambda prompt, **kw: captured.update(prompt=prompt, **kw) or {})
    glr.invoke_issue_agent("web", 7, repo_root=tmp_path, gate=True, run_id="run_y")
    assert "Harness gate is ON" in captured["prompt"]
    assert captured["env"]["LOOP_HANDOFF_PATH"].endswith("outputs/handoffs/run_y/web-7.json")


def test_gate_mode_without_run_id_uses_env_then_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(glr, "_run_build_run_prompt", lambda args, repo_root: "BASE")
    captured = {}
    monkeypatch.setattr(glr, "_invoke_cli_with_prompt", lambda prompt, **kw: captured.update(**kw) or {})
    monkeypatch.setenv("LOOP_RUN_ID", "run_env")
    glr.invoke_batch_issue_agent("web", 7, repo_root=tmp_path, gate=True)
    assert captured["env"]["LOOP_HANDOFF_PATH"].endswith("outputs/handoffs/run_env/web-7.json")
    monkeypatch.delenv("LOOP_RUN_ID")
    with pytest.raises(ValueError, match="run_id"):
        glr.invoke_batch_issue_agent("web", 7, repo_root=tmp_path, gate=True)


def test_observe_prompt_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(glr, "_run_build_run_prompt", lambda args, repo_root: "BASE PROMPT")
    captured = {}
    monkeypatch.setattr(glr, "_invoke_cli_with_prompt", lambda prompt, **kw: captured.update(prompt=prompt, **kw) or {"cost_usd": 0})
    glr.invoke_batch_issue_agent("web", 7, repo_root=tmp_path, timeout_seconds=5, gate=False)
    assert captured["prompt"] == "BASE PROMPT" and captured.get("env") is None


def test_handoff_path_layout(tmp_path):
    assert glr.handoff_path("run_x", "web", 7, repo_root=tmp_path) == tmp_path / "outputs" / "handoffs" / "run_x" / "web-7.json"


def test_run_one_issue_passes_run_id_to_the_invoker(tmp_path):
    seen = []

    def invoker(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None,
                feedback=None, gate=False, run_id=None, max_budget_usd=None):
        seen.append(run_id)
        return {"cost_usd": 0}

    glr._run_one_issue("run_z", "harbor", 1, definition(tmp_path), tmp_path / "r", REPO_ROOT,
                       agent_invoker=invoker, events_dir=tmp_path / "e")
    assert seen == ["run_z"]


def test_invoke_cli_passes_env_only_when_given(tmp_path, monkeypatch):
    calls = []

    class Proc:
        stdout, stderr = "", ""

    monkeypatch.setattr(glr.subprocess, "run", lambda cmd, **kw: calls.append(kw) or Proc())
    monkeypatch.setattr(glr.ai_cli_config, "get_selected_cli", lambda: "codex")
    monkeypatch.setattr(glr.loop_config, "get_worktree_root", lambda: str(tmp_path))
    glr._invoke_cli_with_prompt("p", repo_root=tmp_path, unified_log_path=tmp_path / "u.log")
    glr._invoke_cli_with_prompt("p", repo_root=tmp_path, unified_log_path=tmp_path / "u.log", env={"LOOP_HANDOFF_PATH": "/x"})
    assert "env" not in calls[0]
    assert calls[1]["env"]["LOOP_HANDOFF_PATH"] == "/x" and "PATH" in calls[1]["env"]


# --- Gate mode: harness-owned MR creation and escalation ---------------------


class FakeGitLabAPI:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def api(self, method, path, json_body=None, **kw):
        self.calls.append((method, path, json_body))
        if self.fail:
            raise RuntimeError("gitlab down")
        return {}


def _loop_result(state, output="out", evidence=None):
    import loop_result
    from loop_state import LoopState
    v = lv.VerificationResult("project_commands", state == LoopState.COMPLETED, 0, 1, output, evidence or {})
    it = loop_result.IterationResult(1, state, [v], {}, True)
    return loop_result.LoopResult("l", "run_web_7", "d", state, [it], "x")


def completed_result():
    from loop_state import LoopState
    return _loop_result(LoopState.COMPLETED)


def failed_result(output, evidence=None):
    from loop_state import LoopState
    return _loop_result(LoopState.ESCALATED, output, evidence)


_PROJECT = {"local_path": "/x/web", "instance": "gl", "project_id": "grp/web",
            "test_cmd": "rspec", "lint_cmd": "rubocop ."}
_FIX = '{"action":"fix","branch":"loop/issue-7","target_branch":"main","title":"Fix #7: x","summary":"the summary"}'


def _finalize(tmp_path, result, handoff_text=_FIX, opener=None, gitlab=None, notifier=None, **kw):
    hp = tmp_path / "h.json"
    if handoff_text is not None:
        hp.write_text(handoff_text)
    return glr.finalize_gated_issue(
        result, "web", 7, "run", tmp_path, handoff=hp, project=_PROJECT, opener=opener,
        gitlab=gitlab if gitlab is not None else FakeGitLabAPI(), notifier=notifier or (lambda *a: None),
        events_dir=tmp_path / "ev", **kw)


def test_gate_pass_opens_mr_and_comments(tmp_path):
    opened, gl = [], FakeGitLabAPI()
    out = _finalize(tmp_path, completed_result(), opener=lambda *a: opened.append(a) or (True, "ok"), gitlab=gl)
    assert out == "mr_opened" and opened[0] == ("/x/web", "loop/issue-7", "main", "Fix #7: x")
    assert gl.calls[0][0] == "POST" and gl.calls[0][1].endswith("/issues/7/notes")
    assert "grp%2Fweb" in gl.calls[0][1]
    body = gl.calls[0][2]["body"]
    assert "Opened merge request for this fix: loop/issue-7 → main" in body
    assert "`rspec` ✅" in body and "`rubocop .` ✅" in body and "the summary" in body


def test_gate_fail_escalates_with_label_and_no_mr(tmp_path):
    opened, gl, notes = [], FakeGitLabAPI(), []
    out = _finalize(tmp_path, failed_result("1 example, 1 failure"), opener=lambda *a: opened.append(a),
                    gitlab=gl, notifier=lambda *a: notes.append(a))
    assert out == "escalated:verification_failed" and opened == [] and len(notes) == 1
    note = gl.calls[0][2]["body"]
    for section in ("**What I tried**", "**What failed**", "**Where the work is**", "**What I need from you**"):
        assert section in note
    assert "1 example, 1 failure" in note and "```" in note
    assert gl.calls[1][0] == "PUT" and gl.calls[1][2]["add_labels"] == "loop:needs-human"
    assert not gl.calls[1][1].endswith("/notes")


def test_gate_fail_uses_per_command_tails_bounded(tmp_path):
    ev = {"commands": [{"command": "rspec", "passed": False, "output": "BOOM"},
                       {"command": "rubocop .", "passed": True, "output": "CLEAN"}]}
    gl = FakeGitLabAPI()
    _finalize(tmp_path, failed_result("x" * 9000, ev), gitlab=gl)
    note = gl.calls[0][2]["body"]
    assert "$ rspec\nBOOM" in note and "CLEAN" not in note
    gl = FakeGitLabAPI()
    _finalize(tmp_path, failed_result("Y" * 9000), gitlab=gl)
    assert gl.calls[0][2]["body"].count("Y") == 1500


def test_missing_handoff_escalates_without_mr(tmp_path):
    opened, gl = [], FakeGitLabAPI()
    out = _finalize(tmp_path, completed_result(), handoff_text=None, opener=lambda *a: opened.append(a), gitlab=gl)
    assert out == "escalated:handoff_invalid" and opened == []
    assert gl.calls[1][2]["add_labels"] == "loop:needs-human"


def test_handoff_branch_must_match_issue(tmp_path):
    hp = tmp_path / "h.json"
    hp.write_text('{"action":"fix","branch":"loop/issue-8","target_branch":"main","title":"t","summary":"s"}')
    assert glr.read_handoff(hp, issue_iid=7) is None
    hp.write_text('{"action":"fix","branch":"main","target_branch":"main","title":"t"}')
    assert glr.read_handoff(hp, issue_iid=7) is None
    hp.write_text('{"action":"fix","branch":"loop/issue-7","target_branch":"","title":"t"}')
    assert glr.read_handoff(hp, issue_iid=7) is None
    hp.write_text("not json")
    assert glr.read_handoff(hp, issue_iid=7) is None
    hp.write_text('{"action":"answer"}')
    assert glr.read_handoff(hp, issue_iid=7) == {"action": "answer"}
    hp.write_text(_FIX)
    assert glr.read_handoff(hp, issue_iid=7)["title"] == "Fix #7: x"


def test_answer_and_escalate_handoffs_are_noops(tmp_path):
    gl = FakeGitLabAPI()
    assert _finalize(tmp_path, completed_result(), '{"action":"answer"}', gitlab=gl) == "answered"
    assert _finalize(tmp_path, completed_result(), '{"action":"escalate"}', gitlab=gl) == "escalated:agent"
    assert gl.calls == []


def test_opener_failure_escalates_instead_of_claiming_success(tmp_path):
    gl = FakeGitLabAPI()
    out = _finalize(tmp_path, completed_result(), opener=lambda *a: (False, "push rejected"), gitlab=gl)
    assert out == "escalated:mr_open_failed"
    assert "push rejected" in gl.calls[0][2]["body"] and "Opened merge request" not in gl.calls[0][2]["body"]
    assert gl.calls[1][2]["add_labels"] == "loop:needs-human"


def test_gitlab_and_notifier_failures_never_raise(tmp_path):
    def boom(*a):
        raise RuntimeError("slack down")
    out = _finalize(tmp_path, failed_result("f"), gitlab=FakeGitLabAPI(fail=True), notifier=boom)
    assert out == "escalated:verification_failed"
    out = _finalize(tmp_path, completed_result(), opener=lambda *a: (True, ""), gitlab=FakeGitLabAPI(fail=True))
    assert out == "mr_opened"


def test_gate_events_are_emitted(tmp_path):
    import events as events_module
    _finalize(tmp_path, completed_result(), opener=lambda *a: (True, ""))
    _finalize(tmp_path, failed_result("f"))
    recorded = list(events_module.iter_events(events_dir=tmp_path / "ev"))
    done = [e for e in recorded if e["event_type"] == "issue.completed"]
    esc = [e for e in recorded if e["event_type"] == "issue.escalated"]
    assert done[0]["data"] == {"outcome": "mr_opened", "gated": True, "action": "fix", "mr_url": None}
    assert esc[0]["data"]["reason"] == "verification_failed"


def test_default_opener_runs_the_script_with_the_four_arguments(tmp_path, monkeypatch):
    seen = []

    class Proc:
        returncode, stdout, stderr = 0, "ok", ""

    monkeypatch.setattr(glr.subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw)) or Proc())
    out = _finalize(tmp_path, completed_result())
    cmd, kw = seen[0]
    assert out == "mr_opened"
    assert cmd[0] == "bash" and cmd[1].endswith("bin/scripts/open_merge_request.sh")
    assert cmd[2:] == ["/x/web", "loop/issue-7", "main", "Fix #7: x"]
    assert kw["timeout"] == 300 and kw["cwd"] == str(tmp_path)


def test_observe_mode_keeps_agent_opening_mr(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(glr, "finalize_gated_issue", lambda *a, **k: called.append(1))
    monkeypatch.setattr(glr, "invoke_batch_issue_agent", lambda *a, **k: {"cost_usd": 0})
    monkeypatch.setattr(glr, "build_verifiers", lambda *a, **k: [])
    glr._run_one_issue("run", "web", 7, definition(tmp_path, mode="observe"), tmp_path, tmp_path,
                       agent_invoker=glr.invoke_batch_issue_agent, events_dir=tmp_path)
    assert called == []


def test_gate_mode_calls_finalize_and_survives_its_failure(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(glr, "finalize_gated_issue", lambda *a, **k: calls.append(a) or "mr_opened")
    monkeypatch.setattr(glr, "build_verifiers", lambda *a, **k: [])
    result = glr._run_one_issue("run", "web", 7, definition(tmp_path, mode="gate"), tmp_path, tmp_path,
                                agent_invoker=_fake_invoke, events_dir=tmp_path)
    assert len(calls) == 1 and result.gate_outcome == "mr_opened"

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(glr, "finalize_gated_issue", boom)
    result = glr._run_one_issue("run", "web", 7, definition(tmp_path, mode="gate"), tmp_path, tmp_path,
                                agent_invoker=_fake_invoke, events_dir=tmp_path)
    assert result.final_state.value == "completed" and result.gate_outcome is None


def test_mr_url_is_parsed_from_opener_output(tmp_path):
    import events as events_module
    out = "remote: View merge request for loop/issue-7:\nremote:   https://gl.example.com/grp/web/-/merge_requests/12\n"
    _finalize(tmp_path, completed_result(), opener=lambda *a: (True, out))
    done = [e for e in events_module.iter_events(events_dir=tmp_path / "ev") if e["event_type"] == "issue.completed"]
    assert done[0]["data"]["mr_url"] == "https://gl.example.com/grp/web/-/merge_requests/12"


def test_project_config_error_escalates_instead_of_stranding_the_fix(tmp_path, monkeypatch):
    import events as events_module

    def boom(alias):
        raise KeyError(alias)
    monkeypatch.setattr(glr.loop_config, "get_project", boom)
    notes, hp = [], tmp_path / "h.json"
    hp.write_text(_FIX)
    out = glr.finalize_gated_issue(completed_result(), "web", 7, "run", tmp_path, handoff=hp,
                                   notifier=lambda *a: notes.append(a), events_dir=tmp_path / "ev")
    assert out == "escalated:project_config_error" and len(notes) == 1
    esc = [e for e in events_module.iter_events(events_dir=tmp_path / "ev") if e["event_type"] == "issue.escalated"]
    assert esc[0]["data"]["reason"] == "project_config_error"
    out = glr.finalize_gated_issue(completed_result(), "web", 7, "run", tmp_path, handoff=hp,
                                   project={"local_path": "/x"}, notifier=lambda *a: None, events_dir=tmp_path / "ev")
    assert out == "escalated:project_config_error"


def test_external_completed_event_flags_verifier_error():
    import events as events_module
    from types import SimpleNamespace

    recorded = []
    original = glr._emit_best_effort
    glr._emit_best_effort = lambda event_type, **kw: recorded.append((event_type, kw))
    try:
        result = SimpleNamespace(name="project_commands", passed=False, evidence={"error": True, "observed_passed": False})
        iteration = SimpleNamespace(verification_results=[result], iteration=1)
        glr._emit_verification_events("r", "i", "p", 1, "gate", iteration)
    finally:
        glr._emit_best_effort = original
    assert recorded[0][1]["data"]["error"] is True


# --- Final-review fixes: gate mode verifies only this attempt's fix handoff ---


def _gate_env(tmp_path, monkeypatch, test_cmd="false"):
    """A real worktree whose external check fails, a full project, and a
    fake GitLab connector - so only the handoff decides what happens."""
    worktree_root = tmp_path / "wt"
    (worktree_root / "web-issue-7").mkdir(parents=True)
    project = {**_PROJECT, "local_path": str(tmp_path / "web"), "test_cmd": test_cmd, "lint_cmd": ""}
    monkeypatch.setattr(glr.loop_config, "get_project", lambda alias: project)
    monkeypatch.setattr(glr.loop_config, "get_worktree_root", lambda: str(worktree_root))
    gl = FakeGitLabAPI()
    monkeypatch.setattr(glr.connectors_config, "load_connector", lambda account: gl)
    return gl


def _handoff_writer(tmp_path, texts, calls):
    def invoker(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None,
                feedback=None, gate=False, run_id=None, max_budget_usd=None):
        calls.append(feedback)
        text = texts[min(len(calls), len(texts)) - 1]
        if isinstance(text, Exception):
            raise text
        if text is not None:
            path = glr.handoff_path(run_id, alias, issue_iid, repo_root=tmp_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return {"cost_usd": 0}
    return invoker


@pytest.mark.parametrize("handoff, outcome", [
    ('{"action": "escalate"}', "escalated:agent"),
    ('{"action": "answer"}', "answered"),
    ('{"action": "wait_for_review"}', "waiting_for_review"),
])
def test_gate_non_fix_handoff_is_not_verified_or_retried(tmp_path, monkeypatch, handoff, outcome):
    gl = _gate_env(tmp_path, monkeypatch)
    calls = []
    result = glr._run_one_issue("run", "web", 7, definition(tmp_path, mode="gate"), tmp_path / "r", tmp_path,
                                agent_invoker=_handoff_writer(tmp_path, [handoff], calls), events_dir=tmp_path / "ev")
    assert len(calls) == 1 and gl.calls == []
    assert result.gate_outcome == outcome and result.final_state.value == "completed"
    assert result.iterations[-1].verification_results[0].evidence.get("vacuous") is True


def test_gate_fix_handoff_is_verified_and_retried(tmp_path, monkeypatch):
    gl = _gate_env(tmp_path, monkeypatch)
    calls = []
    result = glr._run_one_issue("run", "web", 7, definition(tmp_path, mode="gate"), tmp_path / "r", tmp_path,
                                agent_invoker=_handoff_writer(tmp_path, [_FIX], calls), events_dir=tmp_path / "ev")
    assert len(calls) == 2 and calls[1] is not None
    assert result.gate_outcome == "escalated:verification_failed"
    assert gl.calls[0][2]["body"].count("$ false") == 1


def test_gate_clears_the_handoff_before_each_attempt(tmp_path, monkeypatch):
    gl = _gate_env(tmp_path, monkeypatch)
    calls, seen = [], []
    inner = _handoff_writer(tmp_path, [_FIX, RuntimeError("agent crashed")], calls)

    def invoker(alias, issue_iid, run_id=None, **kw):
        seen.append(glr.handoff_path(run_id, alias, issue_iid, repo_root=tmp_path).exists())
        return inner(alias, issue_iid, run_id=run_id, **kw)

    result = glr._run_one_issue("run", "web", 7, definition(tmp_path, mode="gate"), tmp_path / "r", tmp_path,
                                agent_invoker=invoker, events_dir=tmp_path / "ev")
    assert seen == [False, False]
    assert result.gate_outcome == "escalated:run_incomplete"
    body = gl.calls[0][2]["body"]
    assert "agent session failed or timed out" in body and "```" not in body


def test_run_incomplete_and_verification_wording_follow_the_stop_reason(tmp_path):
    from loop_state import LoopState
    gl = FakeGitLabAPI()
    result = _loop_result(LoopState.STOPPED)
    result.stop_reason = "budget_exceeded"
    assert _finalize(tmp_path, result, gitlab=gl) == "escalated:run_incomplete"
    assert "budget" in gl.calls[0][2]["body"]

    gl = FakeGitLabAPI()
    result = failed_result("")
    result.stop_reason = "no_progress"
    assert _finalize(tmp_path, result, gitlab=gl) == "escalated:verification_failed"
    body = gl.calls[0][2]["body"]
    assert "the same way" in body
    failed_block = body.split("**What failed**\n", 1)[1].split("\n\n**Where", 1)[0]
    assert failed_block.strip().endswith("(The checks produced no output.)")


def test_run_incomplete_event_carries_the_stop_reason(tmp_path):
    import events as events_module
    from loop_state import LoopState
    result = _loop_result(LoopState.FAILED)
    result.stop_reason = "agent_failed"
    _finalize(tmp_path, result, handoff_text=None)
    esc = [e for e in events_module.iter_events(events_dir=tmp_path / "ev") if e["event_type"] == "issue.escalated"]
    assert esc[0]["data"] == {"reason": "run_incomplete", "gated": True, "stop_reason": "agent_failed"}


def test_wait_for_review_handoff_is_a_noop(tmp_path):
    gl, notes = FakeGitLabAPI(), []
    hp = tmp_path / "h.json"
    hp.write_text('{"action": "wait_for_review"}')
    assert glr.read_handoff(hp, issue_iid=7) == {"action": "wait_for_review"}
    out = _finalize(tmp_path, completed_result(), '{"action":"wait_for_review"}', gitlab=gl,
                    notifier=lambda *a: notes.append(a))
    assert out == "waiting_for_review" and gl.calls == [] and notes == []


def test_mr_opened_sends_the_finished_slack_message(tmp_path):
    notes = []
    url = "https://gl.example.com/grp/web/-/merge_requests/12"
    _finalize(tmp_path, completed_result(), opener=lambda *a: (True, url), notifier=lambda m: notes.append(m))
    assert len(notes) == 1 and "Finished" in notes[0] and url in notes[0]


def test_gate_override_keeps_step_10_bookkeeping_and_names_every_action():
    text = glr.GATE_OVERRIDE
    assert "for the MR description" not in text
    for expected in ("mark-seen", "memory_store.py add", "loop_last_action", "wait_for_review",
                     '"answer"', '"escalate"', "Finished", "issue.completed"):
        assert expected in text, expected


@pytest.mark.parametrize("outcome", [
    "escalated:handoff_invalid", "escalated:mr_open_failed", "escalated:project_config_error",
])
def test_gated_loop_escalation_is_persisted_and_alerted_once(tmp_path, monkeypatch, slack_calls, outcome):
    monkeypatch.setattr(glr, "finalize_gated_issue", lambda *a, **k: outcome)
    monkeypatch.setattr(glr, "build_verifiers", lambda *a, **k: [])
    result = glr._run_one_issue("run", "web", 7, definition(tmp_path, mode="gate"), tmp_path / "r", tmp_path,
                                agent_invoker=_fake_invoke, events_dir=tmp_path / "ev")
    reason = outcome.split(":", 1)[1]
    assert result.final_state.value == "escalated" and result.stop_reason == f"gate:{reason}"
    stored = json.loads((tmp_path / "r" / "run_web_7" / "result.json").read_text())
    assert stored["final_state"] == "escalated" and stored["stop_reason"] == f"gate:{reason}"
    assert glr._alert_on_incomplete_results([result]) == [] and slack_calls == []


@pytest.mark.parametrize("outcome", ["mr_opened", "answered", "waiting_for_review", "escalated:agent"])
def test_gated_non_loop_escalation_outcomes_keep_the_runtime_state(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(glr, "finalize_gated_issue", lambda *a, **k: outcome)
    monkeypatch.setattr(glr, "build_verifiers", lambda *a, **k: [])
    result = glr._run_one_issue("run", "web", 7, definition(tmp_path, mode="gate"), tmp_path / "r", tmp_path,
                                agent_invoker=_fake_invoke, events_dir=tmp_path / "ev")
    assert result.final_state.value == "completed" and result.stop_reason == "completed"


def test_gated_verification_failure_keeps_the_runtime_stop_reason(tmp_path, monkeypatch, slack_calls):
    monkeypatch.setattr(glr, "finalize_gated_issue", lambda *a, **k: "escalated:verification_failed")
    monkeypatch.setattr(glr, "build_verifiers", lambda *a, **k: [SequenceVerifier(iter([False, False]), output="same")])
    result = glr._run_one_issue("run", "web", 7, definition(tmp_path, mode="gate"), tmp_path / "r", tmp_path,
                                agent_invoker=_fake_invoke, events_dir=tmp_path / "ev")
    assert result.final_state.value == "escalated" and result.stop_reason == "no_progress"
    assert glr._alert_on_incomplete_results([result]) == [] and slack_calls == []


def test_cli_command_budget_flag_claude_only():
    cmd = glr._cli_command("claude", "p", Path("/loop"), "/wt", max_budget_usd=2.0)
    assert cmd[cmd.index("--max-budget-usd") + 1] == "2.00"
    assert "--max-budget-usd" not in glr._cli_command("claude", "p", Path("/loop"), "/wt")
    assert "--max-budget-usd" not in glr._cli_command("codex", "p", Path("/loop"), "/wt", max_budget_usd=2.0)


def test_gitlab_second_iteration_gets_remaining_budget(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(glr, "invoke_batch_issue_agent",
                        lambda *a, **k: seen.append(k["max_budget_usd"]) or {"cost_usd": 2.0})
    monkeypatch.setattr(glr, "build_verifiers",
                        lambda *a, **k: [SequenceVerifier(iter([False, True]))])
    glr._run_one_issue("run", "web", 7, definition(tmp_path, mode="gate"), tmp_path, tmp_path,
                       agent_invoker=glr.invoke_batch_issue_agent, events_dir=tmp_path)
    assert seen == [3.0, 1.0]


def test_remaining_budget_floor():
    assert glr.cost_module.remaining_budget(3, 2.99) == 0.05
    assert glr.cost_module.remaining_budget(None, 1) is None


def _budget_envelope_setup(tmp_path, monkeypatch, exit_code):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    env = {"is_error": True, "subtype": "error_max_budget_usd", "total_cost_usd": 1.25}
    script.write_text(f"#!/usr/bin/env python3\nimport sys, json\nprint(json.dumps({env!r}))\nsys.exit({exit_code})\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("LOOP_RUN_ID", "run_20260907_180000")
    monkeypatch.setattr(glr.ai_cli_config, "get_selected_cli", lambda: "claude")
    monkeypatch.setattr(glr.loop_config, "get_worktree_root", lambda: str(tmp_path))


@pytest.mark.parametrize("exit_code,exc_type", [(1, subprocess.CalledProcessError), (0, glr.AgentCallError)])
def test_budget_exceeded_envelope_is_a_failure_with_cost(tmp_path, monkeypatch, exit_code, exc_type):
    _budget_envelope_setup(tmp_path, monkeypatch, exit_code)
    events_dir = tmp_path / "events"
    with pytest.raises(exc_type) as info:
        glr._invoke_cli_with_prompt("p", repo_root=REPO_ROOT, unified_log_path=tmp_path / "u.log",
                                    alias="harbor", issue_iid=1, events_dir=events_dir, max_budget_usd=1.0)
    assert info.value.cost_usd == 1.25
    failures = [e for e in _read_events(events_dir) if e["event_type"] == "issue.agent_failed"]
    assert [f["data"]["reason"] for f in failures] == ["budget_exceeded"]


def _failing_then_ok_run(monkeypatch, tmp_path, fail_cost):
    seen = []
    def invoker(*a, **k):
        seen.append(k["max_budget_usd"])
        if len(seen) == 1:
            exc = RuntimeError("boom")
            if fail_cost is not None:
                exc.cost_usd = fail_cost
            raise exc
        return {"cost_usd": 0.1}
    monkeypatch.setattr(glr, "build_verifiers", lambda *a, **k: [SequenceVerifier(iter([True]))])
    result = glr._run_one_issue("run", "web", 7, definition(tmp_path, mode="gate"), tmp_path, tmp_path,
                                agent_invoker=invoker, events_dir=tmp_path)
    return seen, result


def test_failed_call_with_cost_is_recorded(monkeypatch, tmp_path):
    # LoopRuntime stops on agent_failed (no retry), so the effect is on the recorded cost.
    seen, result = _failing_then_ok_run(monkeypatch, tmp_path, 2.0)
    assert seen == [3.0]
    assert getattr(result, glr._AGENT_COST_ATTR) == 2.0


def test_failed_call_with_unknown_cost_records_none_not_the_cap(monkeypatch, tmp_path):
    seen, result = _failing_then_ok_run(monkeypatch, tmp_path, None)
    assert getattr(result, glr._AGENT_COST_ATTR) is None


def _stub_cli(tmp_path, monkeypatch, body, exit_code):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "claude"
    script.write_text(f"#!/usr/bin/env python3\nimport sys, json\n{body}\nsys.exit({exit_code})\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(glr.ai_cli_config, "get_selected_cli", lambda: "claude")
    monkeypatch.setattr(glr.loop_config, "get_worktree_root", lambda: str(tmp_path))


def _invoke_failing(tmp_path, cap=1.5):
    with pytest.raises(Exception) as info:
        glr._invoke_cli_with_prompt("p", repo_root=REPO_ROOT, unified_log_path=tmp_path / "u.log",
                                    alias="harbor", issue_iid=1, events_dir=tmp_path / "e", max_budget_usd=cap)
    return info.value


def test_budget_stop_without_cost_records_the_cap(tmp_path, monkeypatch):
    _stub_cli(tmp_path, monkeypatch,
              "print(json.dumps({'is_error': True, 'subtype': 'error_max_budget_usd'}))", 1)
    assert _invoke_failing(tmp_path).cost_usd == 1.5


def test_non_budget_failure_without_envelope_records_unknown(tmp_path, monkeypatch):
    _stub_cli(tmp_path, monkeypatch, "print('plain crash', file=sys.stderr)", 3)
    assert _invoke_failing(tmp_path).cost_usd is None


def test_non_budget_envelope_without_cost_records_unknown(tmp_path, monkeypatch):
    _stub_cli(tmp_path, monkeypatch, "print(json.dumps({'is_error': True, 'subtype': 'error_during_execution'}))", 0)
    assert _invoke_failing(tmp_path).cost_usd is None


def test_offline_mode_hard_denies_gitlab_slack_and_dashboard_tools(tmp_path):
    cmd = glr._cli_command("claude", "p", tmp_path, "/wt", gate=True, offline=True)
    disallowed = cmd[cmd.index("--disallowedTools") + 1]
    for tool in ("gitlab_api.py", "track_new_comments.py", "slack_notify.py", "dashboard_server.py"):
        assert f"Bash(python3 *{tool}*)" in disallowed, tool
    assert "open_merge_request.sh" in disallowed and "Bash(git merge*)" in disallowed
    online = glr._cli_command("claude", "p", tmp_path, "/wt", gate=True)
    assert "slack_notify.py" not in online[online.index("--disallowedTools") + 1]


def test_invoke_issue_file_agent_is_gated_offline_and_capped(monkeypatch, tmp_path):
    seen_args = []
    monkeypatch.setattr(glr, "_run_build_run_prompt", lambda args, repo_root: seen_args.append(args) or "ISSUE PROMPT")
    captured = {}
    monkeypatch.setattr(glr, "_invoke_cli_with_prompt",
                        lambda prompt, **kw: captured.update(prompt=prompt, **kw) or {"cost_usd": 0.4})
    issue_file = tmp_path / "issue.json"
    out = glr.invoke_issue_file_agent("golden", 1, issue_file, repo_root=tmp_path, run_id="golden-x",
                                      feedback="FEEDBACK", max_budget_usd=1.5,
                                      unified_log_path=tmp_path / "u.log")
    assert out == {"cost_usd": 0.4}
    assert seen_args == [["--issue-file", "golden", "1", str(issue_file)]]
    assert captured["prompt"].startswith("ISSUE PROMPT")
    assert "Harness gate is ON" in captured["prompt"]
    assert captured["prompt"].index("FEEDBACK") < captured["prompt"].index("Harness gate is ON")
    assert captured["gate"] is True and captured["offline"] is True
    assert captured["max_budget_usd"] == 1.5
    assert captured["unified_log_path"] == tmp_path / "u.log"
    assert captured["env"]["LOOP_HANDOFF_PATH"].endswith("outputs/handoffs/golden-x/golden-1.json")


def test_offline_allowlist_names_only_the_scripts_the_offline_prompt_uses(tmp_path):
    allowed = glr._allowed_tools(tmp_path, gate=True, offline=True)
    # No directory globs: notify.py, loopkit.py, *_runner.py and
    # list_assigned_issues.py would all be reachable through bin/*.py.
    for glob in ("bin/*.py", "bin/web/*.py", "bin/loop_plugins/*.py", "gitlab_api.py", "gitlab_cache.py"):
        assert glob not in allowed, glob
    for script in ("loop_config.py", "events.py", "risk.py", "memory_store.py"):
        assert f"Bash(python3 bin/{script}*)" in allowed, script
        assert f"Bash(python3 {tmp_path}/bin/{script}*)" in allowed, script
    assert f"Bash(python3 {tmp_path}/bin/project_memory.py get*)" in allowed
    for forbidden in ("notify.py", "loopkit.py", "gitlab_loop_runner.py", "list_assigned_issues.py",
                      "slack_notify.py", "dashboard_server.py", "track_new_comments.py", "open_merge_request.sh"):
        assert forbidden not in allowed, forbidden
    # The fixtures' own checks, the worktree script and the handoff write stay.
    assert "Bash(python3 -m pytest*)" in allowed
    assert f"Bash(bash {tmp_path}/bin/scripts/new_worktree.sh*)" in allowed
    assert f"Write({tmp_path}/outputs/handoffs/**)" in allowed
    # The online allowlist is unchanged.
    assert glr._allowed_tools(tmp_path, gate=True) == glr._allowed_tools(tmp_path, gate=True, offline=False)
    assert "Bash(python3 bin/*.py*)" in glr._allowed_tools(tmp_path, gate=True)


def test_offline_cli_command_uses_the_offline_allowlist(tmp_path):
    cmd = glr._cli_command("claude", "p", tmp_path, "/wt", gate=True, offline=True)
    assert cmd[cmd.index("--allowedTools") + 1] == glr._allowed_tools(tmp_path, gate=True, offline=True)
