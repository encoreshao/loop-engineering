import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import metrics
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import events

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(args, env=None):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "metrics.py"), *args],
        capture_output=True,
        text=True,
        env=env if env is not None else {**os.environ},
    )


def _run_started(run_id, ts):
    return {"event_type": "run.started", "run_id": run_id, "timestamp": ts}


def _run_completed(run_id, ts):
    return {"event_type": "run.completed", "run_id": run_id, "timestamp": ts}


def _run_failed(run_id, ts):
    return {"event_type": "run.failed", "run_id": run_id, "timestamp": ts}


def test_compute_run_metrics_counts_and_average_duration():
    events = [
        _run_started("run_1", "2026-09-04T10:00:00.000Z"),
        _run_completed("run_1", "2026-09-04T10:01:00.000Z"),  # 60000ms
        _run_started("run_2", "2026-09-04T11:00:00.000Z"),
        _run_completed("run_2", "2026-09-04T11:02:00.000Z"),  # 120000ms
        _run_started("run_3", "2026-09-04T12:00:00.000Z"),
        _run_failed("run_3", "2026-09-04T12:00:30.000Z"),
    ]

    result = metrics.compute_run_metrics(events)

    assert result["runs_total"] == 3
    assert result["runs_success"] == 2
    assert result["runs_failed"] == 1
    assert result["average_run_duration_ms"] == 90000.0  # mean(60000, 120000)


def test_compute_run_metrics_zero_runs_returns_none_average():
    result = metrics.compute_run_metrics([])

    assert result["runs_total"] == 0
    assert result["runs_success"] == 0
    assert result["runs_failed"] == 0
    assert result["average_run_duration_ms"] is None


def test_compute_run_metrics_missing_timestamp_does_not_raise():
    events = [
        {"event_type": "run.started", "run_id": "run_1"},  # no "timestamp" key
    ]

    result = metrics.compute_run_metrics(events)

    assert result["runs_total"] == 0
    assert result["average_run_duration_ms"] is None


def _issue_started(issue_run_id, project, ts):
    return {"event_type": "issue.started", "issue_run_id": issue_run_id, "project": project, "timestamp": ts}


def _issue_completed(issue_run_id, project, ts):
    return {"event_type": "issue.completed", "issue_run_id": issue_run_id, "project": project, "timestamp": ts}


def _issue_escalated(issue_run_id, project, ts):
    return {"event_type": "issue.escalated", "issue_run_id": issue_run_id, "project": project, "timestamp": ts}


def test_compute_issue_metrics_counts_and_average_duration():
    events = [
        _issue_started("r1_p_1", "kurrant", "2026-09-04T10:00:00.000Z"),
        _issue_completed("r1_p_1", "kurrant", "2026-09-04T10:03:00.000Z"),  # 180000ms
        _issue_started("r1_p_2", "kurrant", "2026-09-04T11:00:00.000Z"),
        _issue_escalated("r1_p_2", "kurrant", "2026-09-04T11:01:00.000Z"),  # 60000ms
    ]

    result = metrics.compute_issue_metrics(events)

    assert result["issues_processed"] == 2
    assert result["issues_completed"] == 1
    assert result["issues_escalated"] == 1
    assert result["issues_failed"] == 0
    assert result["average_issue_duration_ms"] == 120000.0  # mean(180000, 60000)


def test_compute_issue_metrics_filters_by_project():
    events = [
        _issue_started("r1_a_1", "alpha", "2026-09-04T10:00:00.000Z"),
        _issue_completed("r1_a_1", "alpha", "2026-09-04T10:01:00.000Z"),
        _issue_started("r1_b_1", "beta", "2026-09-04T10:00:00.000Z"),
        _issue_completed("r1_b_1", "beta", "2026-09-04T10:05:00.000Z"),
    ]

    result = metrics.compute_issue_metrics(events, project="alpha")

    assert result["issues_processed"] == 1
    assert result["issues_completed"] == 1


def test_compute_issue_metrics_zero_issues_returns_none_average():
    result = metrics.compute_issue_metrics([])

    assert result["issues_processed"] == 0
    assert result["average_issue_duration_ms"] is None


