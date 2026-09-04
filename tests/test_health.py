import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import health


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
    assert result["components"] == {"resolution": 80.0, "autonomy": 80.0, "verification": 90.0, "escalation": 90.0}
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
    assert result["components"] == {"resolution": None, "autonomy": None, "verification": None, "escalation": None}


def test_compute_health_score_missing_reason_is_fixed_constant():
    metrics_report = _metrics_report(0.5, 0.5, 0.5, 4, 1)

    result = health.compute_health_score(metrics_report, {})

    assert result["missing_reason"] == health._ALWAYS_MISSING_REASON
