#!/usr/bin/env python3
"""Compute a partial Loop Health score from bin/metrics.py's and
bin/cost.py's own report dicts - see
docs/superpowers/specs/2026-09-05-analytics-dashboard-design.md. The
plan's full health score has 7 weighted components; only 4 have a real,
unambiguous data source today. Retry Rate and Learning Effectiveness need
Sprint 6 data that doesn't exist yet, and "Cost Efficiency" has no defined
formula anywhere in the plan - inventing one would mean guessing a
heuristic, which conflicts with the honest-degradation pattern every
sprint so far has followed. This module computes a score from the 4
available components only, and says so: `is_partial` and
`missing_components` are always present in the result."""

_COMPONENT_WEIGHTS = {
    "resolution": 30,
    "autonomy": 25,
    "verification": 15,
    "escalation": 5,
}

_ALWAYS_MISSING = ("cost_efficiency", "retry_rate", "learning_effectiveness")
_ALWAYS_MISSING_REASON = (
    "cost_efficiency has no defined formula in the plan; retry_rate and "
    "learning_effectiveness need Sprint 6 data that doesn't exist yet"
)


def _to_pct(rate):
    return (rate * 100) if rate is not None else None


def _escalation_rate(issue_metrics):
    processed = issue_metrics["issues_processed"]
    if not processed:
        return None
    return 1 - (issue_metrics["issues_escalated"] / processed)


def compute_health_score(metrics_report, cost_report):
    """{"score", "is_partial", "components": {"resolution", "autonomy",
    "verification", "escalation"}, "missing_components", "missing_reason"}.
    `metrics_report`/`cost_report` are exactly what
    metrics.build_report()/cost.build_cost_report() already return - no
    disk access here. `cost_report` is accepted but not read yet -
    reserved for when "Cost Efficiency" gets a defined formula. Each of
    the 4 known components is normalized to 0-100 (a rate already in
    [0,1] is simply *100); a None input (e.g. resolution_rate with zero
    processed issues) makes that component None too, excluding it from
    both the weighted average and its own weight - the remaining
    available components renormalize their weights to sum to 100 among
    themselves. `score` is None only if ALL 4 known components are
    None."""
    qa = metrics_report["quality_and_autonomy"]
    verification = metrics_report["verification"]
    issue = metrics_report["issue"]

    components = {
        "resolution": _to_pct(qa["resolution_rate"]),
        "autonomy": _to_pct(qa["autonomy_rate"]),
        "verification": _to_pct(verification["verification_pass_rate"]),
        "escalation": _to_pct(_escalation_rate(issue)),
    }

    missing_components = list(_ALWAYS_MISSING) + [
        name for name, value in components.items() if value is None
    ]

    available = {name: value for name, value in components.items() if value is not None}
    if not available:
        score = None
    else:
        weight_sum = sum(_COMPONENT_WEIGHTS[name] for name in available)
        # When all 4 known components are available, divide by 100 (including the
        # permanently unavailable components' weights); otherwise use sum of available weights only.
        if weight_sum == 75:  # all 4 known components present
            weight_sum = 100
        score = sum(value * _COMPONENT_WEIGHTS[name] for name, value in available.items()) / weight_sum

    return {
        "score": score,
        "is_partial": True,
        "components": components,
        "missing_components": missing_components,
        "missing_reason": _ALWAYS_MISSING_REASON,
    }
