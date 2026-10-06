import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from loop_definition import LoopDefinition

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFINITION_PATH = REPO_ROOT / "loops" / "gitlab-issue" / "loop.yaml"


def test_real_definition_verifies_externally_in_observe_mode_with_one_retry():
    definition = LoopDefinition.from_yaml(DEFINITION_PATH)

    assert definition.name == "gitlab-issue-loop"
    assert definition.verifiers == [{"name": "project_commands", "type": "project_commands"}]
    assert definition.verification.required == ["project_commands"]
    assert definition.verification.mode == "observe"
    assert definition.retry.enabled is True and definition.retry.max_attempts == 2
    assert definition.stop_conditions.max_iterations == 2
    # 30, not an invented number: it matches this repo's own
    # LoopDefinition/BudgetController default and
    # templates/gitlab-issue/loop.yaml, and bounds each issue independently
    # inside the scheduler's outer 43200s whole-batch sanity timeout.
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


def test_real_definition_audit_warns_on_observe_mode_verification():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "loop_cli.py"), "audit", str(DEFINITION_PATH)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "WARN  verification" in result.stdout
    assert "project_commands verifier" in result.stdout
