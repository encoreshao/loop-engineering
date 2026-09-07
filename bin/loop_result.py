#!/usr/bin/env python3
"""IterationResult / LoopResult dataclasses - see
docs/superpowers/specs/2026-09-06-loop-runtime-foundation-design.md.
`IterationResult.progressed` is always True in this phase - a placeholder
field for the future no-progress detector (plan section 11), which is out
of scope here."""
from dataclasses import dataclass


@dataclass
class IterationResult:
    iteration: int
    state: object  # LoopState
    verification_results: list
    budget: dict
    progressed: bool


@dataclass
class LoopResult:
    loop_id: str
    run_id: str
    definition_name: str
    final_state: object  # LoopState
    iterations: list
    stop_reason: str
