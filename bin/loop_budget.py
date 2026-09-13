#!/usr/bin/env python3
"""BudgetController - see
docs/superpowers/specs/2026-09-06-loop-runtime-foundation-design.md.
80% is the WARNING threshold for every dimension (matches the plan's own
`loop status` mockup showing a 78% bar as still-running).

`run_timestamp`/`summarize_by_loop`/`summarize_by_time` back the Budget
dashboard page's Per-Loop/Daily/Weekly/Monthly rollups (V2 tech plan
section 23). `LoopResult`/result.json carry no explicit timestamp field,
so the timestamp is parsed out of the run_id itself - every run_id
produced by loop_cli.py, gitlab_loop_runner.py, and topic_monitor_runner.py
follows the same `run_<YYYYMMDD>_<HHMMSS>_<suffix>` shape."""
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

import loop_serialize

_WARNING_THRESHOLD = 0.8
_RUN_ID_TIMESTAMP_RE = re.compile(r"^run_(\d{8})_(\d{6})_")


class BudgetStatus(str, Enum):
    OK = "ok"
    WARNING = "warning"
    EXCEEDED = "exceeded"


def _status_for(used, limit):
    if limit is None:
        return BudgetStatus.OK
    if used >= limit:
        return BudgetStatus.EXCEEDED
    if used >= limit * _WARNING_THRESHOLD:
        return BudgetStatus.WARNING
    return BudgetStatus.OK


def _worst(statuses):
    if BudgetStatus.EXCEEDED in statuses:
        return BudgetStatus.EXCEEDED
    if BudgetStatus.WARNING in statuses:
        return BudgetStatus.WARNING
    return BudgetStatus.OK


@dataclass
class BudgetController:
    stop_conditions: object

    def check(self, iterations, runtime_seconds, cost_usd):
        max_iterations = self.stop_conditions.max_iterations
        max_runtime_seconds = (
            self.stop_conditions.max_runtime_minutes * 60
            if self.stop_conditions.max_runtime_minutes is not None
            else None
        )
        max_cost_usd = self.stop_conditions.max_cost_usd

        iterations_status = _status_for(iterations, max_iterations)
        runtime_status = _status_for(runtime_seconds, max_runtime_seconds)
        cost_status = _status_for(cost_usd, max_cost_usd)

        return {
            "iterations": {
                "status": iterations_status,
                "used": iterations,
                "limit": max_iterations,
            },
            "runtime": {
                "status": runtime_status,
                "used_seconds": runtime_seconds,
                "limit_seconds": max_runtime_seconds,
            },
            "cost": {
                "status": cost_status,
                "used_usd": cost_usd,
                "limit_usd": max_cost_usd,
            },
            "overall": _worst([iterations_status, runtime_status, cost_status]),
        }


def run_timestamp(run_id):
    """Parse the UTC timestamp embedded in a `run_<YYYYMMDD>_<HHMMSS>_...`
    run_id. Returns None for anything that doesn't match - callers skip
    such runs rather than crash on a malformed/unexpected id."""
    match = _RUN_ID_TIMESTAMP_RE.match(run_id)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _new_group():
    return {"runs": 0, "cost_used_usd": 0.0, "status_counts": {"ok": 0, "warning": 0, "exceeded": 0}}


def _budget_row(data):
    """(definition_name, timestamp, cost_used_usd, overall_status) for one
    read_result() dict, or None if it has no budget/timestamp to aggregate -
    same "skip, don't crash" contract as loop_serialize.summarize_results."""
    if not data["iterations"]:
        return None
    budget = data["iterations"][-1].get("budget") or {}
    if not budget:
        return None
    timestamp = run_timestamp(data["run_id"])
    if timestamp is None:
        return None
    cost_used_usd = budget.get("cost", {}).get("used_usd") or 0
    overall = str(budget.get("overall", BudgetStatus.OK))
    return data["definition_name"], timestamp, cost_used_usd, overall


def summarize_by_loop(results_dir=None):
    """Group every persisted run by definition_name: run count, total
    cost used, and a count of runs at each overall budget status. Rows
    sorted by definition_name for a stable, deterministic order."""
    groups = {}
    for path in loop_serialize.list_results(results_dir=results_dir):
        row = _budget_row(loop_serialize.read_result(path))
        if row is None:
            continue
        definition_name, _timestamp, cost_used_usd, overall = row
        group = groups.setdefault(definition_name, _new_group())
        group["runs"] += 1
        group["cost_used_usd"] += cost_used_usd
        group["status_counts"][overall] += 1

    return [{"definition_name": name, **group} for name, group in sorted(groups.items())]


def _bucket_key(timestamp, granularity):
    if granularity == "day":
        return timestamp.strftime("%Y-%m-%d")
    if granularity == "week":
        return timestamp.strftime("%G-W%V")
    if granularity == "month":
        return timestamp.strftime("%Y-%m")
    raise ValueError(f"unknown granularity: {granularity!r}")


def summarize_by_time(results_dir=None, granularity="day", limit=None):
    """Group every persisted run into day/week/month buckets (by the
    timestamp embedded in its run_id): run count, total cost used, and a
    count of runs at each overall budget status per bucket. Rows are
    sorted most-recent-bucket-first (unlike metrics.bucketed_reports,
    which orders oldest-first for trend charts) and, when `limit` is
    given, capped to the most recent `limit` buckets."""
    groups = {}
    for path in loop_serialize.list_results(results_dir=results_dir):
        row = _budget_row(loop_serialize.read_result(path))
        if row is None:
            continue
        _definition_name, timestamp, cost_used_usd, overall = row
        key = _bucket_key(timestamp, granularity)
        group = groups.setdefault(key, _new_group())
        group["runs"] += 1
        group["cost_used_usd"] += cost_used_usd
        group["status_counts"][overall] += 1

    rows = sorted(
        ({"bucket": key, **group} for key, group in groups.items()),
        key=lambda row: row["bucket"],
        reverse=True,
    )
    return rows[:limit] if limit is not None else rows
