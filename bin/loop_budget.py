#!/usr/bin/env python3
"""BudgetController - see
docs/superpowers/specs/2026-09-06-loop-runtime-foundation-design.md.
80% is the WARNING threshold for every dimension (matches the plan's own
`loop status` mockup showing a 78% bar as still-running)."""
from dataclasses import dataclass
from enum import Enum

_WARNING_THRESHOLD = 0.8


class BudgetStatus(str, Enum):
    OK = "ok"
    WARNING = "warning"
    EXCEEDED = "exceeded"


def _status_for(used, limit):
    if limit is None:
        return BudgetStatus.OK
    if used >= limit:
        return BudgetStatus.EXCEEDED
    if used >= limit * _WARNING_THRESHOLD:
        return BudgetStatus.WARNING
    return BudgetStatus.OK


def _worst(statuses):
    if BudgetStatus.EXCEEDED in statuses:
        return BudgetStatus.EXCEEDED
    if BudgetStatus.WARNING in statuses:
        return BudgetStatus.WARNING
    return BudgetStatus.OK


@dataclass
class BudgetController:
    stop_conditions: object

    def check(self, iterations, runtime_seconds, cost_usd):
        max_iterations = self.stop_conditions.max_iterations
        max_runtime_seconds = (
            self.stop_conditions.max_runtime_minutes * 60
            if self.stop_conditions.max_runtime_minutes is not None
            else None
        )
        max_cost_usd = self.stop_conditions.max_cost_usd

        iterations_status = _status_for(iterations, max_iterations)
        runtime_status = _status_for(runtime_seconds, max_runtime_seconds)
        cost_status = _status_for(cost_usd, max_cost_usd)

        return {
            "iterations": {
                "status": iterations_status,
                "used": iterations,
                "limit": max_iterations,
            },
            "runtime": {
                "status": runtime_status,
                "used_seconds": runtime_seconds,
                "limit_seconds": max_runtime_seconds,
            },
            "cost": {
                "status": cost_status,
                "used_usd": cost_usd,
                "limit_usd": max_cost_usd,
            },
            "overall": _worst([iterations_status, runtime_status, cost_status]),
        }
