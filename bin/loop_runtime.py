#!/usr/bin/env python3
"""LoopRuntime orchestration - see
docs/superpowers/specs/2026-09-06-loop-runtime-foundation-design.md. Does
NOT drive the real GitLab issue loop yet (see the spec's "Constraint that
shapes this design" section) - `agent_fn` and `verifiers` are injected so
this module has no GitLab/Claude/Codex/Slack dependency of its own.
Emits through the existing bin/events.py event log rather than a new
store; every emit is best-effort, matching run-loop.sh's own `|| true`
philosophy - observability must never crash the runtime."""
import time
import uuid

import events as _default_events_module
from loop_budget import BudgetController, BudgetStatus
from loop_policy import PolicyEngine, PolicyViolationError
from loop_progress import ProgressDetector
from loop_result import IterationResult, LoopResult
from loop_state import LoopState, transition


def _can_retry(retry_config, iteration_number):
    return retry_config.enabled and iteration_number < retry_config.max_attempts


class LoopRuntime:
    def __init__(
        self, agent_fn, verifiers, progress_detector=None, policy_engine=None, events_module=None, events_dir=None
    ):
        self.agent_fn = agent_fn
        self.verifiers = verifiers
        self.progress_detector = progress_detector if progress_detector is not None else ProgressDetector()
        self.policy_engine = policy_engine if policy_engine is not None else PolicyEngine()
        self.events_module = events_module if events_module is not None else _default_events_module
        self.events_dir = events_dir

    def _emit(self, event_type, run_id, data=None):
        try:
            self.events_module.emit(event_type, run_id, data=data, events_dir=self.events_dir)
        except Exception:
            pass

    def start(self, definition, run_id, loop_id=None):
        loop_id = loop_id or f"loop_{uuid.uuid4().hex}"

        violations = self.policy_engine.validate_definition(definition)
        if violations:
            self._emit(
                "policy.denied",
                run_id,
                data={"loop_id": loop_id, "violations": [v.action for v in violations]},
            )
            raise PolicyViolationError(violations)
        self._emit("policy.allowed", run_id, data={"loop_id": loop_id})

        budget_controller = BudgetController(definition.stop_conditions)

        self._emit("loop.started", run_id, data={"definition": definition.name, "loop_id": loop_id})

        state = LoopState.PENDING
        state = transition(state, LoopState.DISCOVERING)
        state = transition(state, LoopState.PLANNING)

        iterations = []
        completed_iterations = 0
        total_cost_usd = 0.0
        loop_start = time.monotonic()
        iteration_number = 1
        final_state = None
        stop_reason = None
        previous = None
        same_failure_streak = 0

        while True:
            elapsed = time.monotonic() - loop_start
            budget_check = budget_controller.check(completed_iterations, elapsed, total_cost_usd)

            state = transition(state, LoopState.EXECUTING)

            if budget_check["overall"] == BudgetStatus.EXCEEDED:
                state = transition(state, LoopState.VERIFYING)
                state = transition(state, LoopState.EVALUATING)
                state = transition(state, LoopState.STOPPED)
                iterations.append(
                    IterationResult(
                        iteration=iteration_number,
                        state=LoopState.STOPPED,
                        verification_results=[],
                        budget=budget_check,
                        progressed=False,
                    )
                )
                final_state = LoopState.STOPPED
                stop_reason = "budget_exceeded"
                break

            context = {"iteration": iteration_number, "definition": definition, "previous": previous}
            self._emit("iteration.started", run_id, data={"iteration": iteration_number})

            agent_failed = False
            try:
                agent_result = self.agent_fn(context)
            except Exception:
                agent_failed = True
                agent_result = None

            if isinstance(agent_result, dict):
                total_cost_usd += agent_result.get("cost_usd") or 0

            state = transition(state, LoopState.VERIFYING)
            all_verifiers_passed = True
            if agent_failed:
                verification_results = []
            else:
                self._emit("verification.started", run_id, data={"iteration": iteration_number})
                verification_results = [v.verify(context) for v in self.verifiers]
                all_verifiers_passed = all(r.passed for r in verification_results) if verification_results else True
                # verification.passed/verification.failed (not
                # verification.completed+data.passed) to match the
                # existing event vocabulary that
                # docs/superpowers/specs/2026-09-04-event-system-design.md
                # and bin/metrics.py already define/consume.
                self._emit(
                    "verification.passed" if all_verifiers_passed else "verification.failed",
                    run_id,
                    data={"iteration": iteration_number},
                )
            state = transition(state, LoopState.EVALUATING)

            completed_iterations += 1
            elapsed = time.monotonic() - loop_start
            iteration_budget = budget_controller.check(completed_iterations, elapsed, total_cost_usd)

            if agent_failed:
                state = transition(state, LoopState.FAILED)
                iteration_result = IterationResult(
                    iteration=iteration_number,
                    state=LoopState.FAILED,
                    verification_results=verification_results,
                    budget=iteration_budget,
                    progressed=False,
                )
                iterations.append(iteration_result)
                self._emit("iteration.completed", run_id, data={"iteration": iteration_number, "state": "failed"})
                final_state = LoopState.FAILED
                stop_reason = "agent_failed"
                break

            if all_verifiers_passed:
                state = transition(state, LoopState.COMPLETED)
                iteration_result = IterationResult(
                    iteration=iteration_number,
                    state=LoopState.COMPLETED,
                    verification_results=verification_results,
                    budget=iteration_budget,
                    progressed=True,
                )
                iterations.append(iteration_result)
                self._emit("iteration.completed", run_id, data={"iteration": iteration_number, "state": "completed"})
                final_state = LoopState.COMPLETED
                stop_reason = "completed"
                break

            iteration_result = IterationResult(
                iteration=iteration_number,
                state=LoopState.EVALUATING,
                verification_results=verification_results,
                budget=iteration_budget,
                progressed=False,
            )

            progress = self.progress_detector.compare(previous, iteration_result)
            iteration_result.progressed = progress.progressed
            same_failure_streak = 1 if progress.progressed else same_failure_streak + 1

            if same_failure_streak >= definition.stop_conditions.no_progress_iterations:
                state = transition(state, LoopState.ESCALATED)
                iteration_result.state = LoopState.ESCALATED
                iterations.append(iteration_result)
                self._emit(
                    "iteration.completed",
                    run_id,
                    data={"iteration": iteration_number, "state": "escalated", "reason": "no_progress"},
                )
                final_state = LoopState.ESCALATED
                stop_reason = "no_progress"
                break

            if _can_retry(definition.retry, iteration_number):
                iterations.append(iteration_result)
                self._emit("iteration.completed", run_id, data={"iteration": iteration_number, "state": "retry"})
                previous = iteration_result
                iteration_number += 1
                continue

            state = transition(state, LoopState.ESCALATED)
            iteration_result.state = LoopState.ESCALATED
            iterations.append(iteration_result)
            self._emit(
                "iteration.completed",
                run_id,
                data={"iteration": iteration_number, "state": "escalated", "reason": "retries_exhausted"},
            )
            final_state = LoopState.ESCALATED
            stop_reason = "escalated"
            break

        terminal_event = {
            LoopState.COMPLETED: "loop.completed",
            LoopState.FAILED: "loop.failed",
            LoopState.STOPPED: "loop.stopped",
            LoopState.ESCALATED: "loop.stopped",
        }[final_state]
        self._emit(terminal_event, run_id, data={"final_state": final_state.value, "stop_reason": stop_reason})

        return LoopResult(
            loop_id=loop_id,
            run_id=run_id,
            definition_name=definition.name,
            final_state=final_state,
            iterations=iterations,
            stop_reason=stop_reason,
        )
