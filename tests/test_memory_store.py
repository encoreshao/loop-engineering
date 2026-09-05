import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import memory_store  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(args, env=None):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "memory_store.py"), *args],
        capture_output=True, text=True, env=env,
    )


def test_add_task_memory_creates_index_and_task_file(tmp_path):
    memory_store.add_task_memory(
        "harbor", 142, "RSpec db-cleaner leaves stale rows under parallel runs",
        tags=["flaky-test"], root=tmp_path,
    )

    project_dir = tmp_path / "harbor"
    task_files = [p for p in project_dir.glob("*.md") if p.name != "MEMORY.md"]
    assert len(task_files) == 1
    task_file = task_files[0]
    assert task_file.name.startswith("142-")

    content = task_file.read_text()
    assert "issue_iid: 142" in content
    assert "tags: [flaky-test]" in content
    assert "RSpec db-cleaner leaves stale rows under parallel runs" in content

    index = (project_dir / "MEMORY.md").read_text()
    assert f"]({task_file.name})" in index
    assert "RSpec db-cleaner leaves stale rows under parallel runs" in index


def test_add_task_memory_appends_to_existing_issue_file_without_duplicating(tmp_path):
    memory_store.add_task_memory("harbor", 142, "first lesson on this issue", tags=["flaky-test"], root=tmp_path)
    memory_store.add_task_memory("harbor", 142, "second lesson on this issue", tags=["ci"], root=tmp_path)

    project_dir = tmp_path / "harbor"
    task_files = [p for p in project_dir.glob("*.md") if p.name != "MEMORY.md"]
    assert len(task_files) == 1

    expected_slug = memory_store._slugify("first lesson on this issue")
    assert task_files[0].name == f"142-{expected_slug}.md"

    content = task_files[0].read_text()
    assert "first lesson on this issue" in content
    assert "second lesson on this issue" in content
    assert "tags: [ci, flaky-test]" in content

    index_lines = (project_dir / "MEMORY.md").read_text().splitlines()
    matching = [line for line in index_lines if f"]({task_files[0].name})" in line]
    assert len(matching) == 1
    assert "second lesson on this issue" in matching[0]


def test_get_task_memory_returns_none_when_nothing_recorded(tmp_path):
    assert memory_store.get_task_memory("harbor", 999, root=tmp_path) is None


def test_get_task_memory_returns_parsed_entry(tmp_path):
    memory_store.add_task_memory("harbor", 142, "a lesson", tags=["flaky-test"], root=tmp_path)

    entry = memory_store.get_task_memory("harbor", 142, root=tmp_path)

    assert entry["issue_iid"] == 142
    assert entry["tags"] == ["flaky-test"]
    assert "a lesson" in entry["body"]
    assert entry["description"] == "a lesson"


def test_list_task_memories_returns_empty_list_for_unknown_alias(tmp_path):
    assert memory_store.list_task_memories("unknown", root=tmp_path) == []


def test_list_task_memories_returns_every_recorded_issue(tmp_path):
    memory_store.add_task_memory("harbor", 142, "lesson one", root=tmp_path)
    memory_store.add_task_memory("harbor", 7, "lesson two", root=tmp_path)

    entries = memory_store.list_task_memories("harbor", root=tmp_path)

    assert {e["issue_iid"] for e in entries} == {142, 7}


def test_read_index_returns_none_when_nothing_recorded(tmp_path):
    assert memory_store.read_index("harbor", root=tmp_path) is None


def test_list_task_memories_skips_a_file_with_missing_issue_iid(tmp_path):
    project_dir = tmp_path / "harbor"
    project_dir.mkdir(parents=True)
    (project_dir / "142-broken.md").write_text(
        "---\n"
        "name: 142-broken\n"
        "description: broken entry\n"
        "metadata:\n"
        "  tags: []\n"
        "  modified: 2026-09-02T00:00:00Z\n"
        "---\n"
        "\nsome body\n"
    )

    entries = memory_store.list_task_memories("harbor", root=tmp_path)

    assert entries == []


def test_list_task_memories_skips_a_file_with_non_numeric_issue_iid(tmp_path):
    project_dir = tmp_path / "harbor"
    project_dir.mkdir(parents=True)
    (project_dir / "142-broken.md").write_text(
        "---\n"
        "name: 142-broken\n"
        "description: broken entry\n"
        "metadata:\n"
        "  issue_iid: not-a-number\n"
        "  tags: []\n"
        "  modified: 2026-09-02T00:00:00Z\n"
        "---\n"
        "\nsome body\n"
    )

    entries = memory_store.list_task_memories("harbor", root=tmp_path)

    assert entries == []


def test_get_task_memory_returns_none_for_a_file_with_missing_issue_iid(tmp_path):
    project_dir = tmp_path / "harbor"
    project_dir.mkdir(parents=True)
    (project_dir / "142-broken.md").write_text(
        "---\n"
        "name: 142-broken\n"
        "description: broken entry\n"
        "metadata:\n"
        "  tags: []\n"
        "  modified: 2026-09-02T00:00:00Z\n"
        "---\n"
        "\nsome body\n"
    )

    assert memory_store.get_task_memory("harbor", 142, root=tmp_path) is None


