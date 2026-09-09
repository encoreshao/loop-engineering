import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from loop_result import IterationResult, LoopResult
from loop_state import LoopState


def test_iteration_result_holds_expected_fields():
    iteration = IterationResult(
        iteration=1,
        state=LoopState.EVALUATING,
        verification_results=[],
        budget={"overall": "ok"},
        progressed=True,
    )

    assert iteration.iteration == 1
    assert iteration.state == LoopState.EVALUATING
    assert iteration.verification_results == []
    assert iteration.budget == {"overall": "ok"}
    assert iteration.progressed is True


def test_loop_result_holds_expected_fields():
    iteration = IterationResult(
        iteration=1, state=LoopState.COMPLETED, verification_results=[], budget={}, progressed=True
    )
    result = LoopResult(
        loop_id="loop_1",
        run_id="run_1",
        definition_name="gitlab-issue-fixer",
        final_state=LoopState.COMPLETED,
        iterations=[iteration],
        stop_reason="completed",
    )

    assert result.loop_id == "loop_1"
    assert result.run_id == "run_1"
    assert result.definition_name == "gitlab-issue-fixer"
    assert result.final_state == LoopState.COMPLETED
    assert result.iterations == [iteration]
    assert result.stop_reason == "completed"


def test_loop_result_prompt_and_definition_path_default_to_none():
    iteration = IterationResult(
        iteration=1, state=LoopState.COMPLETED, verification_results=[], budget={}, progressed=True
    )
    result = LoopResult(
        loop_id="loop_1", run_id="run_1", definition_name="gitlab-issue-fixer",
        final_state=LoopState.COMPLETED, iterations=[iteration], stop_reason="completed",
    )

    assert result.prompt is None
    assert result.definition_path is None


def test_loop_result_accepts_prompt_and_definition_path():
    iteration = IterationResult(
        iteration=1, state=LoopState.COMPLETED, verification_results=[], budget={}, progressed=True
    )
    result = LoopResult(
        loop_id="loop_1", run_id="run_1", definition_name="gitlab-issue-fixer",
        final_state=LoopState.COMPLETED, iterations=[iteration], stop_reason="completed",
        prompt="fix the bug", definition_path="/tmp/loop.yaml",
    )

    assert result.prompt == "fix the bug"
    assert result.definition_path == "/tmp/loop.yaml"


def test_loop_result_status_defaults_to_finished():
    iteration = IterationResult(
        iteration=1, state=LoopState.COMPLETED, verification_results=[], budget={}, progressed=True
    )
    result = LoopResult(
        loop_id="loop_1", run_id="run_1", definition_name="gitlab-issue-fixer",
        final_state=LoopState.COMPLETED, iterations=[iteration], stop_reason="completed",
    )

    assert result.status == "finished"


def test_loop_result_accepts_status():
    iteration = IterationResult(
        iteration=1, state=LoopState.COMPLETED, verification_results=[], budget={}, progressed=True
    )
    result = LoopResult(
        loop_id="loop_1", run_id="run_1", definition_name="gitlab-issue-fixer",
        final_state="running", iterations=[iteration], stop_reason="running",
        status="running",
    )

    assert result.status == "running"
