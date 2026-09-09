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


from loop_eval import EvalOutcome, run_case
from loop_definition import LoopDefinition


def _case(name, definition_overrides, agent_script, verifier_scripts, expect):
    definition_data = {
        "name": f"eval-{name}",
        "version": 1,
        "trigger": {"type": "manual"},
        "goal": {"type": "fix"},
    }
    definition_data.update(definition_overrides)
    return EvalCase(
        name=name,
        description=f"test case: {name}",
        definition=LoopDefinition.from_dict(definition_data),
        agent_script=agent_script,
        verifier_scripts=verifier_scripts,
        expect=expect,
    )


def test_run_case_success(tmp_path):
    case = _case(
        "success", {},
        agent_script=[{"changed": True, "cost_usd": 0.05}],
        verifier_scripts={"tests": [True]},
        expect={"final_state": "completed", "stop_reason": "completed", "iterations": 1},
    )

    outcome = run_case(case, events_dir=tmp_path)

    assert isinstance(outcome, EvalOutcome)
    assert outcome.passed is True
    assert outcome.actual["final_state"] == "completed"
    assert outcome.actual["stop_reason"] == "completed"
    assert outcome.actual["iterations"] == 1


def test_run_case_retry_then_succeed(tmp_path):
    case = _case(
        "retry", {},
        agent_script=[{"changed": True, "cost_usd": 0.05}, {"changed": True, "cost_usd": 0.05}],
        verifier_scripts={"tests": [False, True]},
        expect={"final_state": "completed", "stop_reason": "completed", "iterations": 2},
    )

    outcome = run_case(case, events_dir=tmp_path)

    assert outcome.passed is True


def test_run_case_no_progress_escalates(tmp_path):
    case = _case(
        "no-progress", {},
        agent_script=[{"changed": True, "cost_usd": 0.05}, {"changed": True, "cost_usd": 0.05}],
        verifier_scripts={"tests": [False, False]},
        expect={"final_state": "escalated", "stop_reason": "no_progress", "iterations": 2},
    )

    outcome = run_case(case, events_dir=tmp_path)

    assert outcome.passed is True


def test_run_case_budget_exceeded(tmp_path):
    case = _case(
        "budget", {"stop_conditions": {"max_cost_usd": 0.15}},
        agent_script=[{"changed": True, "cost_usd": 0.20}],
        verifier_scripts={"tests": [False]},
        # 2, not 1: LoopRuntime appends the iteration-1 retry-continuation
        # IterationResult, THEN a second STOPPED IterationResult when
        # iteration 2's pre-check catches the now-exceeded budget - the
        # agent/verifier are each only called once, but two IterationResults
        # land in the result either way.
        expect={"final_state": "stopped", "stop_reason": "budget_exceeded", "iterations": 2},
    )

    outcome = run_case(case, events_dir=tmp_path)

    assert outcome.passed is True


def test_run_case_unsafe_action_is_policy_denied(tmp_path):
    case = _case(
        "unsafe-action", {"actions": ["merge"]},
        agent_script=[],
        verifier_scripts={},
        expect={"final_state": "blocked", "stop_reason": "policy_denied"},
    )

    outcome = run_case(case, events_dir=tmp_path)

    assert outcome.passed is True
    assert outcome.actual == {"final_state": "blocked", "stop_reason": "policy_denied"}


def test_run_case_ambiguous_task_vacuously_completes(tmp_path):
    case = _case(
        "ambiguous-task", {},
        agent_script=[{"changed": True, "cost_usd": 0.0}],
        verifier_scripts={},
        expect={"final_state": "completed", "stop_reason": "completed", "iterations": 1},
    )

    outcome = run_case(case, events_dir=tmp_path)

    assert outcome.passed is True


def test_run_case_reports_a_mismatch_without_raising(tmp_path):
    case = _case(
        "wrong-expectation", {},
        agent_script=[{"changed": True, "cost_usd": 0.05}],
        verifier_scripts={"tests": [True]},
        expect={"final_state": "escalated", "stop_reason": "no_progress"},
    )

    outcome = run_case(case, events_dir=tmp_path)

    assert outcome.passed is False
    assert "escalated" in outcome.detail
    assert "completed" in outcome.detail


def test_run_case_lets_non_policy_exceptions_propagate(tmp_path):
    # Under-script the verifier, not the agent: LoopRuntime wraps its single
    # agent_fn call in a bare `except Exception`, so an agent-side
    # ScriptExhausted is swallowed into a normal agent_failed/FAILED
    # IterationResult rather than propagating - it can never reach run_case.
    # Verifier.verify() calls are not wrapped that way, so an empty
    # verifier script is what actually exercises this invariant.
    case = _case(
        "under-scripted", {},
        agent_script=[{"changed": True, "cost_usd": 0.05}],
        verifier_scripts={"tests": []},
        expect={"final_state": "completed", "stop_reason": "completed"},
    )

    with pytest.raises(ScriptExhausted):
        run_case(case, events_dir=tmp_path)


from loop_eval import run_all

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_CASES_DIR = REPO_ROOT / "evals" / "cases"


def test_run_all_every_shipped_case_passes():
    outcomes = run_all(REAL_CASES_DIR)

    failures = [o for o in outcomes if not o.passed]
    assert failures == [], f"eval cases failed: {[(f.case_name, f.detail) for f in failures]}"
    assert {o.case_name for o in outcomes} == {
        "success", "retry", "no-progress", "budget", "unsafe-action", "ambiguous-task",
    }
