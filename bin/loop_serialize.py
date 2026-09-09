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


def write_result(loop_result, results_dir=None):
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
    return path


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


def summarize_results(results_dir=None):
    """{"total_runs", "success_rate", "escalation_rate",
    "average_cost_usd"} across every persisted run - the plan's "Loop
    Overview" (section 23). No average-duration figure - LoopResult
    carries no start/finish timestamp yet, and this deliberately reports
    only what's actually computable rather than guessing (matches
    bin/health.py's honest-degradation pattern). Rates/average are None
    (not 0) when there are no runs to divide by."""
    paths = list_results(results_dir=results_dir)
    total_runs = len(paths)
    if total_runs == 0:
        return {
            "total_runs": 0,
            "success_rate": None,
            "escalation_rate": None,
            "average_cost_usd": None,
        }

    completed = 0
    escalated = 0
    total_cost_usd = 0.0
    for path in paths:
        data = read_result(path)
        if data["final_state"] == "completed":
            completed += 1
        if data["final_state"] == "escalated":
            escalated += 1
        if data["iterations"]:
            total_cost_usd += data["iterations"][-1].get("budget", {}).get("cost", {}).get("used_usd") or 0

    return {
        "total_runs": total_runs,
        "success_rate": completed / total_runs,
        "escalation_rate": escalated / total_runs,
        "average_cost_usd": total_cost_usd / total_runs,
    }


def summarize_run_costs(results_dir=None):
    """{"total_runs", "total_cost_usd", "cost_per_run_usd"} - the same
    numbers `loop_cli.py cost` prints, shared here so the CLI and the
    dashboard's Cost page compute them one way. cost_per_run_usd is None
    (not 0) when there are no runs to divide by, matching
    summarize_results's honest-degradation convention."""
    paths = list_results(results_dir=results_dir)
    total_runs = len(paths)
    total_cost_usd = 0.0
    for path in paths:
        data = read_result(path)
        if data["iterations"]:
            total_cost_usd += data["iterations"][-1].get("budget", {}).get("cost", {}).get("used_usd") or 0

    return {
        "total_runs": total_runs,
        "total_cost_usd": total_cost_usd,
        "cost_per_run_usd": total_cost_usd / total_runs if total_runs else None,
    }
