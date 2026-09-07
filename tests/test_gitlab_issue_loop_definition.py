import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from loop_definition import LoopDefinition

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFINITION_PATH = REPO_ROOT / "loops" / "gitlab-issue" / "loop.yaml"


def test_real_definition_has_no_verifiers_and_retry_disabled():
    definition = LoopDefinition.from_yaml(DEFINITION_PATH)

    assert definition.name == "gitlab-issue-loop"
    assert definition.verifiers == []
    assert definition.retry.enabled is False
    assert definition.stop_conditions.max_iterations == 1
    # 30, not an invented number: it matches this repo's own
    # LoopDefinition/BudgetController default and
    # templates/gitlab-issue/loop.yaml, and bounds each issue independently
    # inside run-loop.sh's outer 21600s whole-batch sanity timeout.
    assert definition.stop_conditions.max_runtime_minutes == 30
    assert definition.stop_conditions.max_cost_usd == 3
    assert definition.human_gates == ["merge", "production_deploy"]


def test_real_definition_passes_loop_cli_validate():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "loop_cli.py"), "validate", str(DEFINITION_PATH)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "Loop configuration valid." in result.stdout


def test_real_definition_audit_honestly_flags_the_verification_gap():
    """loop audit legitimately FAILs the verification check for this
    definition (a code-mutating action, modify_code, with no configured
    verifiers) - this is the "known, accepted limitation" from the design
    doc surfacing correctly, not a bug to hide from the audit tool."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "loop_cli.py"), "audit", str(DEFINITION_PATH)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "verification" in result.stdout
    assert "FAIL" in result.stdout
    assert "Loop Ready Score" in result.stdout
