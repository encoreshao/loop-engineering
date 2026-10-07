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
    has_result: bool = False
    budget_overall: str | None = None
    complete: bool = True


_TERMINAL_EVENTS = {"loop.completed": "completed", "loop.failed": "failed", "loop.stopped": "stopped"}


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def iter_runs(days=None, loop=None, events_dir=None, events_iter=None, since_date=None, until_date=None):
    """`days` is a rolling window (now - days). `since_date`/`until_date`
    ('YYYY-MM-DD', inclusive, UTC - the same calendar-day window
    metrics/cost/learning use) filter on each event's timestamp date; only
    since_date is pushed down to the file scan, since the non-date-named
    backfill file sorts after every date stem and must stay included."""
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
    if since_date is not None:
        iter_kwargs["since_date"] = max(since_date, iter_kwargs.get("since_date", since_date))

    results, started, terminal = {}, {}, {}
    for event in events_iter(**iter_kwargs):
        run_id = event.get("run_id")
        ts = event.get("timestamp") or ""
        if cutoff is not None:
            parsed = _parse_ts(ts)
            if parsed is None or parsed < cutoff:
                continue
        if since_date is not None or until_date is not None:
            parsed = _parse_ts(ts)
            if parsed is None:
                continue
            day = parsed.astimezone(timezone.utc).strftime("%Y-%m-%d")
            if (since_date is not None and day < since_date) or (until_date is not None and day > until_date):
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
            budget_overall=d.get("budget_overall"), has_result=True, complete=True)))
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
    """run_ids that already have a loop.result anywhere in events_dir. Only
    lines mentioning "loop.result" are JSON-parsed, so the per-startup
    backfill stays cheap on a large events dir."""
    run_ids = set()
    events_dir = Path(events_dir)
    if not events_dir.exists():
        return run_ids
    for path in sorted(events_dir.glob("*.jsonl")):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "loop.result" not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get("event_type") == "loop.result":
                        run_ids.add(event.get("run_id"))
        except OSError:
            continue
    return run_ids


def _backfill_line(path, seen, events, loop_budget, loop_serialize):
    """The JSONL line to append for one result.json, or None to skip it."""
    data = loop_serialize.read_result(path)
    if data.get("status", "finished") != "finished":
        return None
    run_id = data.get("run_id")
    if not run_id or run_id in seen:
        return None
    summary = loop_serialize.result_summary(data)
    started = loop_budget.run_timestamp(run_id)
    if started is not None:
        finished = started + timedelta(milliseconds=summary["duration_ms"])
    else:
        finished = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        started = finished - timedelta(milliseconds=summary["duration_ms"])
    summary["started_at"], summary["finished_at"] = _fmt(started), _fmt(finished)
    seen.add(run_id)
    return json.dumps({
        "schema_version": events.SCHEMA_VERSION, "event_id": f"evt_{uuid.uuid4().hex}",
        "timestamp": _fmt(finished), "event_type": "loop.result", "run_id": run_id,
        "issue_run_id": None, "project": None, "issue_iid": None, "data": summary}) + "\n"


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
        events_dir = events.default_events_dir()
    events_dir = Path(events_dir)
    seen = _loop_result_run_ids(events_dir)
    lines = []
    for path in loop_serialize.list_results(results_dir=results_dir):
        # write_result names each run dir by its run_id: skip known runs
        # without parsing their result.json on every startup.
        if path.parent.name in seen:
            continue
        try:
            line = _backfill_line(path, seen, events, loop_budget, loop_serialize)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
            # One corrupt result.json must not abort the whole backfill.
            continue
        if line is not None:
            lines.append(line)
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


def run_startup_backfill(results_dir=None, events_dir=None, backfill=None):
    """Best-effort backfill run once per dashboard/scheduler process start,
    so an upgrade by plain `git pull` (no install.sh --upgrade), or a run
    finished by old code after an earlier backfill, still gets its
    loop.result. Idempotent and cheap (skips run_ids that already have one).
    Returns the number appended, or None on failure (logged to stderr,
    never raised)."""
    if backfill is None:
        backfill = backfill_from_results
    try:
        return backfill(results_dir=results_dir, events_dir=events_dir)
    except Exception as exc:  # noqa: BLE001 - startup must never fail on this
        import sys
        print(f"ledger: startup backfill failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
