#!/usr/bin/env python3
"""LoopResult JSON persistence - see
docs/superpowers/specs/2026-09-07-loop-cli-design.md. Closes the plan's
section 22 storage gap enough for `loop status`/`inspect`/`cost`/`replay`
to have something real to read: <results_dir>/<run_id>/result.json, one
file per run, plain JSON (no reconstruction back into dataclasses -
every CLI consumer only ever reads plain fields back out)."""
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent.parent / "outputs" / "loop-runs"


def _jsonify(value):
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {k: _jsonify(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    return value


def to_json_dict(loop_result):
    return _jsonify(loop_result)


def write_result(loop_result, results_dir=None, events_dir=None, emit=None):
    """Writes via a temp file + os.replace (atomic on POSIX) rather than
    a direct path.write_text - this is now called roughly once per
    iteration (see LoopRuntime.on_iteration), not just once per run, so
    a concurrent reader (`loop status` while `loop run` is still
    executing) must never be able to observe a partially-written file."""
    if results_dir is None:
        results_dir = DEFAULT_RESULTS_DIR
    results_dir = Path(results_dir)
    run_dir = results_dir / loop_result.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "result.json"
    tmp_path = run_dir / "result.json.tmp"
    tmp_path.write_text(json.dumps(to_json_dict(loop_result), indent=2))
    os.replace(tmp_path, path)
    if getattr(loop_result, "status", "finished") == "finished":
        _emit_loop_result(loop_result, events_dir=events_dir, emit=emit)
    return path


def _emit_loop_result(loop_result, events_dir=None, emit=None):
    """Best-effort: an emit failure must never break write_result. Only
    called for terminal results (running snapshots are not ledger rows)."""
    try:
        if emit is None:
            import events
            emit = events.emit
        kwargs = {"data": result_summary(loop_result)}
        if events_dir is not None:
            kwargs["events_dir"] = events_dir
        emit("loop.result", loop_result.run_id, **kwargs)
    except Exception:
        pass


def result_summary(loop_result):
    """Compact loop.result event payload, mapped field by field (never
    asdict - that would drag in verifier outputs). LoopResult has no
    start/finish timestamps, so finished_at is the time of summarising,
    duration_ms comes from the last iteration's budget runtime, and
    started_at is derived as finished_at - duration."""
    data = to_json_dict(loop_result)
    iterations = []
    for it in data["iterations"]:
        budget = it.get("budget") or {}
        verification = it.get("verification_results") or []
        iterations.append({
            "n": it.get("iteration"),
            "state": it.get("state"),
            "verifiers_passed": all(effective_passed(v) for v in verification),
            "cost_usd": (budget.get("cost") or {}).get("used_usd") or 0,
        })
    last_budget = data["iterations"][-1].get("budget") or {} if data["iterations"] else {}
    duration_ms = int(round(((last_budget.get("runtime") or {}).get("used_seconds") or 0) * 1000))
    total_cost = (last_budget.get("cost") or {}).get("used_usd") or 0
    finished = datetime.now(timezone.utc)
    started = finished - timedelta(milliseconds=duration_ms)
    fmt = lambda d: d.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {
        "run_id": data["run_id"],
        "loop_id": data["loop_id"],
        "definition": data["definition_name"],
        "final_state": data["final_state"],
        "stop_reason": data["stop_reason"],
        "started_at": fmt(started),
        "finished_at": fmt(finished),
        "duration_ms": duration_ms,
        "total_cost_usd": total_cost,
        "iterations": iterations,
        "verified_success": _is_verified_successful(data),
        "budget_overall": str(last_budget.get("overall", "ok")) if last_budget else None,
    }


def read_result(path):
    return json.loads(Path(path).read_text())


def list_results(results_dir=None):
    if results_dir is None:
        results_dir = DEFAULT_RESULTS_DIR
    results_dir = Path(results_dir)
    if not results_dir.exists():
        return []
    return sorted(results_dir.glob("*/result.json"))


def find_latest_result(results_dir=None):
    results = list_results(results_dir=results_dir)
    if not results:
        return None
    return max(results, key=lambda p: p.stat().st_mtime)


def effective_passed(verification):
    """A serialized verification result's real outcome: observe mode forces
    `passed` True and keeps the underlying result in evidence."""
    evidence = verification.get("evidence") or {}
    return evidence.get("observed_passed", verification.get("passed"))


def _is_verified_successful(data):
    """A run counts toward the Loop Efficiency Score's numerator when it
    completed AND every verifier that ran passed - a run with no
    verifiers configured counts as verified (matches loop_audit.py's
    "no code-mutating actions -> nothing to verify" convention), since
    plan section 4.2 requires deterministic verification, not merely
    the agent's own say-so, but a loop with nothing to verify hasn't
    failed that requirement."""
    if data["final_state"] != "completed" or not data["iterations"]:
        return False
    verification_results = data["iterations"][-1].get("verification_results") or []
    return all(effective_passed(v) for v in verification_results)


def _complete_runs(runs):
    """Default to the ledger; only runs backed by a `loop.result` event are
    counted - incomplete runs and terminal-event-only legacy records are not."""
    if runs is None:
        import ledger
        runs = ledger.iter_runs()
    return [r for r in runs if r.complete and r.has_result]


def summarize_results(runs=None):
    """{"total_runs", "success_rate", "escalation_rate",
    "average_cost_usd", "efficiency_score"} across every ledger run
    (`runs`: RunRecords, default ledger.iter_runs(); incomplete runs are
    skipped) - the plan's "Loop Overview" (section 23) plus the experimental
    Loop Efficiency Score (section 17): verified-successful runs / (total
    cost_usd * total duration_hours * total iterations), summed across
    ALL runs (not just successful ones) so a failed run's resource use
    still drags the score down. Rates/average/score are None (not 0)
    when there are no runs, or no cost/duration/iteration data, to
    divide by."""
    runs = _complete_runs(runs)
    total_runs = len(runs)
    if total_runs == 0:
        return {
            "total_runs": 0,
            "success_rate": None,
            "escalation_rate": None,
            "average_cost_usd": None,
            "efficiency_score": None,
        }

    completed = sum(1 for r in runs if r.final_state == "completed")
    escalated = sum(1 for r in runs if r.final_state == "escalated")
    total_cost_usd = sum(r.total_cost_usd or 0 for r in runs)
    total_duration_seconds = sum((r.duration_ms or 0) / 1000 for r in runs)
    total_iterations = sum(len(r.iterations) for r in runs)
    verified_successful = sum(1 for r in runs if r.verified_success)

    total_duration_hours = total_duration_seconds / 3600
    if total_cost_usd and total_duration_hours and total_iterations:
        efficiency_score = verified_successful / (total_cost_usd * total_duration_hours * total_iterations)
    else:
        efficiency_score = None

    return {
        "total_runs": total_runs,
        "success_rate": completed / total_runs,
        "escalation_rate": escalated / total_runs,
        "average_cost_usd": total_cost_usd / total_runs,
        "efficiency_score": efficiency_score,
    }


def summarize_run_costs(runs=None):
    """{"total_runs", "total_cost_usd", "cost_per_run_usd"} - the same
    numbers `loop_cli.py cost` prints, shared here so the CLI and the
    dashboard's Cost page compute them one way. cost_per_run_usd is None
    (not 0) when there are no runs to divide by, matching
    summarize_results's honest-degradation convention."""
    runs = _complete_runs(runs)
    total_runs = len(runs)
    total_cost_usd = 0.0
    for r in runs:
        total_cost_usd += r.total_cost_usd or 0
    return {
        "total_runs": total_runs,
        "total_cost_usd": total_cost_usd,
        "cost_per_run_usd": total_cost_usd / total_runs if total_runs else None,
    }
