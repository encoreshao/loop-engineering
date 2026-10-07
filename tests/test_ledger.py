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


def test_legacy_terminal_event_is_complete_not_incomplete():
    events = [ev("loop.started", "t1", "2026-10-06T09:00:00Z", definition="x"),
              ev("loop.completed", "t1", "2026-10-06T09:01:00Z", final_state="completed", stop_reason="completed"),
              ev("loop.started", "t2", "2026-10-06T09:00:00Z", definition="x"),
              ev("loop.stopped", "t2", "2026-10-06T09:02:00Z")]
    runs = {r.run_id: r for r in ledger.iter_runs(events_iter=lambda **kw: events)}
    assert runs["t1"].complete and runs["t1"].final_state == "completed" and runs["t1"].loop_name == "x"
    assert runs["t1"].total_cost_usd is None and runs["t1"].iterations == []
    assert runs["t2"].complete and runs["t2"].final_state == "stopped"


def test_loop_result_wins_over_terminal_event():
    events = [ev("loop.failed", "w", "2026-10-06T09:01:00Z", final_state="failed"),
              ev("loop.result", "w", "2026-10-06T09:00:00Z", run_id="w", definition="x", final_state="completed", iterations=[])]
    assert ledger.iter_runs(events_iter=lambda **kw: events)[0].final_state == "completed"


def test_days_pushes_since_date_down():
    seen = {}
    def it(**kw):
        seen.update(kw)
        return []
    ledger.iter_runs(days=3, events_dir="d", events_iter=it)
    from datetime import datetime, timedelta, timezone
    assert seen["since_date"] == (datetime.now(timezone.utc) - timedelta(days=4)).strftime("%Y-%m-%d")
    assert seen["events_dir"] == "d"


# ---- backfill + golden master ------------------------------------------------

import json
import loop_budget
import loop_serialize
from loop_result import IterationResult, LoopResult
from loop_state import LoopState
from loop_verifiers import VerificationResult


def write_sample_result_json(results_dir, run_id, cost=0.0, state=LoopState.COMPLETED, name="test-loop",
                             seconds=0.0, verified=True, overall="ok", iterations=1, no_budget=False):
    verification = VerificationResult(name="tests", passed=verified, exit_code=0, duration_ms=1, output="x", evidence={})
    budget = {} if no_budget else {"overall": overall, "cost": {"used_usd": cost}, "runtime": {"used_seconds": seconds}}
    its = [IterationResult(iteration=i + 1, state=state, verification_results=[verification],
                           budget=budget, progressed=True) for i in range(iterations)]
    result = LoopResult(loop_id="l_" + run_id, run_id=run_id, definition_name=name, final_state=state,
                        iterations=its, stop_reason=state.value)
    return loop_serialize.write_result(result, results_dir=results_dir, emit=lambda *a, **k: None)


def test_backfill_idempotent(tmp_path):
    write_sample_result_json(tmp_path / "results", run_id="old_1")
    assert ledger.backfill_from_results(tmp_path / "results", tmp_path / "events") == 1
    assert ledger.backfill_from_results(tmp_path / "results", tmp_path / "events") == 0
    assert (tmp_path / "events" / "backfill-loop-result.jsonl").read_text().count("\n") == 1


def test_ledger_dedupes_by_run_id(tmp_path):
    write_sample_result_json(tmp_path / "results", run_id="old_1")
    ledger.backfill_from_results(tmp_path / "results", tmp_path / "events")
    ledger.backfill_from_results(tmp_path / "results", tmp_path / "events")
    assert [r.run_id for r in ledger.iter_runs(events_dir=tmp_path / "events")] == ["old_1"]


def test_backfill_skips_runs_with_loop_result_in_any_file(tmp_path):
    import events
    write_sample_result_json(tmp_path / "results", run_id="live_1")
    events.emit("loop.result", "live_1", data={"run_id": "live_1", "definition": "test-loop", "final_state": "completed", "iterations": []},
                events_dir=tmp_path / "events")
    assert ledger.backfill_from_results(tmp_path / "results", tmp_path / "events") == 0
    assert not (tmp_path / "events" / "backfill-loop-result.jsonl").exists()


def test_backfill_skips_running_snapshots_and_missing_dirs(tmp_path):
    path = write_sample_result_json(tmp_path / "results", run_id="run_x")
    data = json.loads(path.read_text())
    data["status"] = "running"
    path.write_text(json.dumps(data))
    assert ledger.backfill_from_results(tmp_path / "results", tmp_path / "events") == 0
    assert ledger.backfill_from_results(tmp_path / "nope", tmp_path / "events") == 0


