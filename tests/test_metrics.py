import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import metrics


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
