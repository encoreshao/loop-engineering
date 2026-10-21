import subprocess
from pathlib import Path

import pytest

import update_check


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                        "HOME": str(cwd)})


def commit(cwd, name, text="x"):
    (Path(cwd) / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-m", f"add {name}")


@pytest.fixture
def repos(tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    git(work, "checkout", "-B", "main")
    commit(work, "a.txt")
    git(work, "tag", "-a", "v0.1.0", "-m", "v0.1.0")
    git(work, "push", "-u", "origin", "main", "--tags")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(bare), str(other)], check=True, capture_output=True)
    git(other, "checkout", "main")
    return work, other, tmp_path / "state"


def test_up_to_date(repos):
    work, _other, state = repos
    s = update_check.check(repo=work, state_dir=state)
    assert s["behind"] == 0 and s["error"] is None and s["can_fast_forward"] is True
    assert update_check.read_status(state_dir=state)["behind"] == 0


def test_new_commits_and_release_detected(repos):
    work, other, state = repos
    commit(other, "b.txt")
    commit(other, "c.txt")
    git(other, "tag", "-a", "v0.2.0", "-m", "v0.2.0")
    git(other, "push", "origin", "main", "--tags")
    s = update_check.check(repo=work, state_dir=state)
    assert s["behind"] == 2 and s["ahead"] == 0
    assert [c["subject"] for c in s["commits"]] == ["add c.txt", "add b.txt"]
    assert s["latest_tag"] == "v0.2.0" and s["new_release"] is True
    assert s["can_fast_forward"] is True and s["origin_sha"]


def test_dirty_tracked_file_is_not_clean_but_progress_md_and_untracked_are_ignored(repos):
    work, _o, state = repos
    (work / "untracked.txt").write_text("u")
    commit(work, "PROGRESS.md")
    git(work, "push", "origin", "main")
    (work / "PROGRESS.md").write_text("changed by the loop")
    assert update_check.check(repo=work, state_dir=state)["clean"] is True
    (work / "a.txt").write_text("edited")
    assert update_check.check(repo=work, state_dir=state)["clean"] is False


def test_diverged_cannot_fast_forward(repos):
    work, other, state = repos
    commit(other, "b.txt")
    git(other, "push", "origin", "main")
    commit(work, "local.txt")
    s = update_check.check(repo=work, state_dir=state)
    assert s["behind"] == 1 and s["ahead"] == 1 and s["can_fast_forward"] is False


def test_offline_keeps_last_known_behind_and_records_error(repos):
    work, other, state = repos
    commit(other, "b.txt")
    git(other, "push", "origin", "main")
    assert update_check.check(repo=work, state_dir=state)["behind"] == 1
    git(work, "remote", "set-url", "origin", str(work.parent / "gone.git"))
    s = update_check.check(repo=work, state_dir=state)
    assert s["error"] and s["behind"] == 1


def test_no_upstream_branch_is_an_error_not_a_crash(tmp_path):
    repo = tmp_path / "solo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    commit(repo, "a.txt")
    s = update_check.check(repo=repo, state_dir=tmp_path / "s", fetch=False)
    assert s["error"] and s["behind"] == 0


def test_notification_once_per_origin_sha(repos):
    work, other, state = repos
    assert update_check.take_notification(state_dir=state) is None
    commit(other, "b.txt")
    git(other, "push", "origin", "main")
    update_check.check(repo=work, state_dir=state)
    text = update_check.take_notification(state_dir=state)
    assert text and "1" in text
    assert update_check.take_notification(state_dir=state) is None
    commit(other, "c.txt")
    git(other, "push", "origin", "main")
    update_check.check(repo=work, state_dir=state)
    assert update_check.take_notification(state_dir=state) is not None


def test_env_override_for_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOP_UPDATE_DIR", str(tmp_path / "e"))
    assert update_check.default_state_dir() == tmp_path / "e"


def test_fetch_never_prompts_for_credentials(repos, monkeypatch):
    """The background check must not hang on an ssh/https credential prompt."""
    work, _other, state = repos
    seen = []
    real_run = subprocess.run

    def spy(argv, *a, **kw):
        if "fetch" in argv:
            seen.append(kw.get("env") or {})
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(update_check.subprocess, "run", spy)
    update_check.check(repo=work, state_dir=state)
    assert seen and seen[0].get("GIT_TERMINAL_PROMPT") == "0"
    assert "BatchMode=yes" in seen[0].get("GIT_SSH_COMMAND", "")


def test_check_never_raises_when_the_state_dir_is_unwritable(repos, monkeypatch):
    work, _other, state = repos

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(update_check, "_write", boom)
    s = update_check.check(repo=work, state_dir=state)
    assert s["error"]


def test_background_thread_survives_a_failing_check(monkeypatch):
    calls = []

    def flaky(**_kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {}

    monkeypatch.setattr(update_check, "check", flaky)
    monkeypatch.setitem(update_check._background, "thread", None)
    update_check.start_background(interval=0.01)
    import time
    deadline = time.time() + 3
    while len(calls) < 3 and time.time() < deadline:
        time.sleep(0.02)
    assert len(calls) >= 3


def test_tests_never_share_the_real_update_state_dir():
    import os
    assert os.environ.get("LOOP_UPDATE_DIR")
    assert update_check.REPO_DIR not in Path(update_check.default_state_dir()).parents