def test_compute_issue_metrics_duplicate_terminal_event_does_not_inflate_count():
    events = [
        _issue_started("r1_p_1", "kurrant", "2026-09-04T10:00:00.000Z"),
        _issue_completed("r1_p_1", "kurrant", "2026-09-04T10:01:00.000Z"),
        _issue_completed("r1_p_1", "kurrant", "2026-09-04T10:02:00.000Z"),  # duplicate
    ]

    result = metrics.compute_issue_metrics(events)

    assert result["issues_processed"] == 1
    assert result["issues_completed"] == 1


def test_compute_issue_metrics_missing_timestamp_does_not_raise():
    events = [
        {"event_type": "issue.started", "issue_run_id": "r1_p_1", "project": "kurrant"},  # no "timestamp"
        _issue_completed("r1_p_1", "kurrant", "2026-09-04T10:01:00.000Z"),
    ]

    result = metrics.compute_issue_metrics(events)

    assert result["issues_processed"] == 0  # started event lacked a timestamp, so it never registered
    assert result["issues_completed"] == 1
    assert result["average_issue_duration_ms"] is None


def _verification_started(issue_run_id, project, ts):
    return {"event_type": "verification.started", "issue_run_id": issue_run_id, "project": project, "timestamp": ts}


def _verification_passed(issue_run_id, project, ts):
    return {"event_type": "verification.passed", "issue_run_id": issue_run_id, "project": project, "timestamp": ts}


def _verification_failed(issue_run_id, project, ts):
    return {"event_type": "verification.failed", "issue_run_id": issue_run_id, "project": project, "timestamp": ts}


def test_compute_verification_metrics_pass_rate_and_duration():
    events = [
        _verification_started("r1_p_1", "kurrant", "2026-09-04T10:00:00.000Z"),
        _verification_passed("r1_p_1", "kurrant", "2026-09-04T10:00:30.000Z"),  # 30000ms
        _verification_started("r1_p_2", "kurrant", "2026-09-04T11:00:00.000Z"),
        _verification_failed("r1_p_2", "kurrant", "2026-09-04T11:00:10.000Z"),  # 10000ms
    ]

    result = metrics.compute_verification_metrics(events)

    assert result["verification_total"] == 2
    assert result["verification_passed"] == 1
    assert result["verification_failed"] == 1
    assert result["verification_pass_rate"] == 0.5
    assert result["average_verification_duration_ms"] == 20000.0  # mean(30000, 10000)


def test_compute_verification_metrics_zero_denominator_returns_none_rate():
    result = metrics.compute_verification_metrics([])

    assert result["verification_pass_rate"] is None
    assert result["average_verification_duration_ms"] is None


def test_compute_verification_metrics_filters_by_project():
    events = [
        _verification_started("r1_a_1", "alpha", "2026-09-04T10:00:00.000Z"),
        _verification_passed("r1_a_1", "alpha", "2026-09-04T10:00:10.000Z"),
        _verification_started("r1_b_1", "beta", "2026-09-04T10:00:00.000Z"),
        _verification_failed("r1_b_1", "beta", "2026-09-04T10:00:10.000Z"),
    ]

    result = metrics.compute_verification_metrics(events, project="alpha")

    assert result["verification_total"] == 1
    assert result["verification_passed"] == 1
    assert result["verification_failed"] == 0
    assert result["verification_pass_rate"] == 1.0


def test_compute_verification_metrics_duplicate_terminal_event_does_not_inflate_count():
    events = [
        _verification_started("r1_p_1", "kurrant", "2026-09-04T10:00:00.000Z"),
        _verification_passed("r1_p_1", "kurrant", "2026-09-04T10:00:10.000Z"),
        _verification_passed("r1_p_1", "kurrant", "2026-09-04T10:00:20.000Z"),  # duplicate
    ]

    result = metrics.compute_verification_metrics(events)

    assert result["verification_total"] == 1
    assert result["verification_passed"] == 1
    assert result["verification_pass_rate"] == 1.0


def test_compute_verification_metrics_missing_timestamp_does_not_raise():
    events = [
        {"event_type": "verification.started", "issue_run_id": "r1_p_1", "project": "kurrant"},  # no "timestamp"
        _verification_passed("r1_p_1", "kurrant", "2026-09-04T10:00:10.000Z"),
    ]

    result = metrics.compute_verification_metrics(events)

    assert result["verification_passed"] == 1
    assert result["average_verification_duration_ms"] is None


