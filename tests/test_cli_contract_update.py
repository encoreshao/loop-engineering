"""`update_runner.py run` / `loopx update run`: only the refusal paths, which
return before any build/install/relaunch. Never reaches osascript/open."""
import json
import os
import subprocess

import pytest

from cli_runner import run_cli


def git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"], "HOME": str(cwd)})


@pytest.fixture
def repo(tmp_path):
    bare = tmp_path / "o.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    work = tmp_path / "w"
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    git(work, "checkout", "-B", "main")
    (work / "a.txt").write_text("x")
    git(work, "add", "a.txt")
    git(work, "commit", "-m", "a")
    git(work, "push", "-u", "origin", "main")
    return work


def run(repo, state):
    return run_cli("update_runner.py", "run", "--repo", str(repo), "--state-dir", str(state),
                   "--install-target", str(state.parent / "X.app"), env={"LOOP_UPDATE_DIR": str(state)})


def test_dirty_tree_is_refused(repo, tmp_path):
    (repo / "a.txt").write_text("edited")
    state = tmp_path / "s"
    r = run(repo, state)
    assert r.returncode == 1
    p = json.loads((state / "progress.json").read_text())
    assert p["state"] == "refused" and "local changes" in p["error"]
    assert [s["status"] for s in p["steps"]] == ["pending"] * 6
    assert list(p) == ["state", "pid", "started", "finished", "error", "from", "to", "steps"]
    st = json.loads((state / "status.json").read_text())
    assert st["clean"] is False and st["branch"] == "main"
    assert not (state / "upgrade.lock").exists()


def test_live_lock_refuses_without_touching_progress(repo, tmp_path):
    state = tmp_path / "s"
    state.mkdir()
    (state / "upgrade.lock").write_text(str(os.getpid()))
    r = run(repo, state)
    assert r.returncode == 1
    assert not (state / "progress.json").exists()
    assert (state / "upgrade.lock").exists()
