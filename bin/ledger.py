#!/usr/bin/env python3
"""Reader over the event ledger: one RunRecord per run_id, built from
`loop.result` events (last one wins). Runs that emitted `loop.started`
but never a `loop.result` are reported as incomplete."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


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
    complete: bool = True


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def iter_runs(days=None, loop=None, events_dir=None, events_iter=None):
    if events_iter is None:
        import events
        def events_iter(**kw):
            return events.iter_events(events_dir=events_dir)
    cutoff = None
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    results, started = {}, {}
    for event in events_iter(events_dir=events_dir):
        run_id = event.get("run_id")
        ts = event.get("timestamp") or ""
        if cutoff is not None:
            parsed = _parse_ts(ts)
            if parsed is None or parsed < cutoff:
                continue
        if event.get("event_type") == "loop.result":
            if run_id not in results or ts >= results[run_id][0]:
                results[run_id] = (ts, event.get("data") or {})
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
            verified_success=bool(d.get("verified_success")), complete=True)))
    for run_id, (ts, d) in started.items():
        if run_id in results:
            continue
        records.append((ts, RunRecord(
            run_id=run_id, loop_id=d.get("loop_id"), definition=d.get("definition"),
            loop_name=d.get("definition"), final_state="incomplete",
            started_at=ts, complete=False)))
    if loop is not None:
        records = [r for r in records if r[1].loop_name == loop]
    records.sort(key=lambda r: r[0], reverse=True)
    return [r for _, r in records]
