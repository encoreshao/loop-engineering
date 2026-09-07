#!/usr/bin/env python3
"""ProgressDetector - see
docs/superpowers/specs/2026-09-07-no-progress-detector-design.md. Answers
a single question per call: does `current_iteration` repeat the same
failing-verifier signature as `previous_iteration`? `LoopRuntime` is the
one that accumulates this into a streak against
`stop_conditions.no_progress_iterations` - this module stays a pure,
stateless comparison, matching the plan's own
`compare(previous_iteration, current_iteration) -> ProgressResult`
interface (section 11)."""
from dataclasses import dataclass


@dataclass
class ProgressResult:
    progressed: bool
    reason: str


def _failure_signature(verification_results):
    return frozenset((r.name, r.exit_code) for r in verification_results if not r.passed)


class ProgressDetector:
    def compare(self, previous_iteration, current_iteration):
        if previous_iteration is None:
            return ProgressResult(progressed=True, reason="first_iteration")

        previous_signature = _failure_signature(previous_iteration.verification_results)
        current_signature = _failure_signature(current_iteration.verification_results)

        if current_signature and current_signature == previous_signature:
            return ProgressResult(progressed=False, reason="same_verification_failure")

        return ProgressResult(progressed=True, reason="different_result")
