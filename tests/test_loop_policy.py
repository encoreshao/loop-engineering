import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from loop_policy import PolicyEngine, PolicyViolation, RiskLevel
from loop_definition import LoopDefinition


def _definition(actions, human_gates):
    data = {
        "name": "policy-test",
        "version": 1,
        "trigger": {"type": "manual"},
        "goal": {"type": "issue_resolution"},
        "actions": actions,
        "human_gates": human_gates,
    }
    return LoopDefinition.from_dict(data)


def test_known_action_risk_levels_match_the_default_table():
    engine = PolicyEngine()

    assert engine.risk_level_for("inspect_issue") == RiskLevel.L0_READ_ONLY
    assert engine.risk_level_for("modify_code") == RiskLevel.L1_LOCAL_MUTATION
    assert engine.risk_level_for("create_merge_request") == RiskLevel.L2_EXTERNAL_CHANGE
    assert engine.risk_level_for("merge") == RiskLevel.L3_IRREVERSIBLE
    assert engine.risk_level_for("production_deploy") == RiskLevel.L3_IRREVERSIBLE


def test_unmapped_action_defaults_to_l3():
    engine = PolicyEngine()

    assert engine.risk_level_for("some_new_unclassified_action") == RiskLevel.L3_IRREVERSIBLE


def test_l0_and_l1_never_require_human_gate():
    engine = PolicyEngine()

    assert engine.requires_human_gate("inspect_issue", human_gates=[]) is False
    assert engine.requires_human_gate("modify_code", human_gates=[]) is False
    assert engine.requires_human_gate("modify_code", human_gates=["modify_code"]) is False


def test_l2_requires_human_gate_only_when_listed():
    engine = PolicyEngine()

    assert engine.requires_human_gate("create_merge_request", human_gates=[]) is False
    assert engine.requires_human_gate("create_merge_request", human_gates=["create_merge_request"]) is True


def test_l3_always_requires_human_gate_even_if_not_listed():
    engine = PolicyEngine()

    assert engine.requires_human_gate("merge", human_gates=[]) is True
    assert engine.requires_human_gate("merge", human_gates=["merge"]) is True


def test_validate_definition_flags_ungated_l3_action():
    engine = PolicyEngine()
    definition = _definition(actions=["inspect_issue", "merge"], human_gates=[])

    violations = engine.validate_definition(definition)

    assert len(violations) == 1
    assert isinstance(violations[0], PolicyViolation)
    assert violations[0].action == "merge"
    assert violations[0].risk_level == RiskLevel.L3_IRREVERSIBLE


def test_validate_definition_passes_when_l3_action_is_gated():
    engine = PolicyEngine()
    definition = _definition(actions=["inspect_issue", "merge"], human_gates=["merge"])

    violations = engine.validate_definition(definition)

    assert violations == []


def test_validate_definition_does_not_flag_ungated_l2_action():
    engine = PolicyEngine()
    definition = _definition(actions=["create_merge_request"], human_gates=[])

    violations = engine.validate_definition(definition)

    assert violations == []


def test_topic_monitor_actions_are_classified_as_local_mutation():
    from loop_policy import ACTION_RISK_LEVELS, RiskLevel

    assert ACTION_RISK_LEVELS["research_topic"] == RiskLevel.L1_LOCAL_MUTATION
    assert ACTION_RISK_LEVELS["write_briefing"] == RiskLevel.L1_LOCAL_MUTATION
