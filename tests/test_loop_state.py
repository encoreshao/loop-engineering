import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import loop_state
from loop_state import LoopState, InvalidTransition, transition, can_transition


VALID_PAIRS = [
    (LoopState.PENDING, LoopState.DISCOVERING),
    (LoopState.DISCOVERING, LoopState.PLANNING),
    (LoopState.PLANNING, LoopState.EXECUTING),
    (LoopState.EXECUTING, LoopState.VERIFYING),
    (LoopState.VERIFYING, LoopState.EVALUATING),
    (LoopState.EVALUATING, LoopState.COMPLETED),
    (LoopState.EVALUATING, LoopState.EXECUTING),
    (LoopState.EVALUATING, LoopState.BLOCKED),
    (LoopState.EVALUATING, LoopState.ESCALATED),
    (LoopState.EVALUATING, LoopState.STOPPED),
    (LoopState.EVALUATING, LoopState.FAILED),
    (LoopState.BLOCKED, LoopState.ESCALATED),
]

INVALID_PAIRS = [
    (LoopState.PENDING, LoopState.EXECUTING),
    (LoopState.COMPLETED, LoopState.EXECUTING),
    (LoopState.FAILED, LoopState.PENDING),
    (LoopState.STOPPED, LoopState.EVALUATING),
    (LoopState.ESCALATED, LoopState.COMPLETED),
    (LoopState.DISCOVERING, LoopState.EVALUATING),
]


@pytest.mark.parametrize("current,target", VALID_PAIRS)
def test_valid_transitions_succeed(current, target):
    assert can_transition(current, target) is True
    assert transition(current, target) == target


@pytest.mark.parametrize("current,target", INVALID_PAIRS)
def test_invalid_transitions_raise(current, target):
    assert can_transition(current, target) is False
    with pytest.raises(InvalidTransition):
        transition(current, target)


def test_terminal_states_have_no_outgoing_transitions():
    terminal = {
        LoopState.COMPLETED,
        LoopState.FAILED,
        LoopState.STOPPED,
        LoopState.ESCALATED,
    }
    for state in terminal:
        assert loop_state.VALID_TRANSITIONS.get(state, set()) == set()
