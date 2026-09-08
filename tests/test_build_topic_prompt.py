import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "bin" / "scripts" / "build_topic_prompt.sh"


def test_zero_args_produces_the_legacy_full_batch_prompt():
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, check=True)
    assert "TOPIC_MONITOR_INSTRUCTIONS.md" in result.stdout
    assert "scheduled headless run" in result.stdout


def test_one_arg_produces_a_scoped_single_topic_prompt():
    result = subprocess.run(["bash", str(SCRIPT), "ai-news"], capture_output=True, text=True, check=True)
    assert "skip Step 1" in result.stdout
    assert "'ai-news'" in result.stdout
    assert "topic_config.py topic ai-news" in result.stdout


def test_one_arg_prompt_scopes_the_verification_checklist_to_this_topic():
    """TOPIC_MONITOR_INSTRUCTIONS.md's "Verification checklist" has two
    whole-run bullets ("every topic has a briefing", "every topic's status
    is idle or failed") that a single-topic session structurally cannot
    satisfy. The instructions file now scopes them by mode, and the prompt
    says so too - mirroring how build_run_prompt.sh's own --batch-issue
    prompt explicitly tells the agent NOT to do the batch-level step."""
    result = subprocess.run(["bash", str(SCRIPT), "ai-news"], capture_output=True, text=True, check=True)
    assert "verification checklist" in result.stdout
    assert "only to this one topic" in result.stdout


def test_wrong_arg_count_is_rejected():
    result = subprocess.run(["bash", str(SCRIPT), "a", "b"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "Usage" in result.stderr
