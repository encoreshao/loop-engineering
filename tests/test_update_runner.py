import os
import subprocess
from pathlib import Path

import pytest

import update_check
import update_runner
from test_update_check import commit, git, repos  # noqa: F401  (fixture reuse)


class FakeCmd:
    """Stands in for update_runner's command runner. git commands run for
    real (against the temp repos); everything else is recorded and faked."""

    def __init__(self, fail_on=None, output="line"):
        self.calls, self.fail_on, self.output = [], fail_on, output

    def __call__(self, argv, cwd, on_line):
        self.calls.append([str(a) for a in argv])
        on_line(self.output)
        joined = " ".join(map(str, argv))
        if self.fail_on and self.fail_on in joined:
            on_line("boom")
            return 1
        if argv[0] == "git":
            return subprocess.run([str(a) for a in argv], cwd=str(cwd), capture_output=True).returncode
        if "build_macos_app.sh" in joined:  # emulate the build output
            out = Path(argv[argv.index("--output-dir") + 1])
            (out / "Loop X.app" / "Contents").mkdir(parents=True)
            (out / "Loop X.app" / "Contents" / "marker").write_text("new")
            (out / "Loop X.dmg").write_text("dmg")
        return 0


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(update_runner.time, "sleep", lambda *_a: None)


def behind_repo(repos):
    work, other, state = repos
    commit(other, "b.txt")
    git(other, "push", "origin", "main")
    return work, state


def test_happy_path_runs_steps_in_order_and_installs(repos, tmp_path):
    work, state = behind_repo(repos)
    target = tmp_path / "Apps" / "Loop X.app"
    (target / "Contents").mkdir(parents=True)
    (target / "Contents" / "marker").write_text("old")
    cmd = FakeCmd()
    p = update_runner.run(repo=work, state_dir=state, install_target=target, run_cmd=cmd)
    assert p["state"] == "done" and [s["status"] for s in p["steps"]] == ["done"] * 6
    assert [s["name"] for s in p["steps"]] == update_runner.STEPS
    assert (target / "Contents" / "marker").read_text() == "new"
    assert not target.with_name("Loop X.app.old").exists()
    assert (work / "b.txt").exists()                      # pulled for real
    assert (work / "dist" / "Loop X.dmg").read_text() == "dmg"
    flat = [" ".join(c) for c in cmd.calls]
    assert any("restart-daemons.sh" in c for c in flat)
    assert not any("--with-scheduler" in c for c in flat)
    assert any(c.startswith("open") and "Loop X.app" in c for c in flat)
    assert update_check.read_status(state_dir=state)["behind"] == 0
    assert not (state / "upgrade.lock").exists()


def test_build_failure_stops_keeps_old_app_and_reports_log(repos, tmp_path):
    work, state = behind_repo(repos)
    target = tmp_path / "Loop X.app"
    (target / "Contents").mkdir(parents=True)
    (target / "Contents" / "marker").write_text("old")
    p = update_runner.run(repo=work, state_dir=state, install_target=target, run_cmd=FakeCmd(fail_on="build_macos_app"))
    assert p["state"] == "failed" and "build" in p["error"]
    by = {s["name"]: s for s in p["steps"]}
    assert by["build"]["status"] == "failed" and "boom" in by["build"]["log"]
    assert by["install"]["status"] == "pending"
    assert (target / "Contents" / "marker").read_text() == "old"
    assert not (state / "upgrade.lock").exists()


def test_dirty_tracked_file_is_refused_before_any_step(repos, tmp_path):
    work, state = behind_repo(repos)
    (work / "a.txt").write_text("edited")
    cmd = FakeCmd()
    p = update_runner.run(repo=work, state_dir=state, install_target=tmp_path / "x.app", run_cmd=cmd)
    assert p["state"] == "refused" and "local changes" in p["error"] and cmd.calls == []


def test_modified_progress_md_does_not_block(repos, tmp_path):
    work, other, state = repos
    commit(work, "PROGRESS.md")
    git(work, "push", "origin", "main")
    git(other, "pull", "--ff-only")
    commit(other, "b.txt")
    git(other, "push", "origin", "main")
    (work / "PROGRESS.md").write_text("loop wrote this")
    p = update_runner.run(repo=work, state_dir=state, install_target=tmp_path / "L.app", run_cmd=FakeCmd())
    assert p["state"] == "done"


def test_non_fast_forward_is_refused(repos, tmp_path):
    work, other, state = repos
    commit(other, "b.txt")
    git(other, "push", "origin", "main")
    commit(work, "local.txt")
    p = update_runner.run(repo=work, state_dir=state, install_target=tmp_path / "L.app", run_cmd=FakeCmd())
    assert p["state"] == "refused" and "fast-forward" in p["error"]


