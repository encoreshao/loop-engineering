#!/usr/bin/env python3
"""Reader over the event ledger: one RunRecord per run_id, built from
`loop.result` events (last one wins). Runs that emitted `loop.started`
but never a `loop.result` are reported as incomplete."""
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKFILL_FILENAME = "backfill-loop-result.jsonl"


@dataclass
class RunRecord:
    run_id: str
    loop_id: str | None = None
    definition: str | None = None
    loop_name: str | None = None
    final_state: str | None = None
    stop_reason: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    total_cost_usd: float | None = None
    iterations: list = field(default_factory=list)
    verified_success: bool = False
    budget_overall: str | None = None
    complete: bool = True


_TERMINAL_EVENTS = {"loop.completed": "completed", "loop.failed": "failed", "loop.stopped": "stopped"}


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def iter_runs(days=None, loop=None, events_dir=None, events_iter=None):
    if events_iter is None:
        import events
        def events_iter(**kw):
            return events.iter_events(**kw)
    cutoff = None
    iter_kwargs = {"events_dir": events_dir}
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        # One day of slack; the non-date-named backfill file sorts after
        # every date stem so it is always included.
        iter_kwargs["since_date"] = (cutoff - timedelta(days=1)).strftime("%Y-%m-%d")

    results, started, terminal = {}, {}, {}
    for event in events_iter(**iter_kwargs):
        run_id = event.get("run_id")
        ts = event.get("timestamp") or ""
        if cutoff is not None:
            parsed = _parse_ts(ts)
            if parsed is None or parsed < cutoff:
                continue
        if event.get("event_type") == "loop.result":
            if run_id not in results or ts >= results[run_id][0]:
                results[run_id] = (ts, event.get("data") or {})
        elif event.get("event_type") in _TERMINAL_EVENTS:
            terminal[run_id] = (ts, event.get("event_type"), event.get("data") or {})
        elif event.get("event_type") == "loop.started":
            started.setdefault(run_id, (ts, event.get("data") or {}))

    records = []  # (sort_ts, record)
    for run_id, (ts, d) in results.items():
        records.append((ts, RunRecord(
            run_id=d.get("run_id") or run_id, loop_id=d.get("loop_id"),
            definition=d.get("definition"), loop_name=d.get("definition"),
            final_state=d.get("final_state"), stop_reason=d.get("stop_reason"),
            started_at=d.get("started_at"), finished_at=d.get("finished_at"),
            duration_ms=d.get("duration_ms"), total_cost_usd=d.get("total_cost_usd"),
            iterations=d.get("iterations") or [],
            verified_success=bool(d.get("verified_success")),
            budget_overall=d.get("budget_overall"), complete=True)))
    for run_id, (ts, etype, d) in terminal.items():
        if run_id in results:
            continue
        sts, sd = started.get(run_id, (None, {}))
        name = d.get("definition") or sd.get("definition")
        records.append((ts, RunRecord(
            run_id=run_id, loop_id=d.get("loop_id") or sd.get("loop_id"),
            definition=name, loop_name=name,
            final_state=d.get("final_state") or _TERMINAL_EVENTS[etype],
            stop_reason=d.get("stop_reason"), started_at=sts, finished_at=ts,
            duration_ms=d.get("duration_ms"), total_cost_usd=d.get("total_cost_usd"),
            iterations=d.get("iterations") or [], complete=True)))
    for run_id, (ts, d) in started.items():
        if run_id in results or run_id in terminal:
            continue
        records.append((ts, RunRecord(
            run_id=run_id, loop_id=d.get("loop_id"), definition=d.get("definition"),
            loop_name=d.get("definition"), final_state="incomplete",
            started_at=ts, complete=False)))
    if loop is not None:
        records = [r for r in records if r[1].loop_name == loop]
    records.sort(key=lambda r: r[0], reverse=True)
    return [r for _, r in records]


def _fmt(dt):
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _loop_result_run_ids(events_dir):
    import events
    return {e.get("run_id") for e in events.iter_events(events_dir=events_dir)
            if e.get("event_type") == "loop.result"}


def backfill_from_results(results_dir=None, events_dir=None):
    """Append one `loop.result` event per legacy finished result.json whose
    run_id has no `loop.result` anywhere in the events dir. Returns the
    number appended. Idempotent; existing lines are never rewritten - the
    events go to a dedicated <events_dir>/backfill-loop-result.jsonl.

    result.json carries no timestamps, so the event's timestamp (and the
    payload's finished_at) is the run's start time parsed from its
    `run_<YYYYMMDD>_<HHMMSS>_...` run_id (same parse as
    loop_budget.run_timestamp) plus the recorded duration; an id that does
    not match falls back to the file's mtime as finished_at. Running
    snapshots are skipped."""
    import events
    import loop_budget
    import loop_serialize
    if events_dir is None:
        events_dir = events.DEFAULT_EVENTS_DIR
    events_dir = Path(events_dir)
    seen = _loop_result_run_ids(events_dir)
    lines = []
    for path in loop_serialize.list_results(results_dir=results_dir):
        try:
            data = loop_serialize.read_result(path)
        except (OSError, ValueError):
            continue
        if data.get("status", "finished") != "finished":
            continue
        run_id = data.get("run_id")
        if not run_id or run_id in seen:
            continue
        summary = loop_serialize.result_summary(data)
        started = loop_budget.run_timestamp(run_id)
        if started is not None:
            finished = started + timedelta(milliseconds=summary["duration_ms"])
        else:
            finished = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            started = finished - timedelta(milliseconds=summary["duration_ms"])
        summary["started_at"], summary["finished_at"] = _fmt(started), _fmt(finished)
        seen.add(run_id)
        lines.append(json.dumps({
            "schema_version": events.SCHEMA_VERSION, "event_id": f"evt_{uuid.uuid4().hex}",
            "timestamp": _fmt(finished), "event_type": "loop.result", "run_id": run_id,
            "issue_run_id": None, "project": None, "issue_iid": None, "data": summary}) + "\n")
    if not lines:
        return 0
    events_dir.mkdir(parents=True, exist_ok=True)
    payload = "".join(lines).encode("utf-8")
    fd = os.open(str(events_dir / BACKFILL_FILENAME), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    return len(lines)
