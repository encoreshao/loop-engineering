#!/usr/bin/env python3
"""`loop audit` - see docs/superpowers/specs/2026-09-07-loop-audit-design.md.
Honest-degradation pattern matching bin/health.py: scores only the checks
backed by a real LoopDefinition field or module (PolicyEngine), and says
so via is_partial/missing_components/missing_reason rather than
hardcoding a PASS for checks with no real signal behind them yet."""
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from loop_definition import LoopDefinition
from loop_policy import PolicyEngine


class CheckStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


_OUTCOME_WEIGHT = {
    CheckStatus.PASS: 1.0,
    CheckStatus.WARN: 0.5,
    CheckStatus.FAIL: 0.0,
}

_CHECK_WEIGHTS = {
    "goal": 10,
    "trigger": 5,
    "verification": 15,
    "stop_conditions": 15,
    "retry": 10,
    "budget": 10,
    "no_progress_detection": 10,
    "human_gates": 15,
}

_ALWAYS_MISSING = ("credential_boundary", "context_strategy", "memory_strategy", "observability")
_ALWAYS_MISSING_REASON = (
    "credential_boundary/context_strategy/memory_strategy/observability have no "
    "corresponding LoopDefinition field yet - a real check needs a signal this "
    "schema doesn't carry, not a hardcoded PASS"
)


@dataclass
class AuditCheck:
    name: str
    status: CheckStatus
    detail: str


@dataclass
class AuditReport:
    checks: list
    score: float | None
    is_partial: bool
    missing_components: list
    missing_reason: str


def audit_definition(definition, policy_engine=None):
    policy_engine = policy_engine if policy_engine is not None else PolicyEngine()
    checks = []

    checks.append(
        AuditCheck(
            "goal",
            CheckStatus.PASS if definition.goal.type else CheckStatus.FAIL,
            f"goal.type={definition.goal.type!r}",
        )
    )
    checks.append(
        AuditCheck(
            "trigger",
            CheckStatus.PASS if definition.trigger.type else CheckStatus.FAIL,
            f"trigger.type={definition.trigger.type!r}",
        )
    )

    if definition.verification.required:
        checks.append(
            AuditCheck(
                "verification",
                CheckStatus.PASS,
                f"{len(definition.verification.required)} verifier(s) required",
            )
        )
    else:
        checks.append(
            AuditCheck(
                "verification",
                CheckStatus.FAIL,
                "no verifiers required - success cannot be machine-checked",
            )
        )

    sc = definition.stop_conditions
    if sc.max_iterations and sc.max_iterations > 0:
        checks.append(AuditCheck("stop_conditions", CheckStatus.PASS, f"max_iterations={sc.max_iterations}"))
    else:
        checks.append(
            AuditCheck(
                "stop_conditions",
                CheckStatus.FAIL,
                f"max_iterations={sc.max_iterations} - loop cannot make progress",
            )
        )

    retry = definition.retry
    if not retry.enabled:
        checks.append(AuditCheck("retry", CheckStatus.WARN, "retry disabled - a single failure escalates immediately"))
    elif retry.max_attempts >= 1:
        checks.append(AuditCheck("retry", CheckStatus.PASS, f"max_attempts={retry.max_attempts}"))
    else:
        checks.append(AuditCheck("retry", CheckStatus.FAIL, f"enabled with max_attempts={retry.max_attempts}"))

    unbounded = [
        dim
        for dim, limit in (
            ("max_iterations", sc.max_iterations),
            ("max_runtime_minutes", sc.max_runtime_minutes),
            ("max_cost_usd", sc.max_cost_usd),
        )
        if limit is None
    ]
    if not unbounded:
        checks.append(AuditCheck("budget", CheckStatus.PASS, "all three budget dimensions bounded"))
    else:
        checks.append(AuditCheck("budget", CheckStatus.WARN, f"unbounded: {', '.join(unbounded)}"))

    if sc.no_progress_iterations and sc.no_progress_iterations > 0:
        checks.append(
            AuditCheck(
                "no_progress_detection",
                CheckStatus.PASS,
                f"no_progress_iterations={sc.no_progress_iterations}",
            )
        )
    else:
        checks.append(
            AuditCheck(
                "no_progress_detection",
                CheckStatus.FAIL,
                "no_progress_iterations not set - a stuck loop only stops via budget",
            )
        )

    violations = policy_engine.validate_definition(definition)
    if not violations:
        checks.append(AuditCheck("human_gates", CheckStatus.PASS, "every L3 action is gated"))
    else:
        detail = "; ".join(f"{v.action} ({v.risk_level.value})" for v in violations)
        checks.append(AuditCheck("human_gates", CheckStatus.FAIL, detail))

    weight_sum = sum(_CHECK_WEIGHTS[c.name] for c in checks)
    earned = sum(_CHECK_WEIGHTS[c.name] * _OUTCOME_WEIGHT[c.status] for c in checks)
    score = round(100 * earned / weight_sum, 1) if weight_sum else None

    return AuditReport(
        checks=checks,
        score=score,
        is_partial=True,
        missing_components=list(_ALWAYS_MISSING),
        missing_reason=_ALWAYS_MISSING_REASON,
    )


def main():
    if len(sys.argv) != 2:
        print("Usage: loop_audit.py <path/to/loop.yaml>", file=sys.stderr)
        return 2

    definition = LoopDefinition.from_yaml(Path(sys.argv[1]))
    report = audit_definition(definition)

    for check in report.checks:
        print(f"{check.status.value:<4}  {check.name:<22} {check.detail}")

    print()
    print(f"Loop Ready Score: {report.score} / 100 (partial - missing: {', '.join(report.missing_components)})")

    return 1 if any(c.status == CheckStatus.FAIL for c in report.checks) else 0


if __name__ == "__main__":
    sys.exit(main())
