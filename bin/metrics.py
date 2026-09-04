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
        if et == "run.started":
            started[run_id] = event["timestamp"]
        elif et == "run.completed":
            completed[run_id] = event["timestamp"]
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
    completed_count = 0
    escalated_count = 0
    failed_count = 0

    for event in events:
        if project is not None and event.get("project") != project:
            continue
        et = event.get("event_type")
        issue_run_id = event.get("issue_run_id")
        if et == "issue.started":
            started[issue_run_id] = event["timestamp"]
        elif et == "issue.completed":
            completed_count += 1
            ended[issue_run_id] = event["timestamp"]
        elif et == "issue.escalated":
            escalated_count += 1
            ended[issue_run_id] = event["timestamp"]
        elif et == "issue.failed":
            failed_count += 1
            ended[issue_run_id] = event["timestamp"]

    durations = [
        _duration_ms(start_ts, ended[issue_run_id])
        for issue_run_id, start_ts in started.items()
        if issue_run_id in ended
    ]

    return {
        "issues_processed": len(started),
        "issues_completed": completed_count,
        "issues_escalated": escalated_count,
        "issues_failed": failed_count,
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
    passed_count = 0
    failed_count = 0

    for event in events:
        if project is not None and event.get("project") != project:
            continue
        et = event.get("event_type")
        issue_run_id = event.get("issue_run_id")
        if et == "verification.started":
            started[issue_run_id] = event["timestamp"]
        elif et == "verification.passed":
            passed_count += 1
            ended[issue_run_id] = event["timestamp"]
        elif et == "verification.failed":
            failed_count += 1
            ended[issue_run_id] = event["timestamp"]

    durations = [
        _duration_ms(start_ts, ended[issue_run_id])
        for issue_run_id, start_ts in started.items()
        if issue_run_id in ended
    ]

    denom = passed_count + failed_count

    return {
        "verification_total": denom,
        "verification_passed": passed_count,
        "verification_failed": failed_count,
        "verification_pass_rate": (passed_count / denom) if denom else None,
        "average_verification_duration_ms": _mean(durations),
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
    then assembles {"run", "issue", "verification", "quality_and_autonomy"}.
    When project is given, "run" becomes {"not_applicable_reason": ...}
    instead of a filtered (and therefore meaningless) run tally - a run
    spans every project touched that run."""
    all_events = list(events.iter_events(events_dir=events_dir, since_date=since_date, until_date=until_date))

    issue_metrics = compute_issue_metrics(all_events, project=project)
    verification_metrics = compute_verification_metrics(all_events, project=project)
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
    }


def _fmt_rate(value):
    return f"{value * 100:.1f}%" if value is not None else "N/A"


def _fmt_seconds(ms):
    return f"{ms / 1000:.1f}s" if ms is not None else "N/A"


def format_report(report):
    """Renders the dict from build_report() as plain text for the CLI."""
    lines = ["Loop Metrics", ""]

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
    lines.append(f"  Retry rate              N/A ({qa['retry_rate_unavailable_reason']})")
    lines.append(f"  Failure rate            N/A ({qa['failure_rate_unavailable_reason']})")
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
    events_dir = Path(events_dir_raw) if events_dir_raw else None

    report = build_report(events_dir=events_dir, since_date=since_date, until_date=until_date, project=project)
    print(format_report(report))


if __name__ == "__main__":
    main()
