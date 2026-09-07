#!/usr/bin/env python3
"""LoopState enum and its explicit transition table - see
docs/superpowers/specs/2026-09-06-loop-runtime-foundation-design.md.
`transition()` raises rather than silently allowing an unlisted pair, so
"every loop must have explicit exits" is machine-checkable, not just a
convention `bin/loop_runtime.py` is trusted to follow."""
from enum import Enum


class LoopState(str, Enum):
    PENDING = "pending"
    DISCOVERING = "discovering"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    ESCALATED = "escalated"
    STOPPED = "stopped"


class InvalidTransition(ValueError):
    pass


VALID_TRANSITIONS = {
    LoopState.PENDING: {LoopState.DISCOVERING},
    LoopState.DISCOVERING: {LoopState.PLANNING},
    LoopState.PLANNING: {LoopState.EXECUTING},
    LoopState.EXECUTING: {LoopState.VERIFYING},
    LoopState.VERIFYING: {LoopState.EVALUATING},
    LoopState.EVALUATING: {
        LoopState.COMPLETED,
        LoopState.EXECUTING,
        LoopState.BLOCKED,
        LoopState.ESCALATED,
        LoopState.STOPPED,
        LoopState.FAILED,
    },
    LoopState.BLOCKED: {LoopState.ESCALATED},
    LoopState.COMPLETED: set(),
    LoopState.FAILED: set(),
    LoopState.ESCALATED: set(),
    LoopState.STOPPED: set(),
}


def can_transition(current, target):
    """True if `target` is a valid next state from `current`."""
    return target in VALID_TRANSITIONS.get(current, set())


def transition(current, target):
    """Return `target` if the transition is valid, else raise
    InvalidTransition naming both states."""
    if not can_transition(current, target):
        raise InvalidTransition(f"cannot transition from {current} to {target}")
    return target