def test_live_lock_refuses_second_run_without_clobbering_progress_stale_lock_is_ignored(repos, tmp_path):
    work, state = behind_repo(repos)
    state.mkdir(parents=True, exist_ok=True)
    update_runner._write_progress(state, {"state": "running", "pid": os.getpid(), "steps": []})
    (state / "upgrade.lock").write_text(str(os.getpid()))
    p = update_runner.run(repo=work, state_dir=state, install_target=tmp_path / "L.app", run_cmd=FakeCmd())
    assert p["state"] == "refused" and "already" in p["error"]
    assert update_runner.read_progress(state_dir=state)["state"] == "running"  # live run's file untouched
    (state / "upgrade.lock").write_text("999999999")
    p = update_runner.run(repo=work, state_dir=state, install_target=tmp_path / "L.app", run_cmd=FakeCmd())
    assert p["state"] == "done"


def test_swap_replaces_app_and_cleanup_removes_old(tmp_path):
    target = tmp_path / "Loop X.app"
    (target / "Contents").mkdir(parents=True)
    (target / "Contents" / "marker").write_text("old")
    new = tmp_path / "stage" / "Loop X.app"
    (new / "Contents").mkdir(parents=True)
    (new / "Contents" / "marker").write_text("new")
    update_runner.swap_app(new, target)
    assert (target / "Contents" / "marker").read_text() == "new"
    update_runner.cleanup_old(target)
    assert not target.with_name("Loop X.app.old").exists()


def test_swap_restores_old_app_when_the_final_rename_fails(tmp_path, monkeypatch):
    target = tmp_path / "Loop X.app"
    (target / "Contents").mkdir(parents=True)
    (target / "Contents" / "marker").write_text("old")
    new = tmp_path / "stage" / "Loop X.app"
    new.mkdir(parents=True)
    real_rename = update_runner.os.rename

    def failing(src, dst):
        if Path(src).name == ".Loop X.app.new":
            raise OSError("rename failed")
        return real_rename(src, dst)

    monkeypatch.setattr(update_runner.os, "rename", failing)
    with pytest.raises(OSError):
        update_runner.swap_app(new, target)
    assert (target / "Contents" / "marker").read_text() == "old"
    assert not target.with_name("Loop X.app.old").exists()
    assert not (tmp_path / ".Loop X.app.new").exists()


def test_running_progress_with_dead_pid_reads_as_failed(tmp_path):
    update_runner._write_progress(tmp_path, {"state": "running", "pid": 999999999, "steps": []})
    p = update_runner.read_progress(state_dir=tmp_path)
    assert p["state"] == "failed" and "interrupted" in p["error"]


def test_start_detached_writes_initial_progress_and_spawns_runner(tmp_path):
    spawned = []
    p = update_runner.start_detached(repo=tmp_path, state_dir=tmp_path / "s",
                                     popen=lambda argv, **kw: spawned.append((argv, kw)))
    assert p["state"] == "starting" and [s["status"] for s in p["steps"]] == ["pending"] * 6
    argv, kw = spawned[0]
    assert "update_runner.py" in " ".join(map(str, argv)) and "run" in argv
    assert kw["start_new_session"] is True


def test_start_detached_refuses_when_run_in_flight(tmp_path):
    state = tmp_path / "s"
    state.mkdir()
    (state / "upgrade.lock").write_text(str(os.getpid()))
    spawned = []
    p = update_runner.start_detached(repo=tmp_path, state_dir=state, popen=lambda *a, **k: spawned.append(1))
    assert p["state"] == "refused" and spawned == []


def test_resolve_install_target(tmp_path):
    state = tmp_path / "s"
    assert update_runner.resolve_install_target(repo=tmp_path, state_dir=state) == tmp_path / "dist" / "Loop X.app"
    app = tmp_path / "Apps" / "Loop X.app"
    app.mkdir(parents=True)
    update_runner.record_app_path(app, state_dir=state)
    assert update_runner.resolve_install_target(repo=tmp_path, state_dir=state) == app


# --- review fixes: C1 swap safety, C2 build python, I2 relaunch wait, I3 child pid, I4 merge ref ---

def test_swap_leaves_target_intact_when_copy_is_interrupted_midway(tmp_path, monkeypatch):
    target = tmp_path / "Loop X.app"
    (target / "Contents").mkdir(parents=True)
    (target / "Contents" / "marker").write_text("old")
    new = tmp_path / "stage" / "Loop X.app"
    (new / "Contents").mkdir(parents=True)

    def partial_copy(src, dst, **_kw):
        Path(dst).mkdir(parents=True)
        (Path(dst) / "half").write_text("x")
        raise OSError("interrupted")

    monkeypatch.setattr(update_runner.shutil, "copytree", partial_copy)
    with pytest.raises(OSError):
        update_runner.swap_app(new, target)
    assert (target / "Contents" / "marker").read_text() == "old"
    assert not (tmp_path / ".Loop X.app.new").exists()


