#!/usr/bin/env python3
"""LoopResult JSON persistence - see
docs/superpowers/specs/2026-09-07-loop-cli-design.md. Closes the plan's
section 22 storage gap enough for `loop status`/`inspect`/`cost`/`replay`
to have something real to read: <results_dir>/<run_id>/result.json, one
file per run, plain JSON (no reconstruction back into dataclasses -
every CLI consumer only ever reads plain fields back out)."""
import json
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
    if results_dir is None:
        results_dir = DEFAULT_RESULTS_DIR
    results_dir = Path(results_dir)
    run_dir = results_dir / loop_result.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "result.json"
    path.write_text(json.dumps(to_json_dict(loop_result), indent=2))
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