def test_backfill_timestamp_from_run_id_else_mtime(tmp_path):
    write_sample_result_json(tmp_path / "results", run_id="run_20260901_100000_a", seconds=60)
    write_sample_result_json(tmp_path / "results", run_id="weird_id")
    ledger.backfill_from_results(tmp_path / "results", tmp_path / "events")
    recs = {r.run_id: r for r in ledger.iter_runs(events_dir=tmp_path / "events")}
    assert recs["run_20260901_100000_a"].started_at.startswith("2026-09-01T10:00:00")
    assert recs["run_20260901_100000_a"].finished_at.startswith("2026-09-01T10:01:00")
    assert recs["weird_id"].finished_at  # mtime fallback


def test_backfill_days_filter_uses_backfilled_timestamp(tmp_path):
    write_sample_result_json(tmp_path / "results", run_id="run_20200101_100000_a")
    ledger.backfill_from_results(tmp_path / "results", tmp_path / "events")
    assert ledger.iter_runs(days=30, events_dir=tmp_path / "events") == []
    assert len(ledger.iter_runs(events_dir=tmp_path / "events")) == 1


def _golden_fixture(results_dir):
    spec = [
        ("run_20260901_100000_a", 1.5, LoopState.COMPLETED, "gitlab-issue-loop", 3600, True, "ok", 2),
        ("run_20260902_100000_b", 2.0, LoopState.FAILED, "gitlab-issue-loop", 1800, False, "warning", 1),
        ("run_20260902_180000_c", 0.5, LoopState.ESCALATED, "topic-monitor-loop", 600, True, "exceeded", 3),
        ("run_20260915_100000_d", 4.0, LoopState.COMPLETED, "topic-monitor-loop", 7200, True, "ok", 1),
        ("run_20261002_100000_e", 0.0, LoopState.COMPLETED, "gitlab-issue-loop", 0, True, "ok", 1),
    ]
    for rid, cost, st, name, secs, ver, overall, n in spec:
        write_sample_result_json(results_dir, rid, cost=cost, state=st, name=name, seconds=secs,
                                 verified=ver, overall=overall, iterations=n)
    write_sample_result_json(results_dir, "run_20261003_100000_nobudget", name="gitlab-issue-loop", no_budget=True)
    write_sample_result_json(results_dir, "unparseable_id", name="gitlab-issue-loop", cost=1.0)


def _ledger_runs(tmp_path):
    ledger.backfill_from_results(tmp_path / "results", tmp_path / "events")
    return ledger.iter_runs(events_dir=tmp_path / "events")


def _row(bucket, runs, cost, ok, warning, exceeded):
    return {"bucket": bucket, "runs": runs, "cost_used_usd": cost,
            "status_counts": {"ok": ok, "warning": warning, "exceeded": exceeded}}


def test_golden_master_serialize_and_budget(tmp_path):
    """Expected values were computed with the pre-ledger result.json readers
    (summarize_results/summarize_run_costs/summarize_by_*) over this exact
    fixture; the ledger-backed readers must reproduce them."""
    import pytest
    _golden_fixture(tmp_path / "results")
    runs = _ledger_runs(tmp_path)
    s = loop_serialize.summarize_results(runs)
    assert s["total_runs"] == 7
    assert s["success_rate"] == pytest.approx(5 / 7)
    assert s["escalation_rate"] == pytest.approx(1 / 7)
    assert s["average_cost_usd"] == pytest.approx(9.0 / 7)
    assert s["efficiency_score"] == pytest.approx(0.015151515151515152)
    assert loop_serialize.summarize_run_costs(runs) == {
        "total_runs": 7, "total_cost_usd": 9.0, "cost_per_run_usd": pytest.approx(9.0 / 7)}
    assert loop_budget.summarize_by_loop(runs) == [
        {"definition_name": "gitlab-issue-loop", "runs": 3, "cost_used_usd": 3.5,
         "status_counts": {"ok": 2, "warning": 1, "exceeded": 0}},
        {"definition_name": "topic-monitor-loop", "runs": 2, "cost_used_usd": 4.5,
         "status_counts": {"ok": 1, "warning": 0, "exceeded": 1}}]
    day = [_row("2026-10-02", 1, 0.0, 1, 0, 0), _row("2026-09-15", 1, 4.0, 1, 0, 0),
           _row("2026-09-02", 2, 2.5, 0, 1, 1), _row("2026-09-01", 1, 1.5, 1, 0, 0)]
    assert loop_budget.summarize_by_time(runs, granularity="day") == day
    assert loop_budget.summarize_by_time(runs, granularity="day", limit=1) == day[:1]
    assert loop_budget.summarize_by_time(runs, granularity="week") == [
        _row("2026-W40", 1, 0.0, 1, 0, 0), _row("2026-W38", 1, 4.0, 1, 0, 0), _row("2026-W36", 3, 4.0, 1, 1, 1)]
    assert loop_budget.summarize_by_time(runs, granularity="month") == [
        _row("2026-10", 1, 0.0, 1, 0, 0), _row("2026-09", 4, 8.0, 2, 1, 1)]


