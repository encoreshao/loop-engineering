import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import loop_result
import loop_serialize
import loop_state
from loop_budget import BudgetController, BudgetStatus, run_timestamp, summarize_by_loop, summarize_by_time
from loop_definition import StopConditions


def _write_loop_result(results_dir, run_id, definition_name, cost_used_usd, overall_status):
    budget = {
        "iterations": {"status": overall_status, "used": 1, "limit": 5},
        "runtime": {"status": overall_status, "used_seconds": 10, "limit_seconds": 1800},
        "cost": {"status": overall_status, "used_usd": cost_used_usd, "limit_usd": 5},
        "overall": overall_status,
    }
    iteration = loop_result.IterationResult(
        iteration=1, state=loop_state.LoopState.COMPLETED, verification_results=[], budget=budget, progressed=True,
    )
    result = loop_result.LoopResult(
        loop_id=f"loop_{run_id}", run_id=run_id, definition_name=definition_name,
        final_state=loop_state.LoopState.COMPLETED, iterations=[iteration], stop_reason="completed",
    )
    loop_serialize.write_result(result, results_dir=results_dir)


def _controller(**overrides):
    defaults = dict(max_iterations=10, max_runtime_minutes=10, max_cost_usd=10, no_progress_iterations=2)
    defaults.update(overrides)
    return BudgetController(StopConditions(**defaults))


def test_all_dimensions_under_80_percent_is_ok():
    controller = _controller(max_iterations=10, max_runtime_minutes=10, max_cost_usd=10)

    result = controller.check(iterations=5, runtime_seconds=300, cost_usd=5)

    assert result["iterations"]["status"] == BudgetStatus.OK
    assert result["runtime"]["status"] == BudgetStatus.OK
    assert result["cost"]["status"] == BudgetStatus.OK
    assert result["overall"] == BudgetStatus.OK


def test_dimension_at_80_percent_is_warning():
    controller = _controller(max_iterations=10)

    result = controller.check(iterations=8, runtime_seconds=0, cost_usd=0)

    assert result["iterations"]["status"] == BudgetStatus.WARNING
    assert result["overall"] == BudgetStatus.WARNING


def test_dimension_at_or_over_limit_is_exceeded():
    controller = _controller(max_cost_usd=5)

    result = controller.check(iterations=0, runtime_seconds=0, cost_usd=5)

    assert result["cost"]["status"] == BudgetStatus.EXCEEDED
    assert result["overall"] == BudgetStatus.EXCEEDED


def test_overall_picks_worst_of_three():
    controller = _controller(max_iterations=10, max_runtime_minutes=10, max_cost_usd=10)

    result = controller.check(iterations=1, runtime_seconds=1, cost_usd=10)

    assert result["iterations"]["status"] == BudgetStatus.OK
    assert result["cost"]["status"] == BudgetStatus.EXCEEDED
    assert result["overall"] == BudgetStatus.EXCEEDED


def test_unset_limit_is_always_ok(monkeypatch):
    stop_conditions = StopConditions(max_iterations=10, max_runtime_minutes=10, max_cost_usd=10)
    stop_conditions.max_cost_usd = None
    controller = BudgetController(stop_conditions)

    result = controller.check(iterations=0, runtime_seconds=0, cost_usd=999999)

    assert result["cost"]["status"] == BudgetStatus.OK
    assert result["overall"] == BudgetStatus.OK


def test_used_and_limit_values_are_reported():
    controller = _controller(max_iterations=10, max_runtime_minutes=5, max_cost_usd=10)

    result = controller.check(iterations=3, runtime_seconds=60, cost_usd=2)

    assert result["iterations"]["used"] == 3
    assert result["iterations"]["limit"] == 10
    assert result["runtime"]["used_seconds"] == 60
    assert result["runtime"]["limit_seconds"] == 300
    assert result["cost"]["used_usd"] == 2
    assert result["cost"]["limit_usd"] == 10


def test_run_timestamp_parses_valid_run_id():
    from datetime import datetime, timezone

    assert run_timestamp("run_20260909_082338_ai-news") == datetime(2026, 9, 9, 8, 23, 38, tzinfo=timezone.utc)


def test_run_timestamp_returns_none_for_malformed_run_id():
    assert run_timestamp("not-a-run-id") is None


