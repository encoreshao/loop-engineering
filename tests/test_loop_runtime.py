import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from loop_runtime import LoopRuntime
from loop_definition import LoopDefinition
from loop_policy import PolicyViolationError
from loop_state import LoopState
from loop_verifiers import VerificationResult


def _definition(
    max_iterations=10,
    max_runtime_minutes=30,
    max_cost_usd=5,
    max_attempts=2,
    retry_enabled=True,
    actions=None,
    human_gates=None,
):
    data = {
        "name": "test-loop",
        "version": 1,
        "trigger": {"type": "manual"},
        "goal": {"type": "issue_resolution"},
        "stop_conditions": {
            "max_iterations": max_iterations,
            "max_runtime_minutes": max_runtime_minutes,
            "max_cost_usd": max_cost_usd,
            "no_progress_iterations": 2,
        },
        "retry": {"enabled": retry_enabled, "max_attempts": max_attempts},
        "actions": actions or [],
        "human_gates": human_gates or [],
    }
    return LoopDefinition.from_dict(data)


class FakeVerifier:
    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def verify(self, context):
        self.calls += 1
        return self._results.pop(0)


def _passing_result(name="tests"):
    return VerificationResult(name=name, passed=True, exit_code=0, duration_ms=1, output="", evidence={})


def _failing_result(name="tests"):
    return VerificationResult(name=name, passed=False, exit_code=1, duration_ms=1, output="", evidence={})


def test_success_path_completes_after_verifiers_pass(tmp_path):
    definition = _definition()
    verifier = FakeVerifier([_passing_result()])
    runtime = LoopRuntime(agent_fn=lambda context: {"changed": True}, verifiers=[verifier], events_dir=tmp_path)

    result = runtime.start(definition, run_id="run_test_1")

    assert result.final_state == LoopState.COMPLETED
    assert result.stop_reason == "completed"
    assert len(result.iterations) == 1
    assert verifier.calls == 1


def test_retry_path_fails_once_then_passes(tmp_path):
    definition = _definition(max_attempts=2)
    verifier = FakeVerifier([_failing_result(), _passing_result()])
    runtime = LoopRuntime(agent_fn=lambda context: {"changed": True}, verifiers=[verifier], events_dir=tmp_path)

    result = runtime.start(definition, run_id="run_test_2")

    assert result.final_state == LoopState.COMPLETED
    assert len(result.iterations) == 2
    assert verifier.calls == 2


def test_escalation_path_after_exhausting_retries(tmp_path):
    # Two *different* failures (not the same verifier/exit_code repeating)
    # so this exercises "retries genuinely exhausted", not the no-progress
    # detector - see test_no_progress_escalates_before_retries_exhausted
    # for that path.
    definition = _definition(max_attempts=2)
    verifier = FakeVerifier([_failing_result(name="tests"), _failing_result(name="lint")])
    runtime = LoopRuntime(agent_fn=lambda context: {"changed": True}, verifiers=[verifier], events_dir=tmp_path)

    result = runtime.start(definition, run_id="run_test_3")

    assert result.final_state == LoopState.ESCALATED
    assert result.stop_reason == "escalated"
    assert len(result.iterations) == 2


def test_no_progress_escalates_before_retries_exhausted(tmp_path):
    # Plenty of retry budget (max_attempts=5), but the same failure twice
    # in a row should trip the no-progress detector (default
    # no_progress_iterations=2) and escalate early rather than retrying
    # up to the retry budget.
    definition = _definition(max_attempts=5)
    verifier = FakeVerifier([_failing_result(name="tests"), _failing_result(name="tests")])
    runtime = LoopRuntime(agent_fn=lambda context: {"changed": True}, verifiers=[verifier], events_dir=tmp_path)

    result = runtime.start(definition, run_id="run_test_7")

    assert result.final_state == LoopState.ESCALATED
    assert result.stop_reason == "no_progress"
    assert len(result.iterations) == 2
    assert verifier.calls == 2


def test_budget_exceeded_stops_without_calling_verifiers(tmp_path):
    definition = _definition(max_iterations=0)
    verifier = FakeVerifier([_passing_result()])
    runtime = LoopRuntime(agent_fn=lambda context: {"changed": True}, verifiers=[verifier], events_dir=tmp_path)

    result = runtime.start(definition, run_id="run_test_4")

    assert result.final_state == LoopState.STOPPED
    assert result.stop_reason == "budget_exceeded"
    assert verifier.calls == 0


