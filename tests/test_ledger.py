import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import ledger


def ev(t, run_id, ts, /, **data):
    return {"event_type": t, "run_id": run_id, "timestamp": ts, "data": data}


def test_one_record_per_run_last_result_wins():
    events = [ev("loop.started", "r1", "2026-10-06T09:00:00Z", definition="gitlab-issue-loop"),
              ev("loop.result", "r1", "2026-10-06T09:05:00Z", run_id="r1", definition="gitlab-issue-loop", final_state="failed", total_cost_usd=0.1, iterations=[]),
              ev("loop.result", "r1", "2026-10-06T09:06:00Z", run_id="r1", definition="gitlab-issue-loop", final_state="completed", total_cost_usd=0.2, iterations=[])]
    runs = ledger.iter_runs(events_iter=lambda **kw: events)
    assert len(runs) == 1 and runs[0].final_state == "completed"
    assert runs[0].complete is True and runs[0].loop_name == "gitlab-issue-loop"


def test_incomplete_run_reported():
    runs = ledger.iter_runs(events_iter=lambda **kw: [ev("loop.started", "r2", "2026-10-06T09:00:00Z", definition="topic-monitor-loop")])
    assert runs[0].complete is False and runs[0].final_state == "incomplete"


def test_filter_by_loop():
    events = [ev("loop.result", "a", "2026-10-06T09:00:00Z", run_id="a", definition="x", final_state="completed", iterations=[]),
              ev("loop.result", "b", "2026-10-06T09:00:00Z", run_id="b", definition="y", final_state="completed", iterations=[])]
    assert [r.run_id for r in ledger.iter_runs(loop="y", events_iter=lambda **kw: events)] == ["b"]


def test_newest_first():
    events = [ev("loop.result", "old", "2026-10-01T09:00:00Z", run_id="old", definition="x", final_state="completed", iterations=[]),
              ev("loop.result", "new", "2026-10-06T09:00:00Z", run_id="new", definition="x", final_state="completed", iterations=[])]
    assert [r.run_id for r in ledger.iter_runs(events_iter=lambda **kw: events)] == ["new", "old"]


def test_days_filters_on_event_timestamp():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    fmt = lambda d: d.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    events = [ev("loop.result", "old", fmt(now - timedelta(days=10)), run_id="old", definition="x", final_state="completed", iterations=[]),
              ev("loop.result", "new", fmt(now - timedelta(days=1)), run_id="new", definition="x", final_state="completed", iterations=[])]
    assert [r.run_id for r in ledger.iter_runs(days=3, events_iter=lambda **kw: events)] == ["new"]


def test_reads_real_events_dir(tmp_path):
    import events
    events.emit("loop.result", "r9", data={"run_id": "r9", "definition": "x", "final_state": "completed", "iterations": []}, events_dir=tmp_path)
    assert [r.run_id for r in ledger.iter_runs(events_dir=tmp_path)] == ["r9"]
