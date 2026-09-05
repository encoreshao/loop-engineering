#!/usr/bin/env python3
"""Compute performance metrics from this loop's event log
(outputs/events/*.jsonl, written by bin/events.py) - see
docs/superpowers/specs/2026-09-04-metrics-module-design.md. The four
compute_* functions below are pure (no disk access, take already-filtered
event lists) so the arithmetic - including every zero-denominator case -
is cheap to unit-test independently of event reading. build_report() (see
the next task) is the only function that touches disk, via
events.iter_events()."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import events

RETRY_RATE_UNAVAILABLE_REASON = (
    "the loop escalates on first verification failure; no retry behavior exists yet"
)
FAILURE_RATE_UNAVAILABLE_REASON = (
    'no issue-level "failed" outcome exists yet, only fix/answer/escalate'
)


def _parse_timestamp(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _duration_ms(start_iso, end_iso):
    return (_parse_timestamp(end_iso) - _parse_timestamp(start_iso)).total_seconds() * 1000


def _mean(values):
    return sum(values) / len(values) if values else None


def compute_run_metrics(events):
    """{"runs_total", "runs_success", "runs_failed",
    "average_run_duration_ms"} from run.started/run.completed/run.failed
    events, paired by run_id."""
    started = {}
    completed = {}
    failed_run_ids = set()

    for event in events:
        et = event.get("event_type")
        run_id = event.get("run_id")
        ts = event.get("timestamp")
        if et == "run.started":
            if ts is not None:
                started[run_id] = ts
        elif et == "run.completed":
            if ts is not None:
                completed[run_id] = ts
        elif et == "run.failed":
            failed_run_ids.add(run_id)

    durations = [
        _duration_ms(start_ts, completed[run_id])
        for run_id, start_ts in started.items()
        if run_id in completed
    ]

    return {
        "runs_total": len(started),
        "runs_success": len(completed),
        "runs_failed": len(failed_run_ids),
        "average_run_duration_ms": _mean(durations),
    }


def compute_issue_metrics(events, project=None):
    """{"issues_processed", "issues_completed", "issues_escalated",
    "issues_failed", "average_issue_duration_ms"} from issue.started/
    issue.completed/issue.escalated/issue.failed events, paired by
    issue_run_id. When project is given, only events whose "project"
    field equals it are counted."""
    started = {}
    ended = {}
    completed_ids = set()
    escalated_ids = set()
    failed_ids = set()

    for event in events:
        if project is not None and event.get("project") != project:
            continue
        et = event.get("event_type")
        issue_run_id = event.get("issue_run_id")
        ts = event.get("timestamp")
        if et == "issue.started":
            if ts is not None:
                started[issue_run_id] = ts
        elif et == "issue.completed":
            completed_ids.add(issue_run_id)
            if ts is not None:
                ended[issue_run_id] = ts
        elif et == "issue.escalated":
            escalated_ids.add(issue_run_id)
            if ts is not None:
                ended[issue_run_id] = ts
        elif et == "issue.failed":
            failed_ids.add(issue_run_id)
            if ts is not None:
                ended[issue_run_id] = ts

    durations = [
        _duration_ms(start_ts, ended[issue_run_id])
        for issue_run_id, start_ts in started.items()
        if issue_run_id in ended
    ]

    return {
        "issues_processed": len(started),
        "issues_completed": len(completed_ids),
        "issues_escalated": len(escalated_ids),
        "issues_failed": len(failed_ids),
        "average_issue_duration_ms": _mean(durations),
    }


def compute_verification_metrics(events, project=None):
    """{"verification_total", "verification_passed",
    "verification_failed", "verification_pass_rate",
    "average_verification_duration_ms"} from verification.started/passed/
    failed events, paired by issue_run_id. Same project filtering as
    compute_issue_metrics."""
    started = {}
    ended = {}
    passed_ids = set()
    failed_ids = set()

    for event in events:
        if project is not None and event.get("project") != project:
            continue
        et = event.get("event_type")
        issue_run_id = event.get("issue_run_id")
        ts = event.get("timestamp")
        if et == "verification.started":
            if ts is not None:
                started[issue_run_id] = ts
        elif et == "verification.passed":
            passed_ids.add(issue_run_id)
            if ts is not None:
                ended[issue_run_id] = ts
        elif et == "verification.failed":
            failed_ids.add(issue_run_id)
            if ts is not None:
                ended[issue_run_id] = ts

    durations = [
        _duration_ms(start_ts, ended[issue_run_id])
        for issue_run_id, start_ts in started.items()
        if issue_run_id in ended
    ]

    passed_count = len(passed_ids)
    failed_count = len(failed_ids)
    denom = passed_count + failed_count

    return {
        "verification_total": denom,
        "verification_passed": passed_count,
        "verification_failed": failed_count,
        "verification_pass_rate": (passed_count / denom) if denom else None,
        "average_verification_duration_ms": _mean(durations),
    }


def compute_classification_metrics(events, project=None):
    """{"classified_total", "by_type", "by_complexity", "by_risk_level"}
    from issue.classified events' data fields, paired by issue_run_id
    (last event for a given issue_run_id wins, if it was somehow
    classified more than once). Each by_* dict maps the field's value
    to a count, built only from classifications that actually carried
    that field - a classification missing "risk_level", for example,
    simply isn't counted in by_risk_level. Same project filtering as
    compute_issue_metrics."""
    latest_by_issue_run_id = {}
    for event in events:
        if project is not None and event.get("project") != project:
            continue
        if event.get("event_type") != "issue.classified":
            continue
        latest_by_issue_run_id[event.get("issue_run_id")] = event.get("data") or {}

    by_type, by_complexity, by_risk_level = {}, {}, {}
    for data in latest_by_issue_run_id.values():
        if data.get("type") is not None:
            by_type[data["type"]] = by_type.get(data["type"], 0) + 1
        if data.get("complexity") is not None:
            by_complexity[data["complexity"]] = by_complexity.get(data["complexity"], 0) + 1
        if data.get("risk_level") is not None:
            by_risk_level[data["risk_level"]] = by_risk_level.get(data["risk_level"], 0) + 1

    return {
        "classified_total": len(latest_by_issue_run_id),
        "by_type": by_type,
        "by_complexity": by_complexity,
        "by_risk_level": by_risk_level,
    }


FAILURE_REASON_CATEGORY_MAP = {
    "verification_failed": "verification",
    "needs_clarification": "requirement",
    "worktree_creation_failed": "environment",
}


def compute_failure_taxonomy(events, project=None):
    """{"total", "by_category", "by_category_pct"} from issue.escalated
    and issue.failed events' data.reason, mapped through
    FAILURE_REASON_CATEGORY_MAP. A reason not in the map, or a missing
    reason field, counts under "unknown" rather than raising or being
    dropped - a future escalation path introduced without a matching
    LOOPX_INSTRUCTIONS.md update degrades safely instead of silently
    vanishing from the taxonomy. by_category_pct is {} (not filled with
    0.0s) when total is 0. Same project filtering as compute_issue_metrics."""
    by_category = {}
    for event in events:
        if project is not None and event.get("project") != project:
            continue
        if event.get("event_type") not in ("issue.escalated", "issue.failed"):
            continue
        reason = (event.get("data") or {}).get("reason")
        category = FAILURE_REASON_CATEGORY_MAP.get(reason, "unknown")
        by_category[category] = by_category.get(category, 0) + 1

    total = sum(by_category.values())
    by_category_pct = (
        {category: count / total for category, count in by_category.items()}
        if total else {}
    )

    return {
        "total": total,
        "by_category": by_category,
        "by_category_pct": by_category_pct,
    }


def compute_first_pass_verification_metrics(events, project=None):
    """{"first_pass_verification_total", "first_pass_verification_passed",
    "first_pass_verification_rate"}. For each issue_run_id, takes the
    EARLIEST-timestamped verification.passed/verification.failed event
    (timestamps are ISO 8601 UTC strings with millisecond precision, so
    plain string comparison is chronological - no need to parse them,
    matching this event log's format everywhere else) and counts
    whether it passed. Events without a timestamp are ignored, same as
    every other compute_* function here. Numerically identical to
    compute_verification_metrics's verification_pass_rate today - the
    loop only ever produces one verification outcome per issue (see
    RETRY_RATE_UNAVAILABLE_REASON) - but defined by timestamp order
    rather than "ever passed"/"ever failed" set membership, so it stays
    correct once Phase 6 (retries) produces more than one outcome per
    issue_run_id. Same project filtering as compute_issue_metrics."""
    earliest_outcome = {}  # issue_run_id -> (timestamp, event_type)
    for event in events:
        if project is not None and event.get("project") != project:
            continue
        et = event.get("event_type")
        if et not in ("verification.passed", "verification.failed"):
            continue
        ts = event.get("timestamp")
        if ts is None:
            continue
        issue_run_id = event.get("issue_run_id")
        current = earliest_outcome.get(issue_run_id)
        if current is None or ts < current[0]:
            earliest_outcome[issue_run_id] = (ts, et)

    total = len(earliest_outcome)
    passed = sum(1 for _, et in earliest_outcome.values() if et == "verification.passed")

    return {
        "first_pass_verification_total": total,
        "first_pass_verification_passed": passed,
        "first_pass_verification_rate": (passed / total) if total else None,
    }


def compute_quality_and_autonomy_metrics(issue_metrics, verification_metrics):
    """{"resolution_rate", "verification_pass_rate", "retry_rate",
    "retry_rate_unavailable_reason", "failure_rate",
    "failure_rate_unavailable_reason", "autonomy_rate",
    "autonomy_rate_is_placeholder", "human_intervention_rate"} - pure
    arithmetic over the two dicts already produced above, no event list.
    retry_rate/failure_rate are ALWAYS None with a fixed reason string -
    see this module's docstring and
    docs/superpowers/specs/2026-09-04-metrics-module-design.md for why.
    autonomy_rate always equals resolution_rate exactly (placeholder
    definition pending a real human-intervention signal)."""
    processed = issue_metrics["issues_processed"]
    completed = issue_metrics["issues_completed"]
    resolution_rate = (completed / processed) if processed else None

    autonomy_rate = resolution_rate
    human_intervention_rate = (1 - autonomy_rate) if autonomy_rate is not None else None

    return {
        "resolution_rate": resolution_rate,
        "verification_pass_rate": verification_metrics["verification_pass_rate"],
        "retry_rate": None,
        "retry_rate_unavailable_reason": RETRY_RATE_UNAVAILABLE_REASON,
        "failure_rate": None,
        "failure_rate_unavailable_reason": FAILURE_RATE_UNAVAILABLE_REASON,
        "autonomy_rate": autonomy_rate,
        "autonomy_rate_is_placeholder": True,
        "human_intervention_rate": human_intervention_rate,
    }


def build_report(events_dir=None, since_date=None, until_date=None, project=None):
    """Reads events.iter_events(events_dir, since_date, until_date) once,
    then assembles {"run", "issue", "verification", "quality_and_autonomy",
    "classification", "failure_taxonomy"}. When project is given, "run"
    becomes {"not_applicable_reason": ...} instead of a filtered (and
    therefore meaningless) run tally - a run spans every project touched
    that run."""
    all_events = list(events.iter_events(events_dir=events_dir, since_date=since_date, until_date=until_date))

    issue_metrics = compute_issue_metrics(all_events, project=project)
    verification_metrics = compute_verification_metrics(all_events, project=project)
    verification_metrics.update(compute_first_pass_verification_metrics(all_events, project=project))
    classification_metrics = compute_classification_metrics(all_events, project=project)
    failure_taxonomy = compute_failure_taxonomy(all_events, project=project)
    quality_and_autonomy = compute_quality_and_autonomy_metrics(issue_metrics, verification_metrics)

    if project is not None:
        run_section = {"not_applicable_reason": "a run is not scoped to a single project"}
    else:
        run_section = compute_run_metrics(all_events)

    return {
        "run": run_section,
        "issue": issue_metrics,
        "verification": verification_metrics,
        "quality_and_autonomy": quality_and_autonomy,
        "classification": classification_metrics,
        "failure_taxonomy": failure_taxonomy,
        "scope": {"since_date": since_date, "until_date": until_date, "project": project},
    }


def bucketed_reports(events_dir=None, days=7, bucket_days=1):
    """Return a list of build_report()-shaped dicts, oldest first, one
    per sequential UTC-day bucket covering the last `days` days, each
    spanning `bucket_days` calendar days. Buckets are constructed
    newest-to-oldest (starting from today and working backward) so that
    a remainder when `days` doesn't divide evenly by `bucket_days` lands
    on the OLDEST bucket, not the newest - after reversing to oldest-first
    order for the return value, that shorter bucket is therefore first in
    the list, not last."""
    today = datetime.now(timezone.utc).date()
    buckets = []
    bucket_until = today
    remaining = days
    while remaining > 0:
        span = min(bucket_days, remaining)
        bucket_since = bucket_until - timedelta(days=span - 1)
        buckets.append((bucket_since, bucket_until))
        bucket_until = bucket_since - timedelta(days=1)
        remaining -= span
    buckets.reverse()

    return [
        build_report(events_dir=events_dir, since_date=since.isoformat(), until_date=until.isoformat())
        for since, until in buckets
    ]


def _fmt_rate(value):
    return f"{value * 100:.1f}%" if value is not None else "N/A"


def _fmt_seconds(ms):
    return f"{ms / 1000:.1f}s" if ms is not None else "N/A"


def _fmt_counts(counts):
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) if counts else "none"


def format_report(report):
    """Renders the dict from build_report() as plain text for the CLI."""
    scope = report["scope"]
    since_date, until_date, project = scope["since_date"], scope["until_date"], scope["project"]
    if since_date is None and until_date is None:
        header = "Loop Metrics (all time)"
    else:
        header = f"Loop Metrics ({since_date} to {until_date})"
    if project is not None:
        header += f" — project: {project}"
    lines = [header, ""]

    lines.append("Runs")
    run = report["run"]
    if "not_applicable_reason" in run:
        lines.append(f"  N/A ({run['not_applicable_reason']})")
    else:
        lines.append(f"  Total          {run['runs_total']}")
        lines.append(f"  Success        {run['runs_success']}")
        lines.append(f"  Failed         {run['runs_failed']}")
        lines.append(f"  Avg duration   {_fmt_seconds(run['average_run_duration_ms'])}")
    lines.append("")

    issue = report["issue"]
    lines.append("Issues")
    lines.append(f"  Processed      {issue['issues_processed']}")
    lines.append(f"  Completed      {issue['issues_completed']}")
    lines.append(f"  Escalated      {issue['issues_escalated']}")
    lines.append(f"  Failed         {issue['issues_failed']}")
    lines.append("")

    qa = report["quality_and_autonomy"]
    verification = report["verification"]
    lines.append("Quality")
    lines.append(f"  Resolution rate         {_fmt_rate(qa['resolution_rate'])}")
    lines.append(f"  Verification pass rate  {_fmt_rate(verification['verification_pass_rate'])}")
    lines.append(f"  First-pass verification {_fmt_rate(verification['first_pass_verification_rate'])}")
    lines.append(f"  Retry rate              N/A ({qa['retry_rate_unavailable_reason']})")
    lines.append(f"  Failure rate            N/A ({qa['failure_rate_unavailable_reason']})")
    lines.append("")

    classification = report["classification"]
    lines.append("Classification")
    if classification["classified_total"] == 0:
        lines.append("  N/A (no issues classified in this window)")
    else:
        lines.append(f"  Classified      {classification['classified_total']}")
        lines.append(f"  By type         {_fmt_counts(classification['by_type'])}")
        lines.append(f"  By complexity   {_fmt_counts(classification['by_complexity'])}")
        lines.append(f"  By risk level   {_fmt_counts(classification['by_risk_level'])}")
    lines.append("")

    failure_taxonomy = report["failure_taxonomy"]
    lines.append("Failure breakdown")
    if failure_taxonomy["total"] == 0:
        lines.append("  N/A (no escalations in this window)")
    else:
        for category, pct in sorted(failure_taxonomy["by_category_pct"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {category:<15} {pct * 100:.1f}%")
    lines.append("")

    lines.append("Autonomy")
    lines.append(f"  Autonomy rate            {_fmt_rate(qa['autonomy_rate'])} (placeholder: currently identical to resolution rate)")
    lines.append(f"  Human intervention rate  {_fmt_rate(qa['human_intervention_rate'])}")
    lines.append("")

    lines.append("Duration")
    lines.append(f"  Avg issue duration          {_fmt_seconds(issue['average_issue_duration_ms'])}")
    lines.append(f"  Avg verification duration   {_fmt_seconds(verification['average_verification_duration_ms'])}")

    return "\n".join(lines)


def _parse_flag(argv, name):
    if name not in argv:
        return None
    idx = argv.index(name)
    if idx + 1 >= len(argv):
        return None
    return argv[idx + 1]


def main():
    argv = sys.argv[1:]

    since_date = until_date = None
    days_raw = _parse_flag(argv, "--days")
    if days_raw is not None:
        try:
            days = int(days_raw)
        except ValueError:
            print(f"metrics: --days must be an integer, got {days_raw!r}", file=sys.stderr)
            sys.exit(1)
        until = datetime.now(timezone.utc).date()
        since = until - timedelta(days=days - 1)
        since_date, until_date = since.isoformat(), until.isoformat()

    project = _parse_flag(argv, "--project")

    events_dir_raw = _parse_flag(argv, "--events-dir")
    events_dir = None
    if events_dir_raw:
        if not Path(events_dir_raw).exists():
            print(f"metrics: --events-dir {events_dir_raw} does not exist", file=sys.stderr)
            sys.exit(1)
        events_dir = Path(events_dir_raw)

    report = build_report(events_dir=events_dir, since_date=since_date, until_date=until_date, project=project)
    print(format_report(report))


if __name__ == "__main__":
    main()
