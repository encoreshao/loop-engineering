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