def test_compute_quality_and_autonomy_metrics_happy_path():
    issue_metrics = {"issues_processed": 10, "issues_completed": 7}
    verification_metrics = {"verification_pass_rate": 0.9}

    result = metrics.compute_quality_and_autonomy_metrics(issue_metrics, verification_metrics)

    assert result["resolution_rate"] == 0.7
    assert result["verification_pass_rate"] == 0.9
    assert result["autonomy_rate"] == 0.7
    assert result["autonomy_rate_is_placeholder"] is True
    assert abs(result["human_intervention_rate"] - 0.3) < 1e-9


def test_compute_quality_and_autonomy_metrics_zero_processed_returns_none():
    issue_metrics = {"issues_processed": 0, "issues_completed": 0}
    verification_metrics = {"verification_pass_rate": None}

    result = metrics.compute_quality_and_autonomy_metrics(issue_metrics, verification_metrics)

    assert result["resolution_rate"] is None
    assert result["autonomy_rate"] is None
    assert result["human_intervention_rate"] is None


def test_retry_and_failure_rate_always_none_with_fixed_reason_regardless_of_input():
    # Non-trivial input on purpose: guards against a future edit accidentally
    # wiring these up to a real computation without updating the spec first.
    issue_metrics = {"issues_processed": 100, "issues_completed": 100, "issues_failed": 40}
    verification_metrics = {"verification_pass_rate": 1.0}

    result = metrics.compute_quality_and_autonomy_metrics(issue_metrics, verification_metrics)

    assert result["retry_rate"] is None
    assert result["retry_rate_unavailable_reason"] == metrics.RETRY_RATE_UNAVAILABLE_REASON
    assert result["failure_rate"] is None
    assert result["failure_rate_unavailable_reason"] == metrics.FAILURE_RATE_UNAVAILABLE_REASON


def test_build_report_integrates_real_events(tmp_path):
    events.emit("run.started", run_id="run_1", events_dir=tmp_path)
    events.emit(
        "issue.started", run_id="run_1", issue_run_id="run_1_kurrant_1",
        project="kurrant", issue_iid=1, events_dir=tmp_path,
    )
    events.emit(
        "issue.completed", run_id="run_1", issue_run_id="run_1_kurrant_1",
        project="kurrant", issue_iid=1, events_dir=tmp_path,
    )
    events.emit("run.completed", run_id="run_1", events_dir=tmp_path)

    report = metrics.build_report(events_dir=tmp_path)

    assert report["run"]["runs_total"] == 1
    assert report["issue"]["issues_processed"] == 1
    assert report["issue"]["issues_completed"] == 1
    assert report["quality_and_autonomy"]["resolution_rate"] == 1.0


def test_build_report_project_filter_replaces_run_section(tmp_path):
    events.emit("run.started", run_id="run_1", events_dir=tmp_path)
    events.emit(
        "issue.started", run_id="run_1", issue_run_id="run_1_alpha_1",
        project="alpha", issue_iid=1, events_dir=tmp_path,
    )
    events.emit(
        "issue.started", run_id="run_1", issue_run_id="run_1_beta_1",
        project="beta", issue_iid=1, events_dir=tmp_path,
    )

    report = metrics.build_report(events_dir=tmp_path, project="alpha")

    assert "not_applicable_reason" in report["run"]
    assert "runs_total" not in report["run"]
    assert report["issue"]["issues_processed"] == 1


def test_build_report_date_filtering(tmp_path):
    # Today's file, via real emit().
    events.emit("run.started", run_id="run_today", events_dir=tmp_path)

    # A synthetic older file.
    older_date = "2000-01-01"
    older_event = {
        "schema_version": 1, "event_id": "evt_old", "timestamp": "2000-01-01T00:00:00.000Z",
        "event_type": "run.started", "run_id": "run_older", "issue_run_id": None,
        "project": None, "issue_iid": None, "data": {},
    }
    (tmp_path / f"{older_date}.jsonl").write_text(json.dumps(older_event) + "\n")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report = metrics.build_report(events_dir=tmp_path, since_date=today)

    assert report["run"]["runs_total"] == 1  # only run_today


