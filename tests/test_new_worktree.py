import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "scripts" / "new_worktree.sh"


def run_git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def make_repo(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "staging", str(origin)], check=True, capture_output=True)

    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    run_git(repo, "add", "README.md")
    run_git(repo, "commit", "-m", "initial commit")
    run_git(repo, "push", "-u", "origin", "staging")
    return repo


def current_branch(path):
    return subprocess.run(
        ["git", "-C", str(path), "branch", "--show-current"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_new_worktree_creates_branch_from_default(tmp_path):
    repo = make_repo(tmp_path)
    worktree_root = tmp_path / "worktrees"

    result = subprocess.run(
        ["bash", str(SCRIPT), str(repo), "staging", "123", str(worktree_root)],
        check=True, capture_output=True, text=True,
    )
    worktree_path = Path(result.stdout.strip())

    assert worktree_path.exists()
    assert (worktree_path / "README.md").exists()
    assert current_branch(worktree_path) == "loop/issue-123"


def test_new_worktree_reuses_existing_branch(tmp_path):
    repo = make_repo(tmp_path)
    worktree_root = tmp_path / "worktrees"

    first = subprocess.run(
        ["bash", str(SCRIPT), str(repo), "staging", "123", str(worktree_root)],
        check=True, capture_output=True, text=True,
    )
    first_path = Path(first.stdout.strip())
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(first_path)], check=True)

    second = subprocess.run(
        ["bash", str(SCRIPT), str(repo), "staging", "123", str(worktree_root)],
        check=True, capture_output=True, text=True,
    )
    worktree_path = Path(second.stdout.strip())

    assert worktree_path == first_path
    assert worktree_path.exists()
    assert current_branch(worktree_path) == "loop/issue-123"


def test_new_worktree_merges_latest_default_branch_into_existing_branch(tmp_path):
    repo = make_repo(tmp_path)
    worktree_root = tmp_path / "worktrees"

    first = subprocess.run(
        ["bash", str(SCRIPT), str(repo), "staging", "123", str(worktree_root)],
        check=True, capture_output=True, text=True,
    )
    first_path = Path(first.stdout.strip())

    # Simulate new work landing on staging after the issue branch was created.
    other_clone = tmp_path / "other-clone"
    subprocess.run(["git", "clone", str(tmp_path / "origin.git"), str(other_clone)], check=True, capture_output=True)
    run_git(other_clone, "config", "user.email", "test@example.com")
    run_git(other_clone, "config", "user.name", "Test")
    (other_clone / "NEWS.md").write_text("late-breaking change\n")
    run_git(other_clone, "add", "NEWS.md")
    run_git(other_clone, "commit", "-m", "new staging commit")
    run_git(other_clone, "push", "origin", "staging")

    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(first_path)], check=True)

    second = subprocess.run(
        ["bash", str(SCRIPT), str(repo), "staging", "123", str(worktree_root)],
        check=True, capture_output=True, text=True,
    )
    worktree_path = Path(second.stdout.strip())

    assert (worktree_path / "NEWS.md").exists()
    assert (worktree_path / "README.md").exists()


def test_new_worktree_refuses_when_worktree_has_uncommitted_changes(tmp_path):
    repo = make_repo(tmp_path)
    worktree_root = tmp_path / "worktrees"

    first = subprocess.run(
        ["bash", str(SCRIPT), str(repo), "staging", "123", str(worktree_root)],
        check=True, capture_output=True, text=True,
    )
    worktree_path = Path(first.stdout.strip())
    (worktree_path / "README.md").write_text("uncommitted local edit\n")

    result = subprocess.run(
        ["bash", str(SCRIPT), str(repo), "staging", "123", str(worktree_root)],
        capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert "uncommitted changes" in result.stderr.lower()


def test_new_worktree_aborts_cleanly_on_merge_conflict(tmp_path):
    repo = make_repo(tmp_path)
    worktree_root = tmp_path / "worktrees"

    first = subprocess.run(
        ["bash", str(SCRIPT), str(repo), "staging", "123", str(worktree_root)],
        check=True, capture_output=True, text=True,
    )
    worktree_path = Path(first.stdout.strip())
    (worktree_path / "README.md").write_text("conflicting local change\n")
    run_git(worktree_path, "add", "README.md")
    run_git(worktree_path, "commit", "-m", "local conflicting commit")

    other_clone = tmp_path / "other-clone"
    subprocess.run(["git", "clone", str(tmp_path / "origin.git"), str(other_clone)], check=True, capture_output=True)
    run_git(other_clone, "config", "user.email", "test@example.com")
    run_git(other_clone, "config", "user.name", "Test")
    (other_clone / "README.md").write_text("origin-side conflicting change\n")
    run_git(other_clone, "add", "README.md")
    run_git(other_clone, "commit", "-m", "origin conflicting commit")
    run_git(other_clone, "push", "origin", "staging")

    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree_path)], check=True)

    result = subprocess.run(
        ["bash", str(SCRIPT), str(repo), "staging", "123", str(worktree_root)],
        capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert "conflict" in result.stderr.lower()
    # MERGE_HEAD must not be left dangling after the abort
    merge_head_check = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        capture_output=True, text=True,
    )
    assert merge_head_check.returncode != 0