def test_add_task_memory_preserves_existing_text_when_existing_file_has_no_frontmatter(tmp_path):
    project_dir = tmp_path / "harbor"
    project_dir.mkdir(parents=True)
    existing_path = project_dir / "142-broken.md"
    existing_path.write_text("Hand-written notes with no frontmatter at all.\n")

    memory_store.add_task_memory("harbor", 142, "a fresh lesson", root=tmp_path)

    task_files = [p for p in project_dir.glob("*.md") if p.name != "MEMORY.md"]
    assert len(task_files) == 1
    assert task_files[0] == existing_path

    content = existing_path.read_text()
    assert "Hand-written notes with no frontmatter at all." in content
    assert "a fresh lesson" in content


def test_slugify_is_filesystem_safe_and_stable_length():
    slug = memory_store._slugify("Fix: the invalid 84200-84300 port range!")
    assert slug == "fix-the-invalid-84200-84300-port"


def test_slugify_truncates_and_never_ends_with_a_hyphen():
    long_text = "a " * 40 + "final"
    slug = memory_store._slugify(long_text, max_len=10)
    assert len(slug) <= 10
    assert not slug.endswith("-")


def test_add_task_memory_generates_lesson_id_and_created_at_on_first_creation(tmp_path):
    result = memory_store.add_task_memory("harbor", 142, "a fresh lesson", category="testing", root=tmp_path)

    assert result["created"] is True
    assert result["lesson_id"].startswith("lesson_")
    entry = memory_store.get_task_memory("harbor", 142, root=tmp_path)
    assert entry["lesson_id"] == result["lesson_id"]
    assert entry["category"] == "testing"
    assert entry["created_at"] is not None


def test_add_task_memory_preserves_lesson_id_and_created_at_across_append(tmp_path):
    first = memory_store.add_task_memory("harbor", 142, "first lesson", category="testing", root=tmp_path)
    second = memory_store.add_task_memory("harbor", 142, "second lesson", category="something-else", root=tmp_path)

    assert second["created"] is False
    assert second["lesson_id"] == first["lesson_id"]
    entry = memory_store.get_task_memory("harbor", 142, root=tmp_path)
    assert entry["lesson_id"] == first["lesson_id"]
    assert entry["created_at"] == memory_store.get_task_memory("harbor", 142, root=tmp_path)["created_at"]
    # category is immutable once set - the "something-else" passed on append is ignored
    assert entry["category"] == "testing"


def test_add_task_memory_treats_frontmatter_less_file_as_created_and_assigns_fresh_identity(tmp_path):
    project_dir = tmp_path / "harbor"
    project_dir.mkdir(parents=True)
    existing_path = project_dir / "142-broken.md"
    existing_path.write_text("Hand-written notes with no frontmatter at all.\n")

    result = memory_store.add_task_memory("harbor", 142, "a fresh lesson", root=tmp_path)

    assert result["created"] is True
    assert result["lesson_id"].startswith("lesson_")


def test_get_task_memory_pre_sprint_6_entry_has_none_lesson_fields(tmp_path):
    project_dir = tmp_path / "harbor"
    project_dir.mkdir(parents=True)
    (project_dir / "142-old.md").write_text(
        "---\n"
        "name: 142-old\n"
        "description: an old entry\n"
        "metadata:\n"
        "  issue_iid: 142\n"
        "  tags: []\n"
        "  modified: 2026-09-02T00:00:00Z\n"
        "---\n"
        "\nold body\n"
    )

    entry = memory_store.get_task_memory("harbor", 142, root=tmp_path)

    assert entry["lesson_id"] is None
    assert entry["category"] is None
    assert entry["created_at"] is None


def test_add_task_memory_appending_to_pre_sprint_6_entry_does_not_backfill_lesson_id(tmp_path):
    project_dir = tmp_path / "harbor"
    project_dir.mkdir(parents=True)
    (project_dir / "142-old.md").write_text(
        "---\n"
        "name: 142-old\n"
        "description: an old entry\n"
        "metadata:\n"
        "  issue_iid: 142\n"
        "  tags: []\n"
        "  modified: 2026-09-02T00:00:00Z\n"
        "---\n"
        "\nold body\n"
    )

    result = memory_store.add_task_memory("harbor", 142, "a follow-up note", root=tmp_path)

    assert result["created"] is False
    assert result["lesson_id"] is None
    entry = memory_store.get_task_memory("harbor", 142, root=tmp_path)
    assert entry["lesson_id"] is None


def test_cli_add_prints_created_action_and_lesson_id(tmp_path):
    result = _run_cli(["add", "harbor", "142", "a lesson", "flaky-test", "testing"], env={**__import__("os").environ, "LOOP_ENGINEERING_HOME": str(tmp_path)})

    payload = json.loads(result.stdout)
    assert payload["action"] == "created"
    assert payload["lesson_id"].startswith("lesson_")
    assert "path" in payload


def test_cli_add_prints_updated_action_on_append(tmp_path):
    env = {**__import__("os").environ, "LOOP_ENGINEERING_HOME": str(tmp_path)}
    _run_cli(["add", "harbor", "142", "first lesson", "", "testing"], env=env)

    result = _run_cli(["add", "harbor", "142", "second lesson"], env=env)

    payload = json.loads(result.stdout)
    assert payload["action"] == "updated"
