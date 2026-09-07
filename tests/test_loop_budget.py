import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from loop_budget import BudgetController, BudgetStatus
from loop_definition import StopConditions


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
