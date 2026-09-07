import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from loop_audit import CheckStatus, audit_definition
from loop_definition import LoopDefinition
from loop_policy import PolicyEngine

REPO_ROOT = Path(__file__).resolve().parent.parent

_COMPLIANT = {
    "name": "compliant-loop",
    "version": 1,
    "trigger": {"type": "schedule", "schedule": "0 10 * * 1-5"},
    "goal": {"type": "issue_resolution"},
    "actions": ["inspect_issue", "modify_code", "merge"],
    "verification": {"required": ["tests", "lint"]},
    "stop_conditions": {
        "max_iterations": 3,
        "max_runtime_minutes": 30,
        "max_cost_usd": 5,
        "no_progress_iterations": 2,
    },
    "human_gates": ["merge"],
    "retry": {"enabled": True, "max_attempts": 2},
}


def _check(report, name):
    return next(c for c in report.checks if c.name == name)


def test_fully_compliant_definition_scores_100():
    definition = LoopDefinition.from_dict(_COMPLIANT)

    report = audit_definition(definition)

    assert report.score == 100.0
    assert all(c.status == CheckStatus.PASS for c in report.checks)


def test_missing_components_always_reported():
    definition = LoopDefinition.from_dict(_COMPLIANT)

    report = audit_definition(definition)

    assert report.is_partial is True
    assert set(report.missing_components) == {
        "credential_boundary",
        "context_strategy",
        "memory_strategy",
        "observability",
    }
    assert report.missing_reason


def test_verification_fails_when_no_verifiers_required():
    data = dict(_COMPLIANT)
    data["verification"] = {"required": []}
    definition = LoopDefinition.from_dict(data)

    report = audit_definition(definition)

    assert _check(report, "verification").status == CheckStatus.FAIL


def test_verification_passes_with_no_verifiers_when_all_actions_are_read_only():
    data = dict(_COMPLIANT)
    data["actions"] = ["inspect_issue", "inspect_repository"]
    data["verification"] = {"required": []}
    data["verifiers"] = []
    data["human_gates"] = []
    definition = LoopDefinition.from_dict(data)

    report = audit_definition(definition)

    assert _check(report, "verification").status == CheckStatus.PASS


def test_stop_conditions_fails_when_max_iterations_not_positive():
    data = dict(_COMPLIANT)
    data["stop_conditions"] = {**_COMPLIANT["stop_conditions"], "max_iterations": 0}
    definition = LoopDefinition.from_dict(data)

    report = audit_definition(definition)

    assert _check(report, "stop_conditions").status == CheckStatus.FAIL


def test_retry_warns_when_disabled():
    data = dict(_COMPLIANT)
    data["retry"] = {"enabled": False, "max_attempts": 2}
    definition = LoopDefinition.from_dict(data)

    report = audit_definition(definition)

    assert _check(report, "retry").status == CheckStatus.WARN


def test_retry_fails_when_enabled_with_zero_max_attempts():
    data = dict(_COMPLIANT)
    data["retry"] = {"enabled": True, "max_attempts": 0}
    definition = LoopDefinition.from_dict(data)

    report = audit_definition(definition)

    assert _check(report, "retry").status == CheckStatus.FAIL


def test_budget_warns_when_a_dimension_is_unbounded():
    definition = LoopDefinition.from_dict(_COMPLIANT)
    definition.stop_conditions.max_cost_usd = None

    report = audit_definition(definition)

    assert _check(report, "budget").status == CheckStatus.WARN
    assert "max_cost_usd" in _check(report, "budget").detail


def test_no_progress_detection_fails_when_not_set():
    definition = LoopDefinition.from_dict(_COMPLIANT)
    definition.stop_conditions.no_progress_iterations = 0

    report = audit_definition(definition)

    assert _check(report, "no_progress_detection").status == CheckStatus.FAIL


def test_human_gates_fails_when_l3_action_ungated():
    data = dict(_COMPLIANT)
    data["human_gates"] = []
    definition = LoopDefinition.from_dict(data)

    report = audit_definition(definition, policy_engine=PolicyEngine())

    check = _check(report, "human_gates")
    assert check.status == CheckStatus.FAIL
    assert "merge" in check.detail


def test_cli_exits_zero_for_compliant_definition(tmp_path):
    yaml_path = tmp_path / "loop.yaml"
    yaml_path.write_text(yaml.safe_dump(_COMPLIANT))

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "loop_audit.py"), str(yaml_path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Loop Ready Score" in result.stdout


def test_cli_exits_nonzero_for_violating_definition(tmp_path):
    data = dict(_COMPLIANT)
    data["human_gates"] = []
    yaml_path = tmp_path / "loop.yaml"
    yaml_path.write_text(yaml.safe_dump(data))

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "loop_audit.py"), str(yaml_path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "FAIL" in result.stdout
