import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from loop_verifiers import CommandVerifier, DiffVerifier, VerificationResult, build_verifiers


def _run_git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('hi')\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-q", "-m", "initial commit")
    return repo


def test_passing_command_reports_passed():
    verifier = CommandVerifier(name="ok", command="true")

    result = verifier.verify({})

    assert isinstance(result, VerificationResult)
    assert result.name == "ok"
    assert result.passed is True
    assert result.exit_code == 0
    assert result.evidence == {"command": "true", "cwd": None}


def test_failing_command_reports_failed():
    verifier = CommandVerifier(name="broken", command="false")

    result = verifier.verify({})

    assert result.passed is False
    assert result.exit_code == 1


def test_output_captures_stdout_and_stderr():
    verifier = CommandVerifier(name="echo", command="echo hello")

    result = verifier.verify({})

    assert "hello" in result.output


def test_duration_ms_is_recorded():
    verifier = CommandVerifier(name="ok", command="true")

    result = verifier.verify({})

    assert isinstance(result.duration_ms, int)
    assert result.duration_ms >= 0


def test_cwd_is_used_and_recorded_in_evidence(tmp_path):
    marker = tmp_path / "marker.txt"
    marker.write_text("present")
    verifier = CommandVerifier(name="ls-check", command="test -f marker.txt", cwd=tmp_path)

    result = verifier.verify({})

    assert result.passed is True
    assert result.evidence["cwd"] == str(tmp_path)


def test_diff_verifier_passes_when_no_changes(tmp_path):
    repo = _make_repo(tmp_path)
    verifier = DiffVerifier(name="diff", allowed_paths=["src/"], cwd=repo)

    result = verifier.verify({})

    assert result.passed is True
    assert result.evidence["changed_files"] == []
    assert result.evidence["disallowed_files"] == []


def test_diff_verifier_passes_when_edit_is_within_allowed_paths(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "src" / "app.py").write_text("print('changed')\n")
    verifier = DiffVerifier(name="diff", allowed_paths=["src/"], cwd=repo)

    result = verifier.verify({})

    assert result.passed is True
    assert result.evidence["changed_files"] == ["src/app.py"]
    assert result.evidence["disallowed_files"] == []


def test_diff_verifier_fails_when_tracked_edit_is_outside_allowed_paths(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "src" / "app.py").write_text("print('changed')\n")
    verifier = DiffVerifier(name="diff", allowed_paths=["tests/"], cwd=repo)

    result = verifier.verify({})

    assert result.passed is False
    assert result.evidence["disallowed_files"] == ["src/app.py"]


def test_diff_verifier_fails_when_new_untracked_file_is_outside_allowed_paths(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "launchd_hack.plist").write_text("naughty\n")
    verifier = DiffVerifier(name="diff", allowed_paths=["src/"], cwd=repo)

    result = verifier.verify({})

    assert result.passed is False
    assert "launchd_hack.plist" in result.evidence["disallowed_files"]


def test_diff_verifier_empty_allowed_paths_fails_closed_on_any_change(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "src" / "app.py").write_text("print('changed')\n")
    verifier = DiffVerifier(name="diff", allowed_paths=[], cwd=repo)

    result = verifier.verify({})

    assert result.passed is False
    assert result.evidence["disallowed_files"] == ["src/app.py"]


def test_build_verifiers_builds_command_verifier():
    verifiers = build_verifiers([{"name": "tests", "type": "command", "command": "true"}])

    assert len(verifiers) == 1
    assert isinstance(verifiers[0], CommandVerifier)
    assert verifiers[0].name == "tests"
    assert verifiers[0].command == "true"


def test_build_verifiers_builds_diff_verifier(tmp_path):
    verifiers = build_verifiers(
        [{"name": "diff", "type": "git_diff", "allowed_paths": ["src/"]}], cwd=tmp_path
    )

    assert len(verifiers) == 1
    assert isinstance(verifiers[0], DiffVerifier)
    assert verifiers[0].name == "diff"
    assert verifiers[0].allowed_paths == ["src/"]
    assert verifiers[0].cwd == tmp_path


def test_build_verifiers_builds_multiple_in_order():
    verifiers = build_verifiers(
        [
            {"name": "tests", "type": "command", "command": "true"},
            {"name": "diff", "type": "git_diff", "allowed_paths": ["src/"]},
        ]
    )

    assert [v.name for v in verifiers] == ["tests", "diff"]


def test_build_verifiers_raises_on_unknown_type():
    with pytest.raises(ValueError, match="unknown verifier type"):
        build_verifiers([{"name": "mystery", "type": "http"}])


def test_build_verifiers_raises_when_command_missing_for_command_type():
    with pytest.raises(ValueError, match="command"):
        build_verifiers([{"name": "tests", "type": "command"}])


def test_build_verifiers_raises_when_allowed_paths_missing_for_git_diff_type():
    with pytest.raises(ValueError, match="allowed_paths"):
        build_verifiers([{"name": "diff", "type": "git_diff"}])
