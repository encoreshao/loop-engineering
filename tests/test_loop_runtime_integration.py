"""End-to-end integration test for LoopRuntime - the first real (not
faked/mocked) caller of the V2 runtime foundation built across
docs/superpowers/specs/2026-09-0{6,7}-*.md. Deliberately L0/observe-only
(plan section 32): the agent_fn makes no changes, the verifier is a real
CommandVerifier subprocess running a subset of this repo's own test
suite, nothing GitLab/production-facing is touched. Proves the full
stack (policy check -> budget -> state machine -> real subprocess
verification -> COMPLETED) works together, not just each module in
isolation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from loop_definition import LoopDefinition
from loop_runtime import LoopRuntime
from loop_state import LoopState
from loop_verifiers import CommandVerifier

REPO_ROOT = Path(__file__).resolve().parent.parent

# Deliberately excludes this file itself and test_loop_runtime.py (avoid a
# test recursively re-invoking the suite it's part of) - everything else
# in the new V2 foundation is fair game for a real, fast (~seconds) check.
_FOUNDATION_TEST_FILES = (
    "tests/test_loop_state.py",
    "tests/test_loop_definition.py",
    "tests/test_loop_verifiers.py",
    "tests/test_loop_budget.py",
    "tests/test_loop_result.py",
    "tests/test_loop_progress.py",
    "tests/test_loop_policy.py",
)


def _definition():
    return LoopDefinition.from_dict(
        {
            "name": "loop-foundation-selfcheck",
            "version": 1,
            "trigger": {"type": "manual"},
            "goal": {"type": "self_check"},
            "actions": ["run_tests"],
            "verification": {"required": ["loop_foundation_tests"]},
            "stop_conditions": {
                "max_iterations": 1,
                "max_runtime_minutes": 5,
                "max_cost_usd": 1,
                "no_progress_iterations": 1,
            },
            "retry": {"enabled": False, "max_attempts": 1},
        }
    )


def _foundation_tests_verifier():
    command = f"{sys.executable} -m pytest {' '.join(_FOUNDATION_TEST_FILES)} -q"
    return CommandVerifier(name="loop_foundation_tests", command=command, cwd=REPO_ROOT)


def test_loop_runtime_end_to_end_against_the_real_foundation_test_suite(tmp_path):
    runtime = LoopRuntime(
        agent_fn=lambda context: {"changed": False},
        verifiers=[_foundation_tests_verifier()],
        events_dir=tmp_path,
    )

    result = runtime.start(_definition(), run_id="run_integration_selfcheck")

    assert result.final_state == LoopState.COMPLETED
    assert result.stop_reason == "completed"
    assert len(result.iterations) == 1
    verification_result = result.iterations[0].verification_results[0]
    assert verification_result.name == "loop_foundation_tests"
    assert verification_result.passed is True, verification_result.output
