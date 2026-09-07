#!/usr/bin/env python3
"""RiskLevel classification + PolicyEngine - see
docs/superpowers/specs/2026-09-07-policy-engine-design.md. Distinct from
bin/risk.py (which scores issue *text* for triage) - this scores *action
types* (merge, modify_code, ...) for whether they need a human gate.
An action not in ACTION_RISK_LEVELS defaults to L3 - fail-safe/default-
deny, since an unclassified action is the most dangerous assumption, not
the least."""
from dataclasses import dataclass
from enum import Enum


class RiskLevel(str, Enum):
    L0_READ_ONLY = "L0"
    L1_LOCAL_MUTATION = "L1"
    L2_EXTERNAL_CHANGE = "L2"
    L3_IRREVERSIBLE = "L3"


ACTION_RISK_LEVELS = {
    "inspect_issue": RiskLevel.L0_READ_ONLY,
    "inspect_repository": RiskLevel.L0_READ_ONLY,
    "read_issue": RiskLevel.L0_READ_ONLY,
    "modify_worktree": RiskLevel.L1_LOCAL_MUTATION,
    "modify_code": RiskLevel.L1_LOCAL_MUTATION,
    "run_tests": RiskLevel.L1_LOCAL_MUTATION,
    "create_merge_request": RiskLevel.L2_EXTERNAL_CHANGE,
    "create_mr": RiskLevel.L2_EXTERNAL_CHANGE,
    "post_public_comment": RiskLevel.L2_EXTERNAL_CHANGE,
    "merge": RiskLevel.L3_IRREVERSIBLE,
    "production_deploy": RiskLevel.L3_IRREVERSIBLE,
    "delete_production_data": RiskLevel.L3_IRREVERSIBLE,
}


@dataclass
class PolicyViolation:
    action: str
    risk_level: RiskLevel
    reason: str


class PolicyViolationError(ValueError):
    def __init__(self, violations):
        self.violations = violations
        summary = "; ".join(f"{v.action} ({v.risk_level.value}): {v.reason}" for v in violations)
        super().__init__(f"LoopDefinition policy violation: {summary}")


class PolicyEngine:
    def __init__(self, action_risk_levels=None):
        self.action_risk_levels = action_risk_levels if action_risk_levels is not None else ACTION_RISK_LEVELS

    def risk_level_for(self, action):
        return self.action_risk_levels.get(action, RiskLevel.L3_IRREVERSIBLE)

    def requires_human_gate(self, action, human_gates):
        risk = self.risk_level_for(action)
        if risk == RiskLevel.L3_IRREVERSIBLE:
            return True
        if risk == RiskLevel.L2_EXTERNAL_CHANGE:
            return action in human_gates
        return False

    def validate_definition(self, definition):
        human_gates = set(definition.human_gates)
        violations = []
        for action in definition.actions:
            risk = self.risk_level_for(action)
            if risk == RiskLevel.L3_IRREVERSIBLE and action not in human_gates:
                violations.append(
                    PolicyViolation(
                        action=action,
                        risk_level=risk,
                        reason="L3 (irreversible) action must be listed in human_gates",
                    )
                )
        return violations
