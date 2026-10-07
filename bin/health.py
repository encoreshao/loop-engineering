#!/usr/bin/env python3
"""Compute the Loop Health score from bin/metrics.py's and bin/cost.py's
report dicts plus, optionally, run-ledger records (bin/ledger.py) and
memory outcomes (bin/learning.py). The plan's 7 weighted components:
Resolution 30, Autonomy 25, Verification 15, Cost Efficiency 10, Retry
Rate 10, Escalation 5, Learning Effectiveness 5. A component with no data
is None and excluded; the rest renormalize. `is_partial` is False only
when all 7 have data; `missing_components` always lists the rest."""

_GITLAB_ISSUE_LOOP = "gitlab-issue-loop"
_MIN_LEARNING_SAMPLE = 5
# (upper bound USD per verified-successful issue, score)
_COST_BANDS = ((0.50, 100), (1.0, 80), (2.0, 60), (4.0, 40))

_COMPONENT_WEIGHTS = {
    "resolution": 30,
    "autonomy": 25,
    "verification": 15,
    "cost_efficiency": 10,
    "retry_rate": 10,
    "escalation": 5,
    "learning_effectiveness": 5,
}

_MISSING_REASON = (
    "components with no data yet (e.g. no ledger runs, no cost-reporting "
    "runs with a verified success, or fewer than 5 issues with and without "
    "memory reuse) are excluded and the rest renormalize"
)


def _to_pct(rate):
    return (rate * 100) if rate is not None else None


def _escalation_rate(issue_metrics):
    processed = issue_metrics["issues_processed"]
    if not processed:
        return None
    return 1 - (issue_metrics["issues_escalated"] / processed)


def _retry_rate(runs):
    """(score, rate) over completed GitLab-issue runs backed by a
    loop.result event; (None, None) when there are none."""
    done = [
        r for r in runs
        if r.has_result and r.final_state == "completed"
        and _GITLAB_ISSUE_LOOP in (r.definition, r.loop_name)
    ]
    if not done:
        return None, None
    rate = sum(1 for r in done if len(r.iterations) > 1) / len(done)
    return 100 * (1 - rate), rate


def _cost_efficiency(runs):
    """(score, usd_per_verified_issue); only runs with a known cost count."""
    known = [r for r in runs if r.total_cost_usd is not None]
    verified = sum(1 for r in known if r.verified_success)
    if not verified:
        return None, None
    value = sum(r.total_cost_usd for r in known) / verified
    for bound, score in _COST_BANDS:
        if value <= bound:
            return score, value
    return 20, value


def _learning_effectiveness(outcomes):
    reused = [ok for kind, ok in outcomes if kind == "reused"]
    fresh = [ok for kind, ok in outcomes if kind == "fresh"]
    if len(reused) < _MIN_LEARNING_SAMPLE or len(fresh) < _MIN_LEARNING_SAMPLE:
        return None, None
    delta = sum(reused) / len(reused) - sum(fresh) / len(fresh)
    delta = max(-0.5, min(0.5, delta))
    return 50 + 100 * delta, delta


def compute_health_score(metrics_report, cost_report, runs=None, memory_outcomes=None):
    """{"score", "is_partial", "components": {"resolution", "autonomy",
    "verification", "escalation"}, "missing_components", "missing_reason"}.
    `metrics_report`/`cost_report` are exactly what
    metrics.build_report()/cost.build_cost_report() already return - no
    disk access here. `cost_report` is accepted but not read yet -
    unused (cost efficiency derives from `runs`' per-run cost). `runs` is
    a list of ledger.RunRecord and `memory_outcomes` is
    learning.memory_outcomes(events); None means "no data" for the
    components derived from them. Each of the 4 known components is normalized to 0-100 (a rate already in
    [0,1] is simply *100); a None input (e.g. resolution_rate with zero
    processed issues) makes that component None too, excluding it from
    both the weighted average and its own weight - the remaining
    available components renormalize their weights to sum to 100 among
    themselves. `score` is None only if ALL 4 known components are
    None."""
    qa = metrics_report["quality_and_autonomy"]
    verification = metrics_report["verification"]
    issue = metrics_report["issue"]

    retry_score, retry_rate = _retry_rate(runs or [])
    cost_score, cost_value = _cost_efficiency(runs or [])
    learn_score, learn_delta = _learning_effectiveness(memory_outcomes or [])

    components = {
        "resolution": _to_pct(qa["resolution_rate"]),
        "autonomy": _to_pct(qa["autonomy_rate"]),
        "verification": _to_pct(verification["verification_pass_rate"]),
        "escalation": _to_pct(_escalation_rate(issue)),
        "cost_efficiency": cost_score,
        "retry_rate": retry_score,
        "learning_effectiveness": learn_score,
    }

    missing_components = [
        name for name, value in components.items() if value is None
    ]

    available = {name: value for name, value in components.items() if value is not None}
    if not available:
        score = None
    else:
        weight_sum = sum(_COMPONENT_WEIGHTS[name] for name in available)
        score = sum(value * _COMPONENT_WEIGHTS[name] for name, value in available.items()) / weight_sum

    return {
        "score": score,
        "is_partial": bool(missing_components),
        "components": components,
        "details": {
            "cost_efficiency": {"value": cost_value},
            "retry_rate": {"value": retry_rate},
            "learning_effectiveness": {"value": learn_delta},
        },
        "missing_components": missing_components,
        "missing_reason": _MISSING_REASON,
    }