def test_agent_failure_results_in_failed_state(tmp_path):
    definition = _definition()

    def _raising_agent(context):
        raise RuntimeError("boom")

    verifier = FakeVerifier([])
    runtime = LoopRuntime(agent_fn=_raising_agent, verifiers=[verifier], events_dir=tmp_path)

    result = runtime.start(definition, run_id="run_test_5")

    assert result.final_state == LoopState.FAILED
    assert result.stop_reason == "agent_failed"
    assert verifier.calls == 0


def test_policy_violation_prevents_start_and_never_calls_agent_or_verifiers(tmp_path):
    definition = _definition(actions=["merge"], human_gates=[])
    verifier = FakeVerifier([_passing_result()])
    agent_calls = []
    runtime = LoopRuntime(
        agent_fn=lambda context: agent_calls.append(1) or {"changed": True},
        verifiers=[verifier],
        events_dir=tmp_path,
    )

    with pytest.raises(PolicyViolationError):
        runtime.start(definition, run_id="run_test_policy_1")

    assert agent_calls == []
    assert verifier.calls == 0


def test_gated_l3_action_allows_start(tmp_path):
    definition = _definition(actions=["merge"], human_gates=["merge"])
    verifier = FakeVerifier([_passing_result()])
    runtime = LoopRuntime(agent_fn=lambda context: {"changed": True}, verifiers=[verifier], events_dir=tmp_path)

    result = runtime.start(definition, run_id="run_test_policy_2")

    assert result.final_state == LoopState.COMPLETED


def test_events_are_emitted_to_injected_events_dir(tmp_path):
    definition = _definition()
    verifier = FakeVerifier([_passing_result()])
    runtime = LoopRuntime(agent_fn=lambda context: {"changed": True}, verifiers=[verifier], events_dir=tmp_path)

    runtime.start(definition, run_id="run_test_6")

    written = list(tmp_path.glob("*.jsonl"))
    assert len(written) == 1
    lines = [json.loads(line) for line in written[0].read_text().splitlines()]
    event_types = [e["event_type"] for e in lines]
    assert "loop.started" in event_types
    assert "loop.completed" in event_types
    assert all(e["run_id"] == "run_test_6" for e in lines)


def test_on_iteration_fires_once_per_completed_iteration():
    definition = _definition(max_iterations=5)
    verifier = FakeVerifier([_passing_result()])
    calls = []

    def record(run_id, loop_id, definition_name, iterations):
        calls.append((run_id, loop_id, definition_name, len(iterations)))

    runtime = LoopRuntime(
        agent_fn=lambda context: {"changed": True}, verifiers=[verifier], on_iteration=record,
    )

    result = runtime.start(definition, run_id="run_test_on_iteration")

    assert len(calls) == 1  # one successful iteration -> one call
    run_id, loop_id, definition_name, iteration_count = calls[0]
    assert run_id == "run_test_on_iteration"
    assert loop_id == result.loop_id
    assert definition_name == "test-loop"
    assert iteration_count == 1


def test_on_iteration_receives_a_snapshot_not_the_live_list():
    definition = _definition(max_iterations=5)
    verifier = FakeVerifier([_passing_result()])
    snapshots = []
    runtime = LoopRuntime(
        agent_fn=lambda context: {"changed": True}, verifiers=[verifier],
        on_iteration=lambda run_id, loop_id, definition_name, iterations: snapshots.append(iterations),
    )

    result = runtime.start(definition, run_id="run_test_snapshot")

    assert snapshots[0] is not result.iterations
    assert snapshots[0] == result.iterations


def test_no_on_iteration_callback_is_a_safe_default():
    definition = _definition(max_iterations=5)
    verifier = FakeVerifier([_passing_result()])
    runtime = LoopRuntime(agent_fn=lambda context: {"changed": True}, verifiers=[verifier])

    result = runtime.start(definition, run_id="run_test_no_callback")

    assert result.final_state == LoopState.COMPLETED


def test_on_iteration_exception_is_swallowed():
    definition = _definition(max_iterations=5)
    verifier = FakeVerifier([_passing_result()])

    def boom(run_id, loop_id, definition_name, iterations):
        raise RuntimeError("boom")

    runtime = LoopRuntime(agent_fn=lambda context: {"changed": True}, verifiers=[verifier], on_iteration=boom)

    result = runtime.start(definition, run_id="run_test_boom")

    assert result.final_state == LoopState.COMPLETED
