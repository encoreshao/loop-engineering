import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from loop_definition import LoopDefinition

EXAMPLE = {
    "name": "gitlab-issue-fixer",
    "version": 1,
    "trigger": {"type": "schedule", "schedule": "0 10 * * 1-5"},
    "goal": {"type": "issue_resolution"},
    "agent": {"provider": "claude", "model": "default"},
    "context": {"sources": ["issue", "repository", "project_memory", "task_memory"]},
    "actions": [
        "inspect_issue",
        "inspect_repository",
        "modify_code",
        "run_tests",
        "create_merge_request",
    ],
    "verification": {"required": ["tests", "lint", "diff_scope"]},
    "stop_conditions": {
        "max_iterations": 3,
        "max_runtime_minutes": 30,
        "max_cost_usd": 5,
        "no_progress_iterations": 2,
    },
    "human_gates": ["merge", "production_deploy"],
    "retry": {"enabled": True, "max_attempts": 2},
}


def test_from_dict_round_trips_every_field():
    d = LoopDefinition.from_dict(EXAMPLE)

    assert d.name == "gitlab-issue-fixer"
    assert d.version == 1
    assert d.trigger.type == "schedule"
    assert d.trigger.schedule == "0 10 * * 1-5"
    assert d.goal.type == "issue_resolution"
    assert d.agent.provider == "claude"
    assert d.agent.model == "default"
    assert d.context.sources == ["issue", "repository", "project_memory", "task_memory"]
    assert d.actions == [
        "inspect_issue",
        "inspect_repository",
        "modify_code",
        "run_tests",
        "create_merge_request",
    ]
    assert d.verification.required == ["tests", "lint", "diff_scope"]
    assert d.stop_conditions.max_iterations == 3
    assert d.stop_conditions.max_runtime_minutes == 30
    assert d.stop_conditions.max_cost_usd == 5
    assert d.stop_conditions.no_progress_iterations == 2
    assert d.human_gates == ["merge", "production_deploy"]
    assert d.retry.enabled is True
    assert d.retry.max_attempts == 2


@pytest.mark.parametrize(
    "missing_key,path",
    [
        ("name", ()),
        ("version", ()),
    ],
)
def test_missing_top_level_required_field_raises(missing_key, path):
    data = {k: v for k, v in EXAMPLE.items() if k != missing_key}
    with pytest.raises(ValueError, match=missing_key):
        LoopDefinition.from_dict(data)


def test_missing_trigger_type_raises():
    data = dict(EXAMPLE)
    data["trigger"] = {"schedule": "0 10 * * 1-5"}
    with pytest.raises(ValueError, match="trigger.type"):
        LoopDefinition.from_dict(data)


def test_missing_goal_type_raises():
    data = dict(EXAMPLE)
    data["goal"] = {}
    with pytest.raises(ValueError, match="goal.type"):
        LoopDefinition.from_dict(data)


def test_defaults_apply_when_optional_sections_omitted():
    minimal = {
        "name": "minimal-loop",
        "version": 1,
        "trigger": {"type": "manual"},
        "goal": {"type": "issue_resolution"},
    }

    d = LoopDefinition.from_dict(minimal)

    assert d.stop_conditions.max_iterations == 3
    assert d.stop_conditions.max_runtime_minutes == 30
    assert d.stop_conditions.max_cost_usd == 5
    assert d.stop_conditions.no_progress_iterations == 2
    assert d.retry.enabled is True
    assert d.retry.max_attempts == 2
    assert d.human_gates == []
    assert d.actions == []
    assert d.context.sources == []
    assert d.verification.required == []


def test_verifiers_field_defaults_to_empty_list():
    minimal = {
        "name": "minimal-loop",
        "version": 1,
        "trigger": {"type": "manual"},
        "goal": {"type": "issue_resolution"},
    }

    d = LoopDefinition.from_dict(minimal)

    assert d.verifiers == []


def test_verifiers_field_round_trips_raw_specs():
    data = dict(EXAMPLE)
    data["verifiers"] = [
        {"name": "tests", "type": "command", "command": "pytest"},
        {"name": "diff", "type": "git_diff", "allowed_paths": ["src/"]},
    ]

    d = LoopDefinition.from_dict(data)

    assert d.verifiers == [
        {"name": "tests", "type": "command", "command": "pytest"},
        {"name": "diff", "type": "git_diff", "allowed_paths": ["src/"]},
    ]


def test_from_yaml_reads_real_file(tmp_path):
    yaml_path = tmp_path / "loop.yaml"
    yaml_path.write_text(yaml.safe_dump(EXAMPLE))

    d = LoopDefinition.from_yaml(yaml_path)

    assert d.name == "gitlab-issue-fixer"
    assert d.trigger.type == "schedule"
