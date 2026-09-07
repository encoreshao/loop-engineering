import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from loop_progress import ProgressDetector
from loop_result import IterationResult
from loop_state import LoopState
from loop_verifiers import VerificationResult


def _iteration(results, iteration=1):
    return IterationResult(
        iteration=iteration, state=LoopState.EVALUATING, verification_results=results, budget={}, progressed=False
    )


def _failing(name="tests", exit_code=1):
    return VerificationResult(name=name, passed=False, exit_code=exit_code, duration_ms=1, output="", evidence={})


def test_first_iteration_always_progressed():
    detector = ProgressDetector()

    result = detector.compare(None, _iteration([_failing()]))

    assert result.progressed is True
    assert result.reason == "first_iteration"


def test_same_failure_signature_is_not_progressed():
    detector = ProgressDetector()
    previous = _iteration([_failing(name="pytest", exit_code=1)], iteration=1)
    current = _iteration([_failing(name="pytest", exit_code=1)], iteration=2)

    result = detector.compare(previous, current)

    assert result.progressed is False
    assert result.reason == "same_verification_failure"


def test_different_verifier_name_is_progressed():
    detector = ProgressDetector()
    previous = _iteration([_failing(name="pytest", exit_code=1)], iteration=1)
    current = _iteration([_failing(name="lint", exit_code=1)], iteration=2)

    result = detector.compare(previous, current)

    assert result.progressed is True
    assert result.reason == "different_result"


def test_same_name_different_exit_code_is_progressed():
    detector = ProgressDetector()
    previous = _iteration([_failing(name="pytest", exit_code=1)], iteration=1)
    current = _iteration([_failing(name="pytest", exit_code=2)], iteration=2)

    result = detector.compare(previous, current)

    assert result.progressed is True


def test_signature_comparison_is_order_independent():
    detector = ProgressDetector()
    previous = _iteration([_failing(name="a", exit_code=1), _failing(name="b", exit_code=1)], iteration=1)
    current = _iteration([_failing(name="b", exit_code=1), _failing(name="a", exit_code=1)], iteration=2)

    result = detector.compare(previous, current)

    assert result.progressed is False
