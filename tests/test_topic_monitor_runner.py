import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import topic_monitor_runner as tmr

from conftest import SANITIZED_PATH

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFINITION_PATH = REPO_ROOT / "loops" / "topic-monitor" / "loop.yaml"


@pytest.fixture(autouse=True)
def _no_real_ai_cli(sanitized_path):
    pass


@pytest.fixture(autouse=True)
def slack_calls(monkeypatch):
    """Safe default for `slack_notify.post_message`, which
    `bin/topic_monitor_runner.py` now calls in-process on any
    non-COMPLETED topic: unstubbed it reads the real ~/.slack/config.json
    and POSTs over the network, so a test that forgets to fake it would
    reach the real Slack - the same shape of accident the `sanitized_path`
    fixture above prevents for the real `claude`/`codex` CLI. Mirrors
    tests/test_gitlab_loop_runner.py's own identical fixture.

    Returns the list of message texts the stub recorded, so a test can
    just request `slack_calls` to inspect them."""
    calls = []
    monkeypatch.setattr(tmr.slack_notify, "post_message", lambda text, **kwargs: calls.append(text))
    return calls


@pytest.fixture(autouse=True)
def _no_real_topic_status_writes(monkeypatch, tmp_path):
    """`_mark_topic_failed` writes the REAL
    outputs/topic-monitor/status.json when no `status_path` is threaded
    through, which would both pollute this checkout and (since
    trigger_topic_monitor_run refuses to start while any topic reads
    "running") mutate live dashboard state. Default every test's write to
    a scratch file; the tests that care about the write pass their own
    `status_path` explicitly, which overrides this."""
    monkeypatch.setattr(
        tmr.dashboard_server, "TOPIC_MONITOR_STATUS_PATH", tmp_path / "default-status.json"
    )


def test_the_modules_safety_net_stubs_both_the_ai_cli_and_slack(slack_calls):
    """Guards the safety net itself: a test that forgets to fake Slack must
    hit the recording stub rather than the real webhook."""
    assert os.environ["PATH"] == SANITIZED_PATH
    assert tmr._notify_slack_best_effort("a message nobody should receive") is True
    assert slack_calls == ["a message nobody should receive"]


def _write_fake_cli(bin_dir, name, output_text="a briefing", exit_code=0):
    script_path = bin_dir / name
    script_path.write_text(
        f"#!/usr/bin/env python3\nimport sys\nprint({output_text!r}, end='')\nsys.exit({exit_code})\n"
    )
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)


def test_invoke_topic_agent_succeeds_against_a_fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_cli(bin_dir, "claude")

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(
        tmr, "build_prompt",
        lambda name=None, repo_root=None: "a prompt",
    )
    monkeypatch.setattr(tmr.ai_cli_config, "get_selected_cli", lambda: "claude")

    unified_log = tmp_path / "unified.log"
    result = tmr.invoke_topic_agent("ai-news", repo_root=REPO_ROOT, unified_log_path=unified_log)

    assert result == {"changed": True, "cost_usd": None}
    assert "a briefing" in unified_log.read_text()


