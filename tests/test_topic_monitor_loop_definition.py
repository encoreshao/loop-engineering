import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from loop_definition import LoopDefinition

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFINITION_PATH = REPO_ROOT / "loops" / "topic-monitor" / "loop.yaml"


def test_real_definition_has_no_verifiers_and_retry_disabled():
    definition = LoopDefinition.from_yaml(DEFINITION_PATH)

    assert definition.name == "topic-monitor-loop"
    assert definition.verifiers == []
    assert definition.retry.enabled is False
    assert definition.stop_conditions.max_iterations == 1
    assert definition.stop_conditions.max_runtime_minutes == 30
    assert definition.human_gates == []
    assert definition.actions == ["research_topic", "write_briefing"]


def test_real_definition_passes_loop_cli_validate():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "loop_cli.py"), "validate", str(DEFINITION_PATH)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "Loop configuration valid." in result.stdout


def test_real_definition_passes_loop_cli_audit_cleanly():
    """Unlike the GitLab loop's definition, this one has NO code-mutating
    action, so bin/loop_audit.py's existing "nothing to verify" branch
    should let this pass audit cleanly (exit 0) - verified here, not
    assumed, per the design doc's own explicit instruction."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "loop_cli.py"), "audit", str(DEFINITION_PATH)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "FAIL" not in result.stdout
