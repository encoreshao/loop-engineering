import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin" / "web"))
import loop_scheduler as sched

GITLAB_LOOP = {
    "name": "gitlab-issue-loop",
    "entry_point": "bin.gitlab_loop_runner",
    "schedule": {"weekdays": [1, 2, 3, 4, 5], "hour": 10, "minute": 0},
    "timeout_seconds": 21600,
    "log_suffix": "",
    "emit_run_events": True,
}
TOPIC_LOOP = {
    "name": "topic-monitor",
    "entry_point": "bin.topic_monitor_runner",
    "schedule": {"weekdays": "all", "hour": 10, "minute": 0},
    "timeout_seconds": 21600,
    "log_suffix": "-topic-monitor",
    "emit_run_events": False,
}


def test_is_due_true_when_weekday_and_time_reached_and_not_attempted_today():
    now = datetime(2026, 9, 14, 10, 5)  # Monday
    assert sched.is_due(GITLAB_LOOP, {}, now=now) is True


def test_is_due_false_before_scheduled_time():
    now = datetime(2026, 9, 14, 9, 59)  # Monday, before 10:00
    assert sched.is_due(GITLAB_LOOP, {}, now=now) is False


def test_is_due_false_on_non_scheduled_weekday():
    now = datetime(2026, 9, 12, 10, 5)  # Saturday - not in [1,2,3,4,5]
    assert sched.is_due(GITLAB_LOOP, {}, now=now) is False


def test_is_due_true_every_weekday_for_all_schedule():
    now = datetime(2026, 9, 12, 10, 5)  # Saturday
    assert sched.is_due(TOPIC_LOOP, {}, now=now) is True


def test_is_due_false_when_already_attempted_today():
    now = datetime(2026, 9, 14, 10, 5)
    state = {"gitlab-issue-loop": {"last_attempted_date": "2026-09-14"}}
    assert sched.is_due(GITLAB_LOOP, state, now=now) is False


def test_is_due_true_when_last_attempt_was_yesterday():
    now = datetime(2026, 9, 14, 10, 5)
    state = {"gitlab-issue-loop": {"last_attempted_date": "2026-09-13"}}
    assert sched.is_due(GITLAB_LOOP, state, now=now) is True


def test_run_due_loops_invokes_run_loop_now_for_each_due_loop(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sched.dashboard_server, "read_status", lambda path: {"state": "idle"})

    attempted = sched.run_due_loops(
        loops=[GITLAB_LOOP, TOPIC_LOOP], state_path=tmp_path / "state.json",
        run_loop_now_path=tmp_path / "run-loop-now.sh",
        now=datetime(2026, 9, 14, 10, 5), runner=lambda args, **kw: calls.append(args),
    )

    assert attempted == ["gitlab-issue-loop", "topic-monitor"]
    assert calls == [
        ["bash", str(tmp_path / "run-loop-now.sh"), "gitlab-issue-loop"],
        ["bash", str(tmp_path / "run-loop-now.sh"), "topic-monitor"],
    ]


def test_run_due_loops_skips_a_loop_that_is_already_running(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        sched.dashboard_server, "read_status",
        lambda path: {"state": "running"},
    )

    attempted = sched.run_due_loops(
        loops=[GITLAB_LOOP], state_path=tmp_path / "state.json",
        run_loop_now_path=tmp_path / "run-loop-now.sh",
        now=datetime(2026, 9, 14, 10, 5), runner=lambda args, **kw: calls.append(args),
    )

    assert attempted == []
    assert calls == []


def test_run_due_loops_records_state_even_when_runner_raises(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(sched.dashboard_server, "read_status", lambda path: {"state": "idle"})

    def failing_runner(args, **kwargs):
        raise RuntimeError("boom")

    attempted = sched.run_due_loops(
        loops=[GITLAB_LOOP], state_path=state_path,
        run_loop_now_path=tmp_path / "run-loop-now.sh",
        now=datetime(2026, 9, 14, 10, 5), runner=failing_runner,
    )

    assert attempted == ["gitlab-issue-loop"]
    state = sched._read_state(state_path)
    assert state["gitlab-issue-loop"]["last_attempted_date"] == "2026-09-14"


def test_run_due_loops_a_crashing_loop_does_not_block_the_next_one(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sched.dashboard_server, "read_status", lambda path: {"state": "idle"})

    def runner(args, **kwargs):
        calls.append(args)
        if "gitlab-issue-loop" in args:
            raise RuntimeError("boom")

    attempted = sched.run_due_loops(
        loops=[GITLAB_LOOP, TOPIC_LOOP], state_path=tmp_path / "state.json",
        run_loop_now_path=tmp_path / "run-loop-now.sh",
        now=datetime(2026, 9, 14, 10, 5), runner=runner,
    )

    assert attempted == ["gitlab-issue-loop", "topic-monitor"]
    assert len(calls) == 2


def test_run_due_loops_skips_loops_that_are_not_due(tmp_path, monkeypatch):
    monkeypatch.setattr(sched.dashboard_server, "read_status", lambda path: {"state": "idle"})

    attempted = sched.run_due_loops(
        loops=[GITLAB_LOOP], state_path=tmp_path / "state.json",
        run_loop_now_path=tmp_path / "run-loop-now.sh",
        now=datetime(2026, 9, 14, 9, 0), runner=lambda args, **kw: None,
    )

    assert attempted == []


def test_run_due_loops_a_state_write_failure_does_not_block_the_next_loop(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sched.dashboard_server, "read_status", lambda path: {"state": "idle"})
    original_write_state = sched._write_state

    def failing_write_state(state, state_path=None):
        if "gitlab-issue-loop" in state:
            raise OSError("disk full")
        return original_write_state(state, state_path)

    monkeypatch.setattr(sched, "_write_state", failing_write_state)

    attempted = sched.run_due_loops(
        loops=[GITLAB_LOOP, TOPIC_LOOP], state_path=tmp_path / "state.json",
        run_loop_now_path=tmp_path / "run-loop-now.sh",
        now=datetime(2026, 9, 14, 10, 5), runner=lambda args, **kw: calls.append(args),
    )

    assert attempted == ["gitlab-issue-loop", "topic-monitor"]
    assert len(calls) == 2