def test_invoke_topic_agent_raises_on_nonzero_exit(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_cli(bin_dir, "claude", exit_code=1)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(
        tmr, "build_prompt",
        lambda name=None, repo_root=None: "a prompt",
    )
    monkeypatch.setattr(tmr.ai_cli_config, "get_selected_cli", lambda: "claude")

    try:
        tmr.invoke_topic_agent("ai-news", repo_root=REPO_ROOT, unified_log_path=tmp_path / "unified.log")
        assert False, "expected CalledProcessError"
    except subprocess.CalledProcessError:
        pass


def test_allowed_tools_scopes_writes_to_outputs_topic_monitor():
    allowed = tmr._allowed_tools(REPO_ROOT)
    assert "Read(**/outputs/topic-monitor/**)" in allowed
    assert "Edit(**/outputs/topic-monitor/**)" in allowed
    assert "WebSearch" in allowed
    assert "WebFetch" in allowed


def test_disallowed_tools_blocks_git_and_denies_writes_outside_topic_monitor():
    disallowed = tmr._disallowed_tools()
    assert "Bash(git*)" in disallowed
    assert "Edit(**/bin/**)" in disallowed
    assert "Edit(**/launchd/**)" in disallowed
    assert "Read(**/.env*)" in disallowed


def test_disallowed_tools_denies_the_installed_launchagents_plist():
    """The one non-cwd-anchored rule in the deny list, and the only thing
    standing between a prompt injection from fetched web content and
    rewriting the installed GitLab loop's own launchd schedule. It must be
    built from the real, expanded home directory (the `//`-prefixed
    absolute form bash's own double-quoted DISALLOWED_TOOLS produced) -
    not the literal string "$HOME", which no shell here ever expands and
    which would match nothing on disk."""
    disallowed = tmr._disallowed_tools(home="/Users/fakehome")
    assert "Edit(//Users/fakehome/Library/LaunchAgents/**)" in disallowed
    assert "Edit(/$HOME/Library/LaunchAgents/**)" not in disallowed


def test_run_all_topics_writes_one_result_per_topic(tmp_path, monkeypatch):
    calls = []

    def fake_invoke(name, repo_root=None, timeout_seconds=1800, unified_log_path=None):
        calls.append(name)
        return {"changed": True, "cost_usd": None}

    monkeypatch.setattr(tmr, "invoke_topic_agent", fake_invoke)

    results_dir = tmp_path / "loop-runs"
    results = tmr.run_all_topics(
        "run_20260907_100000", results_dir=results_dir,
        definition_path=REPO_ROOT / "loops" / "topic-monitor" / "loop.yaml",
        names=["ai-news", "ai-coding-tools"],
        events_dir=tmp_path / "events",
    )

    assert calls == ["ai-news", "ai-coding-tools"]
    assert [r.final_state.value for r in results] == ["completed", "completed"]
    assert sorted(p.name for p in results_dir.iterdir()) == [
        "run_20260907_100000_ai-coding-tools",
        "run_20260907_100000_ai-news",
    ]


def test_run_all_topics_continues_after_one_topic_fails(tmp_path, monkeypatch):
    def fake_invoke(name, repo_root=None, timeout_seconds=1800, unified_log_path=None):
        if name == "ai-news":
            raise RuntimeError("agent crashed")
        return {"changed": True, "cost_usd": None}

    monkeypatch.setattr(tmr, "invoke_topic_agent", fake_invoke)

    results_dir = tmp_path / "loop-runs"
    results = tmr.run_all_topics(
        "run_20260907_110000", results_dir=results_dir,
        definition_path=REPO_ROOT / "loops" / "topic-monitor" / "loop.yaml",
        names=["ai-news", "ai-coding-tools"],
        events_dir=tmp_path / "events",
    )

    assert [r.final_state.value for r in results] == ["failed", "completed"]


def test_run_all_topics_marks_a_crashed_topic_failed_via_write_topic_status(tmp_path, monkeypatch):
    """outputs/topic-monitor/status.json is single, shared, cross-topic
    state, and the agent's own step 8 is what normally moves a topic off
    "running". A per-topic timeout kills the CLI before that step, so the
    runner has to write the terminal status itself or the topic latches at
    "running" forever - permanently disabling the dashboard's Run now
    button (trigger_topic_monitor_run refuses while any topic is running),
    with no reaper anywhere to clear it."""
    writes = []
    monkeypatch.setattr(
        tmr.dashboard_server, "write_topic_status",
        lambda topic_name, state, status_path=None, **extra: writes.append((topic_name, state)),
    )

    def fake_invoke(name, repo_root=None, timeout_seconds=1800, unified_log_path=None):
        if name == "ai-news":
            raise RuntimeError("agent crashed")
        return {"changed": True, "cost_usd": None}

    monkeypatch.setattr(tmr, "invoke_topic_agent", fake_invoke)

    tmr.run_all_topics(
        "run_20260907_130000", results_dir=tmp_path / "loop-runs",
        definition_path=DEFINITION_PATH, names=["ai-news", "ai-coding-tools"],
        events_dir=tmp_path / "events",
    )

    # Only the failed topic - the completed one wrote its own `idle` from
    # inside its own agent session, which must not be overwritten here.
    assert writes == [("ai-news", "failed")]


def test_run_all_topics_failed_status_lands_in_the_real_status_json(tmp_path, monkeypatch):
    """The same fix driven end-to-end through the real
    dashboard_server.write_topic_status against a scratch status.json,
    including its "leave every other topic's entry untouched" contract."""
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({"topics": {"ai-coding-tools": {"state": "idle"}}}))

    def fake_invoke(name, repo_root=None, timeout_seconds=1800, unified_log_path=None):
        raise RuntimeError("agent crashed")

    monkeypatch.setattr(tmr, "invoke_topic_agent", fake_invoke)

    tmr.run_all_topics(
        "run_20260907_133000", results_dir=tmp_path / "loop-runs",
        definition_path=DEFINITION_PATH, names=["ai-news"],
        events_dir=tmp_path / "events", status_path=status_path,
    )

    topics = json.loads(status_path.read_text())["topics"]
    assert topics["ai-news"]["state"] == "failed"
    assert topics["ai-coding-tools"]["state"] == "idle"


