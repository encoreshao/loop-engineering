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


def test_command_exceeding_timeout_seconds_reports_failed_not_raised():
    verifier = CommandVerifier(name="slow", command="sleep 2", timeout_seconds=0.2)

    result = verifier.verify({})

    assert result.passed is False
    assert result.exit_code is None
    assert result.evidence["timed_out"] is True


def test_command_exceeding_timeout_with_output_on_both_streams_reports_failed_not_raised():
    verifier = CommandVerifier(
        name="slow", command="bash -c 'echo out; echo err >&2; sleep 2'", timeout_seconds=0.2,
    )

    result = verifier.verify({})

    assert result.passed is False
    assert isinstance(result.output, str)
    assert "out" in result.output
    assert "err" in result.output


def test_evidence_includes_timeout_seconds_when_configured():
    verifier = CommandVerifier(name="ok", command="true", timeout_seconds=5)

    result = verifier.verify({})

    assert result.passed is True
    assert result.evidence["timeout_seconds"] == 5


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


import loop_verifiers as lv


def _project(tmp_path, test_cmd="true", lint_cmd="true"):
    return {"local_path": str(tmp_path / "repo"), "test_cmd": test_cmd, "lint_cmd": lint_cmd}


def test_no_worktree_is_vacuous_pass(tmp_path):
    v = lv.ProjectCommandsVerifier("pc", "web", 7, 30, project_fn=lambda a: _project(tmp_path),
                                   worktree_root_fn=lambda: tmp_path / "wt")
    r = v.verify({})
    assert r.passed and r.evidence["vacuous"] is True


def test_runs_test_and_lint_in_worktree(tmp_path):
    (tmp_path / "wt" / "repo-issue-7").mkdir(parents=True)
    v = lv.ProjectCommandsVerifier("pc", "web", 7, 30, project_fn=lambda a: _project(tmp_path, "pwd", "false"),
                                   worktree_root_fn=lambda: tmp_path / "wt")
    r = v.verify({})
    assert r.passed is False
    assert [c["kind"] for c in r.evidence["commands"]] == ["test", "lint"]
    assert "repo-issue-7" in r.evidence["commands"][0]["output"]
    assert r.output == "$ pwd: passed\n$ false: failed (exit 1)"


def test_empty_commands_are_skipped(tmp_path):
    (tmp_path / "wt" / "repo-issue-7").mkdir(parents=True)
    v = lv.ProjectCommandsVerifier("pc", "web", 7, 30, project_fn=lambda a: _project(tmp_path, "true", ""),
                                   worktree_root_fn=lambda: tmp_path / "wt")
    r = v.verify({})
    assert r.passed and [c["kind"] for c in r.evidence["commands"]] == ["test"]


def test_output_tail_bounded(tmp_path):
    (tmp_path / "wt" / "repo-issue-7").mkdir(parents=True)
    v = lv.ProjectCommandsVerifier("pc", "web", 7, 30,
                                   project_fn=lambda a: _project(tmp_path, "python3 -c \"print('x'*100000); raise SystemExit(1)\"", ""),
                                   worktree_root_fn=lambda: tmp_path / "wt")
    assert len(v.verify({}).output) < 4200


def test_observe_only_never_fails():
    class Bad(lv.Verifier):
        def verify(self, context): return lv.VerificationResult("x", False, 1, 5, "boom", {})
    r = lv.ObserveOnly(Bad()).verify({})
    assert r.passed is True and r.evidence == {"observed_passed": False, "mode": "observe"} and r.output == "boom"


def test_build_verifiers_project_commands_needs_issue():
    with pytest.raises(ValueError):
        lv.build_verifiers([{"name": "pc", "type": "project_commands"}])


def test_build_verifiers_gate_mode_unwrapped():
    issue = {"alias": "web", "issue_iid": 7, "timeout_seconds": 30}
    vs = lv.build_verifiers([{"name": "pc", "type": "project_commands"}], issue=issue, mode="gate")
    assert isinstance(vs[0], lv.ProjectCommandsVerifier)
    vs = lv.build_verifiers([{"name": "pc", "type": "project_commands"}], issue=issue, mode="observe")
    assert isinstance(vs[0], lv.ObserveOnly)


def test_project_commands_verify_never_raises_on_config_error(tmp_path):
    def boom(alias):
        raise KeyError(alias)
    v = lv.ProjectCommandsVerifier("pc", "web", 7, 30, project_fn=boom, worktree_root_fn=lambda: tmp_path / "wt")
    r = v.verify({})
    assert r.passed is False and r.exit_code is None
    assert r.output.startswith("KeyError:") and r.evidence == {"error": True}


def test_project_commands_verify_never_raises_on_missing_binary(tmp_path):
    (tmp_path / "wt" / "repo-issue-7").mkdir(parents=True)
    v = lv.ProjectCommandsVerifier("pc", "web", 7, 30,
                                   project_fn=lambda a: _project(tmp_path, "no-such-binary-xyz --flag", ""),
                                   worktree_root_fn=lambda: tmp_path / "wt")
    r = v.verify({})
    assert r.passed is False and r.output.startswith("FileNotFoundError:") and r.evidence == {"error": True}


@pytest.mark.parametrize("handoff", [None, '{"action": "answer"}', '{"action": "escalate"}', "not json"])
def test_handoff_without_fix_is_vacuous_even_with_a_worktree(tmp_path, handoff):
    (tmp_path / "wt" / "repo-issue-7").mkdir(parents=True)
    hp = tmp_path / "h.json"
    if handoff is not None:
        hp.write_text(handoff)
    v = lv.ProjectCommandsVerifier("pc", "web", 7, 30, project_fn=lambda a: _project(tmp_path, "false", ""),
                                   worktree_root_fn=lambda: tmp_path / "wt", handoff_path=hp)
    r = v.verify({})
    assert r.passed and r.evidence["vacuous"] is True


def test_fix_handoff_runs_the_checks(tmp_path):
    (tmp_path / "wt" / "repo-issue-7").mkdir(parents=True)
    hp = tmp_path / "h.json"
    hp.write_text('{"action": "fix"}')
    v = lv.ProjectCommandsVerifier("pc", "web", 7, 30, project_fn=lambda a: _project(tmp_path, "false", ""),
                                   worktree_root_fn=lambda: tmp_path / "wt", handoff_path=hp)
    assert v.verify({}).passed is False


def test_build_verifiers_passes_the_handoff_path_only_when_given(tmp_path):
    issue = {"alias": "web", "issue_iid": 7, "timeout_seconds": 30}
    spec = [{"name": "pc", "type": "project_commands"}]
    assert lv.build_verifiers(spec, issue=issue, mode="observe")[0].inner.handoff_path is None
    gated = lv.build_verifiers(spec, issue={**issue, "handoff_path": tmp_path / "h.json"}, mode="gate")
    assert gated[0].handoff_path == tmp_path / "h.json"


def test_command_tails_are_stored_once(tmp_path):
    (tmp_path / "wt" / "repo-issue-7").mkdir(parents=True)
    v = lv.ProjectCommandsVerifier("pc", "web", 7, 30,
                                   project_fn=lambda a: _project(tmp_path, "python3 -c \"print('TAIL' * 500)\"", ""),
                                   worktree_root_fn=lambda: tmp_path / "wt")
    r = v.verify({})
    assert "TAIL" in r.evidence["commands"][0]["output"]
    assert "TAILTAIL" not in r.output and len(r.output) < 300
