import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from loop_definition import LoopDefinition  # noqa: E402
from loop_policy import PolicyEngine, RiskLevel  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFINITION_PATH = REPO_ROOT / "loops" / "inbox-triage" / "loop.yaml"


def test_real_definition_shape():
    definition = LoopDefinition.from_yaml(DEFINITION_PATH)
    assert definition.name == "inbox-triage-loop"
    assert definition.actions == ["read_mailbox", "classify_messages", "label_messages", "create_reply_drafts"]
    assert definition.verifiers == []
    assert definition.retry.enabled is False
    assert definition.stop_conditions.max_iterations == 1
    assert definition.stop_conditions.max_runtime_minutes == 15


def test_real_definition_passes_policy_and_never_declares_send():
    definition = LoopDefinition.from_yaml(DEFINITION_PATH)
    assert PolicyEngine().validate_definition(definition) == []
    assert "send_email" not in definition.actions


def test_send_email_is_irreversible():
    assert PolicyEngine().risk_level_for("send_email") == RiskLevel.L3_IRREVERSIBLE
    assert PolicyEngine().risk_level_for("label_messages") == RiskLevel.L2_EXTERNAL_CHANGE
    assert PolicyEngine().risk_level_for("create_reply_drafts") == RiskLevel.L2_EXTERNAL_CHANGE
    assert PolicyEngine().risk_level_for("read_mailbox") == RiskLevel.L0_READ_ONLY


def test_real_definition_passes_loop_cli_validate_and_audit():
    for command in ("validate", "audit"):
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "bin" / "loop_cli.py"), command, str(DEFINITION_PATH)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
