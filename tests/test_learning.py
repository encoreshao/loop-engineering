import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import learning
import events

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(args, env=None):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "learning.py"), *args],
        capture_output=True, text=True,
        env=env if env is not None else {**os.environ},
    )


def _memory_created(lesson_id, category, project, ts="2026-09-04T09:00:00.000Z"):
    return {
        "event_type": "memory.created", "project": project, "timestamp": ts,
        "data": {"lesson_id": lesson_id, "category": category},
    }


def _memory_reused(lesson_id, issue_run_id, project, ts="2026-09-04T10:00:00.000Z"):
    return {
        "event_type": "memory.reused", "issue_run_id": issue_run_id, "project": project,
        "timestamp": ts, "data": {"lesson_id": lesson_id},
    }


def _issue_started(issue_run_id, project, ts="2026-09-04T10:00:00.000Z"):
    return {"event_type": "issue.started", "issue_run_id": issue_run_id, "project": project, "timestamp": ts}


def _issue_completed(issue_run_id, project, ts="2026-09-04T10:05:00.000Z"):
    return {"event_type": "issue.completed", "issue_run_id": issue_run_id, "project": project, "timestamp": ts}


def _issue_escalated(issue_run_id, project, ts="2026-09-04T10:05:00.000Z"):
    return {"event_type": "issue.escalated", "issue_run_id": issue_run_id, "project": project, "timestamp": ts}


def test_compute_lesson_effectiveness_counts_success_and_failure():
    events_list = [
        _memory_created("lesson_1", "testing", "kurrant"),
        _memory_reused("lesson_1", "r1_kurrant_1", "kurrant"),
        _issue_completed("r1_kurrant_1", "kurrant"),
        _memory_reused("lesson_1", "r1_kurrant_2", "kurrant"),
        _issue_escalated("r1_kurrant_2", "kurrant"),
    ]

    result = learning.compute_lesson_effectiveness(events_list)

    assert len(result) == 1
    entry = result[0]
    assert entry["lesson_id"] == "lesson_1"
    assert entry["category"] == "testing"
    assert entry["times_reused"] == 2
    assert entry["successful_reuses"] == 1
    assert entry["failed_reuses"] == 1
    assert entry["effectiveness_rate"] == 0.5


def test_compute_lesson_effectiveness_pending_reuse_counts_toward_times_reused_only():
    events_list = [
        _memory_created("lesson_1", "testing", "kurrant"),
        _memory_reused("lesson_1", "r1_kurrant_1", "kurrant"),  # no terminal outcome event
    ]

    result = learning.compute_lesson_effectiveness(events_list)

    entry = result[0]
    assert entry["times_reused"] == 1
    assert entry["successful_reuses"] == 0
    assert entry["failed_reuses"] == 0
    assert entry["effectiveness_rate"] is None


def test_compute_lesson_effectiveness_unknown_category_when_no_matching_created_event():
    events_list = [_memory_reused("lesson_1", "r1_kurrant_1", "kurrant"), _issue_completed("r1_kurrant_1", "kurrant")]

    result = learning.compute_lesson_effectiveness(events_list)

    assert result[0]["category"] is None


def test_compute_lesson_effectiveness_filters_by_project():
    events_list = [
        _memory_created("lesson_1", "testing", "alpha"),
        _memory_reused("lesson_1", "r1_alpha_1", "alpha"),
        _issue_completed("r1_alpha_1", "alpha"),
        _memory_created("lesson_2", "auth", "beta"),
        _memory_reused("lesson_2", "r1_beta_1", "beta"),
        _issue_completed("r1_beta_1", "beta"),
    ]

    result = learning.compute_lesson_effectiveness(events_list, project="alpha")

    assert len(result) == 1
    assert result[0]["lesson_id"] == "lesson_1"


def test_compute_lesson_effectiveness_empty_events_returns_empty_list():
    assert learning.compute_lesson_effectiveness([]) == []