def test_readers_default_to_ledger(tmp_path, monkeypatch):
    import events
    monkeypatch.setattr(events, "DEFAULT_EVENTS_DIR", tmp_path / "events")
    _golden_fixture(tmp_path / "results")
    ledger.backfill_from_results(tmp_path / "results")
    assert loop_serialize.summarize_results()["total_runs"] == 7
    assert loop_serialize.summarize_run_costs()["total_runs"] == 7


def test_incomplete_runs_are_excluded_from_summaries():
    runs = [ledger.RunRecord(run_id="inc", final_state="incomplete", complete=False),
            ledger.RunRecord(run_id="ok", final_state="completed", total_cost_usd=2.0,
                             iterations=[{"n": 1}], duration_ms=3600000, verified_success=True, budget_overall="ok", has_result=True,
                             definition="x", loop_name="x")]
    assert loop_serialize.summarize_results(runs)["total_runs"] == 1
    assert loop_serialize.summarize_run_costs(runs)["total_runs"] == 1
    assert loop_budget.summarize_by_loop(runs) == []  # no timestamp-bearing run_id


def test_terminal_only_runs_do_not_change_totals():
    base = ledger.RunRecord(run_id="run_20260901_100000_a", final_state="completed", total_cost_usd=2.0,
                            iterations=[{"n": 1}], duration_ms=3600000, verified_success=True,
                            budget_overall="ok", has_result=True, definition="x", loop_name="x")
    legacy = ledger.RunRecord(run_id="run_20260902_100000_b", final_state="failed", total_cost_usd=5.0,
                              iterations=[{"n": 1}], budget_overall="ok", definition="x", loop_name="x", complete=True)
    assert legacy.has_result is False
    assert loop_serialize.summarize_results([base, legacy]) == loop_serialize.summarize_results([base])
    assert loop_serialize.summarize_run_costs([base, legacy]) == loop_serialize.summarize_run_costs([base])
    assert loop_budget.summarize_by_loop([base, legacy]) == loop_budget.summarize_by_loop([base])
    assert loop_budget.summarize_by_time([base, legacy]) == loop_budget.summarize_by_time([base])


def test_iter_runs_marks_has_result():
    events = [ev("loop.completed", "t", "2026-10-06T09:01:00Z", final_state="completed"),
              ev("loop.result", "r", "2026-10-06T09:00:00Z", run_id="r", definition="x", final_state="completed", iterations=[])]
    runs = {r.run_id: r for r in ledger.iter_runs(events_iter=lambda **kw: events)}
    assert runs["r"].has_result is True and runs["t"].has_result is False


def test_events_dir_env_override_resolved_at_call_time(tmp_path, monkeypatch):
    import events
    monkeypatch.undo()  # drop conftest's monkeypatch of the constant
    monkeypatch.setenv("LOOP_EVENTS_DIR", str(tmp_path / "e"))
    assert events.default_events_dir() == tmp_path / "e"


def test_backfill_skips_corrupt_result_files(tmp_path):
    write_sample_result_json(tmp_path / "results", run_id="run_good")
    for run_id, payload in (("run_keyerr", {"run_id": "run_keyerr"}),
                            ("run_typeerr", {"run_id": "run_typeerr", "iterations": 5,
                                             "final_state": "completed", "loop_id": "l",
                                             "definition_name": "x", "stop_reason": "completed"}),
                            ("run_list", ["not", "a", "dict"])):
        d = tmp_path / "results" / run_id
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps(payload))
    assert ledger.backfill_from_results(tmp_path / "results", tmp_path / "events") == 1
    assert [r.run_id for r in ledger.iter_runs(events_dir=tmp_path / "events")] == ["run_good"]


def test_startup_backfill_appends_and_is_best_effort(tmp_path, capsys):
    write_sample_result_json(tmp_path / "results", run_id="run_a")
    assert ledger.run_startup_backfill(results_dir=tmp_path / "results", events_dir=tmp_path / "events") == 1
    assert ledger.run_startup_backfill(results_dir=tmp_path / "results", events_dir=tmp_path / "events") == 0

    def boom(**kw):
        raise RuntimeError("disk gone")
    assert ledger.run_startup_backfill(backfill=boom) is None
    assert "disk gone" in capsys.readouterr().err


def test_since_until_date_window_matches_calendar_days():
    events = [ev("loop.result", "d1", "2026-10-01T23:59:00.000Z", run_id="d1", definition="x", final_state="completed", iterations=[]),
              ev("loop.result", "d2", "2026-10-02T00:00:01.000Z", run_id="d2", definition="x", final_state="completed", iterations=[]),
              ev("loop.result", "d8", "2026-10-08T00:00:01.000Z", run_id="d8", definition="x", final_state="completed", iterations=[])]
    seen = {}
    def it(**kw):
        seen.update(kw)
        return events
    runs = ledger.iter_runs(since_date="2026-10-02", until_date="2026-10-07", events_iter=it)
    assert [r.run_id for r in runs] == ["d2"]
    # since is pushed down; until is not (the backfill file sorts after dates).
    assert seen["since_date"] == "2026-10-02" and "until_date" not in seen
