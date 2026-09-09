import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

from loop_eval import ScriptedAgent, ScriptedVerifier, ScriptExhausted
from loop_verifiers import Verifier


def test_scripted_agent_returns_entries_in_order():
    agent = ScriptedAgent([
        {"changed": True, "cost_usd": 0.10},
        {"changed": False, "cost_usd": 0.0},
    ])

    first = agent(context={})
    second = agent(context={})

    assert first == {"changed": True, "cost_usd": 0.10}
    assert second == {"changed": False, "cost_usd": 0.0}


def test_scripted_agent_raises_when_called_beyond_its_script():
    agent = ScriptedAgent([{"changed": True, "cost_usd": 0.10}])
    agent(context={})

    with pytest.raises(ScriptExhausted):
        agent(context={})


def test_scripted_verifier_is_a_real_verifier():
    verifier = ScriptedVerifier("tests", [True])
    assert isinstance(verifier, Verifier)


def test_scripted_verifier_returns_passed_result_with_exit_code_zero():
    verifier = ScriptedVerifier("tests", [True])

    result = verifier.verify(context={})

    assert result.name == "tests"
    assert result.passed is True
    assert result.exit_code == 0


def test_scripted_verifier_returns_failed_result_with_a_fixed_exit_code():
    verifier = ScriptedVerifier("tests", [False, False])

    first = verifier.verify(context={})
    second = verifier.verify(context={})

    assert first.passed is False
    assert second.passed is False
    assert first.exit_code == second.exit_code == 1


def test_scripted_verifier_raises_when_called_beyond_its_script():
    verifier = ScriptedVerifier("tests", [True])
    verifier.verify(context={})

    with pytest.raises(ScriptExhausted):
        verifier.verify(context={})


import yaml

from loop_eval import EvalCase, load_case, load_cases


_MINIMAL_CASE = {
    "name": "minimal",
    "description": "A minimal case for loader tests.",
    "definition": {
        "name": "eval-minimal",
        "version": 1,
        "trigger": {"type": "manual"},
        "goal": {"type": "fix"},
    },
    "script": {
        "agent": [{"changed": True, "cost_usd": 0.0}],
        "verifiers": {"tests": [True]},
    },
    "expect": {"final_state": "completed", "stop_reason": "completed"},
}


def test_load_case_builds_an_eval_case(tmp_path):
    path = tmp_path / "minimal.yaml"
    path.write_text(yaml.safe_dump(_MINIMAL_CASE))

    case = load_case(path)

    assert isinstance(case, EvalCase)
    assert case.name == "minimal"
    assert case.definition.name == "eval-minimal"
    assert case.agent_script == [{"changed": True, "cost_usd": 0.0}]
    assert case.verifier_scripts == {"tests": [True]}
    assert case.expect == {"final_state": "completed", "stop_reason": "completed"}


def test_load_case_defaults_missing_script_sections_to_empty(tmp_path):
    data = dict(_MINIMAL_CASE)
    data["script"] = {}
    path = tmp_path / "no-script.yaml"
    path.write_text(yaml.safe_dump(data))

    case = load_case(path)

    assert case.agent_script == []
    assert case.verifier_scripts == {}


@pytest.mark.parametrize("missing_key", ["name", "description", "definition", "script", "expect"])
def test_load_case_raises_on_missing_required_field(tmp_path, missing_key):
    data = dict(_MINIMAL_CASE)
    del data[missing_key]
    path = tmp_path / "broken.yaml"
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(ValueError, match=missing_key):
        load_case(path)


def test_load_cases_reads_every_yaml_file_sorted_by_name(tmp_path):
    (tmp_path / "b.yaml").write_text(yaml.safe_dump({**_MINIMAL_CASE, "name": "b"}))
    (tmp_path / "a.yaml").write_text(yaml.safe_dump({**_MINIMAL_CASE, "name": "a"}))

    cases = load_cases(tmp_path)

    assert [case.name for case in cases] == ["a", "b"]