def test_compute_reuse_metrics_arithmetic():
    events_list = [
        _memory_created("lesson_1", "testing", "kurrant"),
        _issue_started("r1_kurrant_1", "kurrant"),
        _memory_reused("lesson_1", "r1_kurrant_1", "kurrant"),
        _issue_completed("r1_kurrant_1", "kurrant"),
        _issue_started("r1_kurrant_2", "kurrant"),  # no reuse on this one
        _issue_completed("r1_kurrant_2", "kurrant"),
    ]

    result = learning.compute_reuse_metrics(events_list)

    assert result["lessons_created"] == 1
    assert result["total_reuses"] == 1
    assert result["memory_reuse_rate"] == 0.5  # 1 of 2 issues cited a lesson
    assert result["memory_success_rate"] == 1.0  # the one reuse succeeded
    assert result["failures_prevented"] is None
    assert result["failures_prevented_reason"] == learning.FAILURES_PREVENTED_REASON


def test_compute_reuse_metrics_zero_issues_processed_returns_none_reuse_rate():
    result = learning.compute_reuse_metrics([])

    assert result["memory_reuse_rate"] is None
    assert result["memory_success_rate"] is None
    assert result["lessons_created"] == 0
    assert result["total_reuses"] == 0


def test_compute_reuse_metrics_missing_timestamp_issue_started_not_counted():
    events_list = [{"event_type": "issue.started", "issue_run_id": "r1_kurrant_1", "project": "kurrant"}]  # no timestamp

    result = learning.compute_reuse_metrics(events_list)

    assert result["memory_reuse_rate"] is None


def test_compute_reuse_metrics_filters_by_project():
    events_list = [
        _issue_started("r1_alpha_1", "alpha"),
        _memory_reused("lesson_1", "r1_alpha_1", "alpha"),
        _issue_completed("r1_alpha_1", "alpha"),
        _issue_started("r1_beta_1", "beta"),
    ]

    result = learning.compute_reuse_metrics(events_list, project="alpha")

    assert result["memory_reuse_rate"] == 1.0


def test_build_learning_report_integrates_real_events(tmp_path):
    events.emit("memory.created", run_id="run_1", project="kurrant", data={"lesson_id": "lesson_1", "category": "testing"}, events_dir=tmp_path)
    events.emit("issue.started", run_id="run_1", issue_run_id="run_1_kurrant_1", project="kurrant", events_dir=tmp_path)
    events.emit("memory.reused", run_id="run_1", issue_run_id="run_1_kurrant_1", project="kurrant", data={"lesson_id": "lesson_1"}, events_dir=tmp_path)
    events.emit("issue.completed", run_id="run_1", issue_run_id="run_1_kurrant_1", project="kurrant", events_dir=tmp_path)

    report = learning.build_learning_report(events_dir=tmp_path)

    assert report["reuse"]["lessons_created"] == 1
    assert report["reuse"]["total_reuses"] == 1
    assert report["lessons"][0]["lesson_id"] == "lesson_1"
    assert report["lessons"][0]["successful_reuses"] == 1


def test_format_learning_report_shows_na_for_failures_prevented(tmp_path):
    report = learning.build_learning_report(events_dir=tmp_path)

    text = learning.format_learning_report(report)

    assert "N/A" in text
    assert learning.FAILURES_PREVENTED_REASON in text


def test_cli_report_prints_formatted_output(tmp_path):
    result = _run_cli(["--events-dir", str(tmp_path)])

    assert result.returncode == 0
    assert "Loop Learning" in result.stdout


def test_cli_report_days_flag_computes_date_range(tmp_path):
    result = _run_cli(["--days", "7", "--events-dir", str(tmp_path)])

    assert result.returncode == 0
    today = datetime.now(timezone.utc).date()
    since = (today - timedelta(days=6)).isoformat()
    assert since in result.stdout
