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