def test_run_all_topics_does_not_touch_status_when_every_topic_completes(tmp_path, monkeypatch):
    writes = []
    monkeypatch.setattr(
        tmr.dashboard_server, "write_topic_status",
        lambda topic_name, state, status_path=None, **extra: writes.append((topic_name, state)),
    )
    monkeypatch.setattr(
        tmr, "invoke_topic_agent",
        lambda name, repo_root=None, timeout_seconds=1800, unified_log_path=None: {
            "changed": True, "cost_usd": None,
        },
    )

    tmr.run_all_topics(
        "run_20260907_134500", results_dir=tmp_path / "loop-runs",
        definition_path=DEFINITION_PATH, names=["ai-news"],
        events_dir=tmp_path / "events",
    )

    assert writes == []


def test_main_alerts_slack_when_a_topic_does_not_complete(tmp_path, monkeypatch):
    """LoopRuntime catches the agent exception, run_all_topics returns
    normally and main still exits 0, so run-topic-monitor-loop.sh's ERR
    trap never fires - the failure has to announce itself from here."""
    sent = []
    monkeypatch.setattr(tmr.slack_notify, "post_message", lambda text, **kwargs: sent.append(text))

    def fake_invoke(name, repo_root=None, timeout_seconds=1800, unified_log_path=None):
        if name == "ai-news":
            raise RuntimeError("agent crashed")
        return {"changed": True, "cost_usd": None}

    monkeypatch.setattr(tmr, "invoke_topic_agent", fake_invoke)

    exit_code = tmr.main_with_argv(
        ["run_20260907_140000"], results_dir=tmp_path / "loop-runs",
        definition_path=DEFINITION_PATH, names=["ai-news", "ai-coding-tools"],
        events_dir=tmp_path / "events", status_path=tmp_path / "status.json",
    )

    # Still 0: each topic's failure is contained and now announced - a
    # non-zero exit would trip the outer script's ERR trap and report the
    # WHOLE run as failed, which it wasn't.
    assert exit_code == 0
    assert len(sent) == 1
    assert "run_20260907_140000_ai-news" in sent[0]
    assert "failed" in sent[0]
    assert "run_20260907_140000_ai-coding-tools" not in sent[0]


def test_main_does_not_alert_slack_when_every_topic_completes(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(tmr.slack_notify, "post_message", lambda text, **kwargs: sent.append(text))
    monkeypatch.setattr(
        tmr, "invoke_topic_agent",
        lambda name, repo_root=None, timeout_seconds=1800, unified_log_path=None: {
            "changed": True, "cost_usd": None,
        },
    )

    exit_code = tmr.main_with_argv(
        ["run_20260907_141500"], results_dir=tmp_path / "loop-runs",
        definition_path=DEFINITION_PATH, names=["ai-news"],
        events_dir=tmp_path / "events", status_path=tmp_path / "status.json",
    )

    assert exit_code == 0
    assert sent == []


def test_main_calls_run_all_topics_with_the_given_run_id(monkeypatch):
    captured = {}

    def fake_run_all_topics(run_id, **kwargs):
        captured["run_id"] = run_id
        return []

    monkeypatch.setattr(tmr, "run_all_topics", fake_run_all_topics)

    exit_code = tmr.main_with_argv(["run_20260907_120000"])

    assert exit_code == 0
    assert captured["run_id"] == "run_20260907_120000"


def test_main_rejects_wrong_argv_length(monkeypatch):
    calls = []
    monkeypatch.setattr(tmr, "run_all_topics", lambda run_id, **kwargs: calls.append(run_id))

    assert tmr.main_with_argv([]) == 2
    assert tmr.main_with_argv(["a", "b"]) == 2
    assert calls == []