def test_summarize_by_loop_groups_runs_and_sums_cost(tmp_path):
    _write_loop_result(tmp_path, "run_20260901_100000_a", "gitlab-issue-loop", 1.5, BudgetStatus.OK)
    _write_loop_result(tmp_path, "run_20260902_100000_b", "gitlab-issue-loop", 2.0, BudgetStatus.WARNING)
    _write_loop_result(tmp_path, "run_20260903_100000_c", "topic-monitor-loop", 0.5, BudgetStatus.EXCEEDED)

    rows = summarize_by_loop(results_dir=tmp_path)

    by_name = {row["definition_name"]: row for row in rows}
    assert by_name["gitlab-issue-loop"]["runs"] == 2
    assert by_name["gitlab-issue-loop"]["cost_used_usd"] == 3.5
    assert by_name["gitlab-issue-loop"]["status_counts"] == {"ok": 1, "warning": 1, "exceeded": 0}
    assert by_name["topic-monitor-loop"]["runs"] == 1
    assert by_name["topic-monitor-loop"]["status_counts"] == {"ok": 0, "warning": 0, "exceeded": 1}


def test_summarize_by_loop_returns_empty_list_for_no_runs(tmp_path):
    assert summarize_by_loop(results_dir=tmp_path) == []


def test_summarize_by_time_buckets_by_day_most_recent_first(tmp_path):
    _write_loop_result(tmp_path, "run_20260901_100000_a", "gitlab-issue-loop", 1.0, BudgetStatus.OK)
    _write_loop_result(tmp_path, "run_20260901_150000_b", "gitlab-issue-loop", 1.0, BudgetStatus.OK)
    _write_loop_result(tmp_path, "run_20260902_100000_c", "gitlab-issue-loop", 2.0, BudgetStatus.WARNING)

    rows = summarize_by_time(results_dir=tmp_path, granularity="day")

    assert [row["bucket"] for row in rows] == ["2026-09-02", "2026-09-01"]
    assert rows[0]["runs"] == 1
    assert rows[0]["cost_used_usd"] == 2.0
    assert rows[1]["runs"] == 2
    assert rows[1]["cost_used_usd"] == 2.0


def test_summarize_by_time_buckets_by_week_groups_same_iso_week(tmp_path):
    _write_loop_result(tmp_path, "run_20260907_100000_a", "gitlab-issue-loop", 1.0, BudgetStatus.OK)
    _write_loop_result(tmp_path, "run_20260909_100000_b", "gitlab-issue-loop", 1.0, BudgetStatus.OK)
    _write_loop_result(tmp_path, "run_20260914_100000_c", "gitlab-issue-loop", 1.0, BudgetStatus.OK)

    rows = summarize_by_time(results_dir=tmp_path, granularity="week")

    assert [row["bucket"] for row in rows] == ["2026-W38", "2026-W37"]
    assert rows[0]["runs"] == 1
    assert rows[1]["runs"] == 2


def test_summarize_by_time_buckets_by_month(tmp_path):
    _write_loop_result(tmp_path, "run_20260815_100000_a", "gitlab-issue-loop", 1.0, BudgetStatus.OK)
    _write_loop_result(tmp_path, "run_20260901_100000_b", "gitlab-issue-loop", 2.0, BudgetStatus.OK)

    rows = summarize_by_time(results_dir=tmp_path, granularity="month")

    assert [row["bucket"] for row in rows] == ["2026-09", "2026-08"]


def test_summarize_by_time_respects_limit(tmp_path):
    _write_loop_result(tmp_path, "run_20260901_100000_a", "gitlab-issue-loop", 1.0, BudgetStatus.OK)
    _write_loop_result(tmp_path, "run_20260902_100000_b", "gitlab-issue-loop", 1.0, BudgetStatus.OK)
    _write_loop_result(tmp_path, "run_20260903_100000_c", "gitlab-issue-loop", 1.0, BudgetStatus.OK)

    rows = summarize_by_time(results_dir=tmp_path, granularity="day", limit=2)

    assert [row["bucket"] for row in rows] == ["2026-09-03", "2026-09-02"]


def test_summarize_by_time_skips_runs_with_unparseable_run_id(tmp_path):
    _write_loop_result(tmp_path, "not-a-parseable-run-id", "gitlab-issue-loop", 1.0, BudgetStatus.OK)

    assert summarize_by_time(results_dir=tmp_path, granularity="day") == []
    assert summarize_by_loop(results_dir=tmp_path) == []
