#!/usr/bin/env python3
"""Compute lesson-reuse and lesson-effectiveness metrics from this loop's
event log (outputs/events/*.jsonl) - see
docs/superpowers/specs/2026-09-05-learning-design.md. A sibling
report-builder to bin/cost.py: one clear responsibility (learning/memory
metrics), built entirely from memory.created/memory.reused events plus
each reused issue's own terminal outcome event
(issue.completed/issue.escalated/issue.failed). No dependency on
bin/metrics.py - this module re-derives the one issue-count it needs
directly from the event list, keeping it dependency-free like
bin/risk.py/bin/health.py."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import events

FAILURES_PREVENTED_REASON = (
    "no counterfactual data exists - cannot know whether a specific issue "
    "would have failed without the lesson it cited, only whether it "
    "succeeded or failed with it"
)

_SUCCESS_EVENT_TYPES = ("issue.completed",)
_FAILURE_EVENT_TYPES = ("issue.escalated", "issue.failed")


def _issue_outcomes(events_list):
    """{issue_run_id: "success"|"failure"} for every issue_run_id that
    has reached a terminal outcome. An issue_run_id with no terminal
    event yet is simply absent - callers treat that as "pending"."""
    outcomes = {}
    for event in events_list:
        et = event.get("event_type")
        issue_run_id = event.get("issue_run_id")
        if et in _SUCCESS_EVENT_TYPES:
            outcomes[issue_run_id] = "success"
        elif et in _FAILURE_EVENT_TYPES:
            outcomes[issue_run_id] = "failure"
    return outcomes


def _lesson_categories(events_list, project=None):
    """{lesson_id: category} from every memory.created event's data -
    the category as it was at creation time. A lesson_id with no
    matching memory.created event in this date range (e.g. it predates
    the report window) is simply absent."""
    categories = {}
    for event in events_list:
        if project is not None and event.get("project") != project:
            continue
        if event.get("event_type") != "memory.created":
            continue
        data = event.get("data") or {}
        lesson_id = data.get("lesson_id")
        if lesson_id is not None:
            categories[lesson_id] = data.get("category")
    return categories


def compute_lesson_effectiveness(events, project=None):
    """[{"lesson_id", "category", "times_reused", "successful_reuses",
    "failed_reuses", "effectiveness_rate"}, ...] - one entry per
    distinct lesson_id seen in a memory.reused event (a lesson created
    but never reused doesn't appear here - effectiveness is only
    meaningful once cited at least once). times_reused counts every
    memory.reused event for that lesson_id regardless of outcome;
    successful_reuses/failed_reuses only count reuses whose
    issue_run_id has a resolved terminal outcome by the time this runs.
    effectiveness_rate = successful_reuses / (successful_reuses +
    failed_reuses), None if that sum is 0."""
    events = list(events)
    outcomes = _issue_outcomes(events)
    categories = _lesson_categories(events, project=project)

    per_lesson = {}
    for event in events:
        if project is not None and event.get("project") != project:
            continue
        if event.get("event_type") != "memory.reused":
            continue
        data = event.get("data") or {}
        lesson_id = data.get("lesson_id")
        if lesson_id is None:
            continue
        stats = per_lesson.setdefault(lesson_id, {"times_reused": 0, "successful_reuses": 0, "failed_reuses": 0})
        stats["times_reused"] += 1
        outcome = outcomes.get(event.get("issue_run_id"))
        if outcome == "success":
            stats["successful_reuses"] += 1
        elif outcome == "failure":
            stats["failed_reuses"] += 1

    result = []
    for lesson_id, stats in per_lesson.items():
        denom = stats["successful_reuses"] + stats["failed_reuses"]
        result.append({
            "lesson_id": lesson_id,
            "category": categories.get(lesson_id),
            "times_reused": stats["times_reused"],
            "successful_reuses": stats["successful_reuses"],
            "failed_reuses": stats["failed_reuses"],
            "effectiveness_rate": (stats["successful_reuses"] / denom) if denom else None,
        })
    return result


def compute_reuse_metrics(events, project=None):
    """{"lessons_created", "total_reuses", "memory_reuse_rate",
    "memory_success_rate", "failures_prevented": None,
    "failures_prevented_reason"}. memory_reuse_rate = (issues with at
    least one memory.reused event) / issues_processed (issue.started
    events with a timestamp, deduped by issue_run_id - same safety
    check compute_issue_metrics uses, re-derived here to keep this
    module dependency-free from bin/metrics.py). memory_success_rate =
    total successful reuses / (total successful + total failed) across
    every lesson combined. failures_prevented is always None - there
    is no counterfactual data."""
    events = list(events)

    lessons_created = 0
    issues_processed = set()
    issues_with_reuse = set()
    total_successful = 0
    total_failed = 0
    total_reuses = 0
    outcomes = _issue_outcomes(events)

    for event in events:
        if project is not None and event.get("project") != project:
            continue
        et = event.get("event_type")
        if et == "memory.created":
            lessons_created += 1
        elif et == "issue.started":
            if event.get("timestamp") is not None:
                issues_processed.add(event.get("issue_run_id"))
        elif et == "memory.reused":
            total_reuses += 1
            issue_run_id = event.get("issue_run_id")
            issues_with_reuse.add(issue_run_id)
            outcome = outcomes.get(issue_run_id)
            if outcome == "success":
                total_successful += 1
            elif outcome == "failure":
                total_failed += 1

    denom = total_successful + total_failed
    return {
        "lessons_created": lessons_created,
        "total_reuses": total_reuses,
        "memory_reuse_rate": (len(issues_with_reuse) / len(issues_processed)) if issues_processed else None,
        "memory_success_rate": (total_successful / denom) if denom else None,
        "failures_prevented": None,
        "failures_prevented_reason": FAILURES_PREVENTED_REASON,
    }


def build_learning_report(events_dir=None, since_date=None, until_date=None, project=None):
    """{"reuse": compute_reuse_metrics(...), "lessons":
    compute_lesson_effectiveness(...), "scope": {"since_date",
    "until_date", "project"}}."""
    all_events = list(events.iter_events(events_dir=events_dir, since_date=since_date, until_date=until_date))
    return {
        "reuse": compute_reuse_metrics(all_events, project=project),
        "lessons": compute_lesson_effectiveness(all_events, project=project),
        "scope": {"since_date": since_date, "until_date": until_date, "project": project},
    }


def _fmt_rate(value):
    return f"{value * 100:.1f}%" if value is not None else "N/A"


def format_learning_report(report):
    scope = report["scope"]
    since_date, until_date, project = scope["since_date"], scope["until_date"], scope["project"]
    if since_date is None and until_date is None:
        header = "Loop Learning (all time)"
    else:
        header = f"Loop Learning ({since_date} to {until_date})"
    if project is not None:
        header += f" — project: {project}"
    lines = [header, ""]

    reuse = report["reuse"]
    lines.append("Reuse")
    lines.append(f"  Lessons created       {reuse['lessons_created']}")
    lines.append(f"  Total reuses          {reuse['total_reuses']}")
    lines.append(f"  Memory reuse rate     {_fmt_rate(reuse['memory_reuse_rate'])}")
    lines.append(f"  Memory success rate   {_fmt_rate(reuse['memory_success_rate'])}")
    lines.append(f"  Failures prevented    N/A ({reuse['failures_prevented_reason']})")
    lines.append("")

    lessons = report["lessons"]
    lines.append("Lessons")
    if not lessons:
        lines.append("  N/A (no lessons reused in this window)")
    else:
        for lesson in sorted(lessons, key=lambda entry: -entry["times_reused"]):
            category = lesson["category"] or "uncategorized"
            lines.append(
                f"  {lesson['lesson_id']}  ({category})  "
                f"reused={lesson['times_reused']} "
                f"effectiveness={_fmt_rate(lesson['effectiveness_rate'])}"
            )
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
            print(f"learning: --days must be an integer, got {days_raw!r}", file=sys.stderr)
            sys.exit(1)
        until = datetime.now(timezone.utc).date()
        since = until - timedelta(days=days - 1)
        since_date, until_date = since.isoformat(), until.isoformat()

    project = _parse_flag(argv, "--project")

    events_dir_raw = _parse_flag(argv, "--events-dir")
    events_dir = None
    if events_dir_raw:
        if not Path(events_dir_raw).exists():
            print(f"learning: --events-dir {events_dir_raw} does not exist", file=sys.stderr)
            sys.exit(1)
        events_dir = Path(events_dir_raw)

    report = build_learning_report(events_dir=events_dir, since_date=since_date, until_date=until_date, project=project)
    print(format_learning_report(report))


if __name__ == "__main__":
    main()
