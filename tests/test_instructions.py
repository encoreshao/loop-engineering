import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import instructions  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_builtin_files_live_in_one_folder():
    for loop in instructions.LOOPS:
        assert instructions.builtin_path(loop) == REPO_ROOT / "instructions" / f"{loop}.md"
        assert instructions.builtin_path(loop).is_file()


def test_no_stray_instructions_files_at_repo_root():
    assert not list(REPO_ROOT.glob("*_INSTRUCTIONS.md"))


def test_custom_paths_are_global_then_per_loop(tmp_path):
    assert instructions.custom_paths("topic-monitor", home=tmp_path) == [
        tmp_path / "instructions.md",
        tmp_path / "instructions" / "topic-monitor.md",
    ]


def test_custom_paths_reject_unknown_loop(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        instructions.custom_paths("../evil", home=tmp_path)


def test_read_custom_layers_global_then_per_loop_and_skips_empty(tmp_path):
    (tmp_path / "instructions").mkdir()
    (tmp_path / "instructions.md").write_text("global rule\n")
    (tmp_path / "instructions" / "inbox-triage.md").write_text("   \n")
    assert instructions.read_custom("inbox-triage", home=tmp_path) == "global rule"
    (tmp_path / "instructions" / "inbox-triage.md").write_text("loop rule")
    assert instructions.read_custom("inbox-triage", home=tmp_path) == "global rule\n\nloop rule"


def test_read_custom_missing_files_is_empty(tmp_path):
    assert instructions.read_custom("gitlab-issue", home=tmp_path) == ""


def test_compose_appends_custom_after_builtin(tmp_path):
    (tmp_path / "instructions.md").write_text("be terse")
    text = instructions.compose("inbox-triage", home=tmp_path)
    assert text.startswith(instructions.builtin_path("inbox-triage").read_text())
    assert text.rstrip().endswith("be terse")