def test_build_report_missing_timestamp_event_does_not_raise(tmp_path):
    event = {
        "schema_version": 1, "event_id": "evt_1", "event_type": "run.started",
        "run_id": "run_1", "issue_run_id": None, "project": None, "issue_iid": None, "data": {},
    }  # note: no "timestamp" key at all
    (tmp_path / "2026-09-04.jsonl").write_text(json.dumps(event) + "\n")

    report = metrics.build_report(events_dir=tmp_path)

    assert report["run"]["runs_total"] == 0


def test_format_report_renders_na_for_unavailable_metrics(tmp_path):
    report = metrics.build_report(events_dir=tmp_path)  # empty dir - everything zero/None

    text = metrics.format_report(report)

    assert "N/A" in text
    assert metrics.RETRY_RATE_UNAVAILABLE_REASON in text
    assert metrics.FAILURE_RATE_UNAVAILABLE_REASON in text


def test_format_report_header_shows_all_time_by_default(tmp_path):
    report = metrics.build_report(events_dir=tmp_path)

    text = metrics.format_report(report)

    assert text.startswith("Loop Metrics (all time)")


def test_format_report_header_shows_date_range_and_project(tmp_path):
    report = metrics.build_report(
        events_dir=tmp_path, since_date="2026-09-01", until_date="2026-09-04", project="kurrant",
    )

    text = metrics.format_report(report)

    assert "Loop Metrics (2026-09-01 to 2026-09-04)" in text
    assert "project: kurrant" in text


def test_cli_bare_invocation_prints_report(tmp_path):
    events.emit("run.started", run_id="run_1", events_dir=tmp_path)
    events.emit("run.completed", run_id="run_1", events_dir=tmp_path)

    result = _run_cli(["--events-dir", str(tmp_path)])

    assert result.returncode == 0, result.stderr
    assert "Runs" in result.stdout
    assert "Total" in result.stdout


def test_cli_days_flag_filters(tmp_path):
    older_date = "2000-01-01"
    older_event = {
        "schema_version": 1, "event_id": "evt_old", "timestamp": "2000-01-01T00:00:00.000Z",
        "event_type": "run.started", "run_id": "run_older", "issue_run_id": None,
        "project": None, "issue_iid": None, "data": {},
    }
    (tmp_path / f"{older_date}.jsonl").write_text(json.dumps(older_event) + "\n")
    events.emit("run.started", run_id="run_today", events_dir=tmp_path)

    result = _run_cli(["--days", "1", "--events-dir", str(tmp_path)])

    assert result.returncode == 0, result.stderr
    # Whitespace-tolerant: don't assume format_report's exact column padding.
    assert re.search(r"Total\s+1\b", result.stdout), result.stdout


def test_cli_project_flag(tmp_path):
    events.emit(
        "issue.started", run_id="run_1", issue_run_id="run_1_alpha_1",
        project="alpha", issue_iid=1, events_dir=tmp_path,
    )

    result = _run_cli(["--project", "alpha", "--events-dir", str(tmp_path)])

    assert result.returncode == 0, result.stderr
    assert re.search(r"Processed\s+1\b", result.stdout), result.stdout


def test_cli_bad_days_value_fails_clearly(tmp_path):
    result = _run_cli(["--days", "not-a-number", "--events-dir", str(tmp_path)])

    assert result.returncode == 1
    assert "days" in result.stderr.lower()


def test_cli_nonexistent_events_dir_fails_clearly(tmp_path):
    bad_dir = tmp_path / "does-not-exist"

    result = _run_cli(["--events-dir", str(bad_dir)])

    assert result.returncode == 1
    assert "does not exist" in result.stderr.lower()
    assert result.stdout == ""


def test_bucketed_reports_seven_daily_buckets(tmp_path):
    reports = metrics.bucketed_reports(events_dir=tmp_path, days=7, bucket_days=1)

    assert len(reports) == 7
    today = datetime.now(timezone.utc).date()
    expected_dates = [(today - timedelta(days=n)).isoformat() for n in range(6, -1, -1)]
    actual_dates = [r["scope"]["since_date"] for r in reports]
    assert actual_dates == expected_dates
    for r in reports:
        assert r["scope"]["since_date"] == r["scope"]["until_date"]  # 1-day bucket


