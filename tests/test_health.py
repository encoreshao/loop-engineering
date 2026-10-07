import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import health
import ledger


def _metrics_report(resolution_rate, autonomy_rate, verification_pass_rate, issues_processed, issues_escalated):
    return {
        "quality_and_autonomy": {"resolution_rate": resolution_rate, "autonomy_rate": autonomy_rate},
        "verification": {"verification_pass_rate": verification_pass_rate},
        "issue": {"issues_processed": issues_processed, "issues_escalated": issues_escalated},
    }


def test_compute_health_score_all_four_available():
    metrics_report = _metrics_report(
        resolution_rate=0.8, autonomy_rate=0.8, verification_pass_rate=0.9,
        issues_processed=10, issues_escalated=1,
    )

    result = health.compute_health_score(metrics_report, {})

    # resolution=80, autonomy=80, verification=90, escalation=(1 - 1/10)*100=90
    # Renormalization is uniform in every case: divide by the available weight sum (75),
    # never a fixed 100. This prevents the paradox where more data scores lower.
    expected = (80 * 30 + 80 * 25 + 90 * 15 + 90 * 5) / 75
    assert abs(result["score"] - expected) < 1e-9
    assert result["is_partial"] is True
    assert {k: v for k, v in result["components"].items() if v is not None} == {
        "resolution": 80.0, "autonomy": 80.0, "verification": 90.0, "escalation": 90.0}
    assert "cost_efficiency" in result["missing_components"]
    assert "retry_rate" in result["missing_components"]
    assert "learning_effectiveness" in result["missing_components"]
    assert "resolution" not in result["missing_components"]


def test_compute_health_score_resolution_none_renormalizes_remaining_weights():
    metrics_report = _metrics_report(
        resolution_rate=None, autonomy_rate=0.8, verification_pass_rate=0.9,
        issues_processed=10, issues_escalated=1,
    )

    result = health.compute_health_score(metrics_report, {})

    # resolution excluded; remaining weights autonomy=25, verification=15, escalation=5 sum to 45
    expected = (80 * 25 + 90 * 15 + 90 * 5) / 45
    assert abs(result["score"] - expected) < 1e-9
    assert result["components"]["resolution"] is None
    assert "resolution" in result["missing_components"]


def test_compute_health_score_zero_processed_issues_makes_resolution_and_escalation_none():
    metrics_report = _metrics_report(
        resolution_rate=None, autonomy_rate=None, verification_pass_rate=None,
        issues_processed=0, issues_escalated=0,
    )

    result = health.compute_health_score(metrics_report, {})

    assert result["score"] is None
    assert all(v is None for v in result["components"].values())


def test_compute_health_score_missing_reason_is_fixed_constant():
    metrics_report = _metrics_report(0.5, 0.5, 0.5, 4, 1)

    result = health.compute_health_score(metrics_report, {})

    assert result["missing_reason"] == health._MISSING_REASON


METRICS = _metrics_report(0.8, 0.8, 0.9, 10, 1)
COST = {}


def rec(iterations=1, final_state="completed", cost=None, verified=False, definition="gitlab-issue-loop", has_result=True):
    return ledger.RunRecord(
        run_id="r", definition=definition, loop_name=definition, final_state=final_state,
        total_cost_usd=cost, verified_success=verified, has_result=has_result,
        iterations=[{"n": i + 1, "state": "x", "verifiers_passed": True, "cost_usd": None} for i in range(iterations)],
    )


def test_retry_rate_component():
    runs = [rec(iterations=1), rec(iterations=2)]
    h = health.compute_health_score(METRICS, COST, runs=runs, memory_outcomes=[])
    assert h["components"]["retry_rate"] == 50
    assert "retry_rate" not in h["missing_components"]


def test_retry_rate_ignores_other_loops_and_unfinished():
    runs = [rec(definition="rss-watch-loop", iterations=3), rec(final_state="failed", iterations=3), rec(has_result=False)]
    h = health.compute_health_score(METRICS, COST, runs=runs, memory_outcomes=[])
    assert h["components"]["retry_rate"] is None


def test_cost_efficiency_excludes_unknown_cost():
    runs = [rec(cost=1.0, verified=True), rec(cost=None, verified=True)]
    h = health.compute_health_score(METRICS, COST, runs=runs, memory_outcomes=[])
    assert h["details"]["cost_efficiency"]["value"] == 1.0
    assert h["components"]["cost_efficiency"] == 80


def test_cost_efficiency_none_without_verified():
    h = health.compute_health_score(METRICS, COST, runs=[rec(cost=1.0, verified=False)], memory_outcomes=[])
    assert h["components"]["cost_efficiency"] is None


def test_learning_effectiveness_needs_sample():
    h = health.compute_health_score(METRICS, COST, runs=[], memory_outcomes=[("reused", True)] * 3)
    assert h["components"]["learning_effectiveness"] is None


def test_learning_effectiveness_delta():
    outcomes = [("reused", True)] * 5 + [("fresh", True)] * 3 + [("fresh", False)] * 2
    h = health.compute_health_score(METRICS, COST, runs=[], memory_outcomes=outcomes)
    assert abs(h["components"]["learning_effectiveness"] - 90) < 1e-9


def test_full_health_is_not_partial_and_uses_plan_weights():
    runs = [rec(iterations=1, cost=0.4, verified=True)]
    outcomes = [("reused", True)] * 5 + [("fresh", True)] * 5
    h = health.compute_health_score(METRICS, COST, runs=runs, memory_outcomes=outcomes)
    assert h["is_partial"] is False
    assert h["missing_components"] == []
    expected = 80 * 30 + 80 * 25 + 90 * 15 + 90 * 5 + 100 * 10 + 100 * 10 + 50 * 5
    assert abs(h["score"] - expected / 100) < 1e-9


def test_no_runs_or_outcomes_keeps_three_missing():
    h = health.compute_health_score(METRICS, COST)
    assert h["is_partial"] is True
    assert {"cost_efficiency", "retry_rate", "learning_effectiveness"} <= set(h["missing_components"])