def test_swap_recovers_a_lost_target_from_a_leftover_old_instead_of_deleting_it(tmp_path):
    old = tmp_path / "Loop X.app.old"
    (old / "Contents").mkdir(parents=True)
    (old / "Contents" / "marker").write_text("last good app")
    target = tmp_path / "Loop X.app"            # missing: an earlier swap was interrupted
    new = tmp_path / "stage" / "Loop X.app"
    (new / "Contents").mkdir(parents=True)
    (new / "Contents" / "marker").write_text("new")
    update_runner.swap_app(new, target)
    assert (target / "Contents" / "marker").read_text() == "new"
    assert (target.with_name("Loop X.app.old") / "Contents" / "marker").read_text() == "last good app"


def test_swap_keeps_old_until_cleanup_and_cleanup_needs_a_target(tmp_path):
    target = tmp_path / "Loop X.app"
    (target / "Contents").mkdir(parents=True)
    (target / "Contents" / "marker").write_text("old")
    new = tmp_path / "stage" / "Loop X.app"
    (new / "Contents").mkdir(parents=True)
    (new / "Contents" / "marker").write_text("new")
    update_runner.swap_app(new, target)
    old = target.with_name("Loop X.app.old")
    assert old.exists()                          # the running app's bundle is not deleted under it
    update_runner.cleanup_old(target)
    assert not old.exists()
    lone_old = tmp_path / "Other.app.old"
    lone_old.mkdir()
    update_runner.cleanup_old(tmp_path / "Other.app")   # no target: keep the only copy
    assert lone_old.exists()


def test_build_gets_an_explicit_modern_python_and_a_full_path(repos, tmp_path):
    work, state = behind_repo(repos)
    cmd = FakeCmd()
    update_runner.run(repo=work, state_dir=state, install_target=tmp_path / "L.app", run_cmd=cmd)
    build = next(c for c in cmd.calls if any("build_macos_app.sh" in a for a in c))
    assert build[build.index("--python") + 1] == update_runner.build_python()
    assert "/opt/homebrew/bin" in update_runner._command_env()["PATH"].split(":")


def test_relaunch_waits_for_the_old_app_to_quit_before_opening(repos, tmp_path):
    work, state = behind_repo(repos)
    polls = []

    class Cmd(FakeCmd):
        def __call__(self, argv, cwd, on_line):
            if argv[0] == "osascript" and "is running" in " ".join(map(str, argv)):
                polls.append(1)
                on_line("true" if len(polls) < 3 else "false")
                self.calls.append(["is-running-poll"])
                return 0
            return super().__call__(argv, cwd, on_line)

    cmd = Cmd()
    update_runner.run(repo=work, state_dir=state, install_target=tmp_path / "L.app", run_cmd=cmd)
    names = [c[0] for c in cmd.calls]
    assert names.count("is-running-poll") == 3
    assert names.index("open") > max(i for i, n in enumerate(names) if n == "is-running-poll")


def test_start_detached_records_the_child_pid(tmp_path):
    class Proc:
        pid = 4242

    p = update_runner.start_detached(repo=tmp_path, state_dir=tmp_path / "s", popen=lambda *a, **k: Proc())
    assert p["pid"] == 4242
    import json
    assert json.loads((tmp_path / "s" / "progress.json").read_text())["pid"] == 4242


def test_upgrade_works_on_a_branch_with_no_configured_upstream(repos, tmp_path):
    work, state = behind_repo(repos)
    git(work, "branch", "--unset-upstream")
    p = update_runner.run(repo=work, state_dir=state, install_target=tmp_path / "L.app", run_cmd=FakeCmd())
    assert p["state"] == "done" and (work / "b.txt").exists()


def test_upgrade_succeeds_with_a_locally_modified_progress_md_that_upstream_did_not_touch(repos, tmp_path):
    work, other, state = repos
    commit(work, "PROGRESS.md", "v1")
    git(work, "push", "origin", "main")
    git(other, "pull", "--ff-only")
    commit(other, "b.txt")
    git(other, "push", "origin", "main")
    (work / "PROGRESS.md").write_text("loop wrote this")
    p = update_runner.run(repo=work, state_dir=state, install_target=tmp_path / "L.app", run_cmd=FakeCmd())
    assert p["state"] == "done" and (work / "PROGRESS.md").read_text() == "loop wrote this"