def test_bucketed_reports_ninety_days_weekly_buckets_first_bucket_is_shorter(tmp_path):
    reports = metrics.bucketed_reports(events_dir=tmp_path, days=90, bucket_days=7)

    assert len(reports) == 13

    first_since = datetime.fromisoformat(reports[0]["scope"]["since_date"]).date()
    first_until = datetime.fromisoformat(reports[0]["scope"]["until_date"]).date()
    assert (first_until - first_since).days == 5  # 6-day span (5 = span - 1)

    for r in reports[1:]:
        since = datetime.fromisoformat(r["scope"]["since_date"]).date()
        until = datetime.fromisoformat(r["scope"]["until_date"]).date()
        assert (until - since).days == 6  # full 7-day span

    today = datetime.now(timezone.utc).date()
    assert reports[-1]["scope"]["until_date"] == today.isoformat()


def test_bucketed_reports_empty_events_dir_still_returns_well_formed_reports(tmp_path):
    reports = metrics.bucketed_reports(events_dir=tmp_path, days=3, bucket_days=1)

    assert len(reports) == 3
    for r in reports:
        assert r["issue"]["issues_processed"] == 0
        assert r["quality_and_autonomy"]["resolution_rate"] is None


def _issue_classified(issue_run_id, project, data, ts="2026-09-04T10:00:00.000Z"):
    return {"event_type": "issue.classified", "issue_run_id": issue_run_id, "project": project, "timestamp": ts, "data": data}


def test_compute_classification_metrics_counts_by_type_complexity_risk_level():
    events = [
        _issue_classified("r1_p_1", "kurrant", {"type": "bug", "complexity": "M", "risk_level": "MEDIUM"}),
        _issue_classified("r1_p_2", "kurrant", {"type": "bug", "complexity": "S", "risk_level": "LOW"}),
        _issue_classified("r1_p_3", "kurrant", {"type": "feature", "complexity": "M", "risk_level": "LOW"}),
    ]

    result = metrics.compute_classification_metrics(events)

    assert result["classified_total"] == 3
    assert result["by_type"] == {"bug": 2, "feature": 1}
    assert result["by_complexity"] == {"M": 2, "S": 1}
    assert result["by_risk_level"] == {"MEDIUM": 1, "LOW": 2}


def test_compute_classification_metrics_filters_by_project():
    events = [
        _issue_classified("r1_a_1", "alpha", {"type": "bug", "complexity": "S", "risk_level": "LOW"}),
        _issue_classified("r1_b_1", "beta", {"type": "feature", "complexity": "L", "risk_level": "HIGH"}),
    ]

    result = metrics.compute_classification_metrics(events, project="alpha")

    assert result["classified_total"] == 1
    assert result["by_type"] == {"bug": 1}


def test_compute_classification_metrics_duplicate_classification_keeps_latest_only():
    events = [
        _issue_classified("r1_p_1", "kurrant", {"type": "bug", "complexity": "S", "risk_level": "LOW"}),
        _issue_classified("r1_p_1", "kurrant", {"type": "feature", "complexity": "L", "risk_level": "HIGH"}),
    ]

    result = metrics.compute_classification_metrics(events)

    assert result["classified_total"] == 1
    assert result["by_type"] == {"feature": 1}


def test_compute_classification_metrics_empty_events_returns_zero():
    result = metrics.compute_classification_metrics([])

    assert result["classified_total"] == 0
    assert result["by_type"] == {}
    assert result["by_complexity"] == {}
    assert result["by_risk_level"] == {}


def _issue_escalated_reason(issue_run_id, project, ts, reason):
    return {
        "event_type": "issue.escalated", "issue_run_id": issue_run_id, "project": project,
        "timestamp": ts, "data": {"reason": reason},
    }


def _issue_failed_reason(issue_run_id, project, ts, reason=None):
    data = {"reason": reason} if reason is not None else {}
    return {
        "event_type": "issue.failed", "issue_run_id": issue_run_id, "project": project,
        "timestamp": ts, "data": data,
    }


def test_compute_failure_taxonomy_maps_known_reasons_to_categories():
    events = [
        _issue_escalated_reason("r1_p_1", "kurrant", "2026-09-04T10:00:00.000Z", "verification_failed"),
        _issue_escalated_reason("r1_p_2", "kurrant", "2026-09-04T10:00:00.000Z", "needs_clarification"),
        _issue_escalated_reason("r1_p_3", "kurrant", "2026-09-04T10:00:00.000Z", "worktree_creation_failed"),
    ]

    result = metrics.compute_failure_taxonomy(events)

    assert result["total"] == 3
    assert result["by_category"] == {"verification": 1, "requirement": 1, "environment": 1}
    assert abs(result["by_category_pct"]["verification"] - (1 / 3)) < 1e-9


