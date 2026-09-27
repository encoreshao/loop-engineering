from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_task_doc_exists_with_fixed_safety_boundary():
    text = (REPO_ROOT / "docs" / "tasks" / "inbox-triage-loop.md").read_text()
    for needle in ("## Safety boundary", "Never sends mail", "Mail.Send", "Keychain", "inboxes.json", "loops.json"):
        assert needle in text


def test_task_index_and_readme_mention_the_loop():
    assert "docs/tasks/inbox-triage-loop.md" in (REPO_ROOT / "TASK.md").read_text()
    assert "inbox-triage-loop" in (REPO_ROOT / "README.md").read_text()


def test_claude_md_documents_keychain_sandbox_rule():
    text = (REPO_ROOT / "CLAUDE.md").read_text()
    assert "loop-engineering.mail" in text and "inbox_triage_runner.py" in text