def test_compute_failure_taxonomy_unmapped_reason_becomes_unknown():
    events = [_issue_escalated_reason("r1_p_1", "kurrant", "2026-09-04T10:00:00.000Z", "something_new")]

    result = metrics.compute_failure_taxonomy(events)

    assert result["by_category"] == {"unknown": 1}


def test_compute_failure_taxonomy_missing_reason_becomes_unknown():
    events = [_issue_failed_reason("r1_p_1", "kurrant", "2026-09-04T10:00:00.000Z")]  # no reason field

    result = metrics.compute_failure_taxonomy(events)

    assert result["by_category"] == {"unknown": 1}


def test_compute_failure_taxonomy_filters_by_project():
    events = [
        _issue_escalated_reason("r1_a_1", "alpha", "2026-09-04T10:00:00.000Z", "verification_failed"),
        _issue_escalated_reason("r1_b_1", "beta", "2026-09-04T10:00:00.000Z", "needs_clarification"),
    ]

    result = metrics.compute_failure_taxonomy(events, project="alpha")

    assert result["total"] == 1
    assert result["by_category"] == {"verification": 1}


def test_compute_failure_taxonomy_zero_escalations_returns_empty_pct_not_error():
    result = metrics.compute_failure_taxonomy([])

    assert result["total"] == 0
    assert result["by_category"] == {}
    assert result["by_category_pct"] == {}


def test_compute_first_pass_verification_metrics_matches_verification_pass_rate_today():
    events = [
        _verification_started("r1_p_1", "kurrant", "2026-09-04T10:00:00.000Z"),
        _verification_passed("r1_p_1", "kurrant", "2026-09-04T10:00:30.000Z"),
        _verification_started("r1_p_2", "kurrant", "2026-09-04T11:00:00.000Z"),
        _verification_failed("r1_p_2", "kurrant", "2026-09-04T11:00:10.000Z"),
    ]

    result = metrics.compute_first_pass_verification_metrics(events)

    assert result["first_pass_verification_total"] == 2
    assert result["first_pass_verification_passed"] == 1
    assert result["first_pass_verification_rate"] == 0.5


def test_compute_first_pass_verification_metrics_picks_earliest_outcome_not_latest():
    # Simulates a future retry: the first attempt failed, a later one passed.
    # First-pass must count this issue as a FAILED first attempt, not a
    # passed one - proving this isn't just "did it ever pass."
    events = [
        _verification_failed("r1_p_1", "kurrant", "2026-09-04T10:00:00.000Z"),
        _verification_passed("r1_p_1", "kurrant", "2026-09-04T10:05:00.000Z"),
    ]

    result = metrics.compute_first_pass_verification_metrics(events)

    assert result["first_pass_verification_total"] == 1
    assert result["first_pass_verification_passed"] == 0
    assert result["first_pass_verification_rate"] == 0.0


def test_compute_first_pass_verification_metrics_filters_by_project():
    events = [
        _verification_passed("r1_a_1", "alpha", "2026-09-04T10:00:00.000Z"),
        _verification_failed("r1_b_1", "beta", "2026-09-04T10:00:00.000Z"),
    ]

    result = metrics.compute_first_pass_verification_metrics(events, project="alpha")

    assert result["first_pass_verification_total"] == 1
    assert result["first_pass_verification_rate"] == 1.0


def test_compute_first_pass_verification_metrics_zero_denominator_returns_none():
    result = metrics.compute_first_pass_verification_metrics([])

    assert result["first_pass_verification_total"] == 0
    assert result["first_pass_verification_rate"] is None


def test_compute_first_pass_verification_metrics_missing_timestamp_ignored():
    events = [
        {"event_type": "verification.passed", "issue_run_id": "r1_p_1", "project": "kurrant"},  # no timestamp
    ]

    result = metrics.compute_first_pass_verification_metrics(events)

    assert result["first_pass_verification_total"] == 0
