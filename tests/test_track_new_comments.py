import sys
from pathlib import Path

GITLAB_CONFIG_SCRIPTS = Path.home() / ".encore-skills" / "skills" / "gitlab-config" / "scripts"
sys.path.insert(0, str(GITLAB_CONFIG_SCRIPTS))
import gitlab_cache  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import track_new_comments as tnc  # noqa: E402

INSTANCE = "acme"
PROJECT = "acme/brightleaf/harbor"


def test_get_new_notes_returns_all_when_no_prior_annotation(tmp_path, monkeypatch):
    monkeypatch.setattr(gitlab_cache, "CACHE_ROOT", tmp_path)
    gitlab_cache.sync_issue(INSTANCE, PROJECT, 42, {
        "iid": 42,
        "notes": [{"id": 1, "body": "first"}, {"id": 2, "body": "second"}],
    })

    new_notes = tnc.get_new_notes(INSTANCE, PROJECT, 42)

    assert [n["id"] for n in new_notes] == [1, 2]


def test_get_new_notes_returns_only_notes_after_last_seen(tmp_path, monkeypatch):
    monkeypatch.setattr(gitlab_cache, "CACHE_ROOT", tmp_path)
    gitlab_cache.sync_issue(INSTANCE, PROJECT, 42, {
        "iid": 42,
        "notes": [{"id": 1, "body": "first"}, {"id": 2, "body": "second"}],
    })
    tnc.mark_notes_seen(INSTANCE, PROJECT, 42, [{"id": 1}, {"id": 2}])

    gitlab_cache.sync_issue(INSTANCE, PROJECT, 42, {
        "iid": 42,
        "notes": [{"id": 3, "body": "third"}],
    })

    new_notes = tnc.get_new_notes(INSTANCE, PROJECT, 42)

    assert [n["id"] for n in new_notes] == [3]


def test_get_new_notes_returns_empty_when_no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(gitlab_cache, "CACHE_ROOT", tmp_path)

    assert tnc.get_new_notes(INSTANCE, PROJECT, 999) == []


def test_mark_notes_seen_is_noop_on_empty_list(tmp_path, monkeypatch):
    monkeypatch.setattr(gitlab_cache, "CACHE_ROOT", tmp_path)
    gitlab_cache.sync_issue(INSTANCE, PROJECT, 42, {
        "iid": 42,
        "notes": [{"id": 1, "body": "first"}],
    })

    tnc.mark_notes_seen(INSTANCE, PROJECT, 42, [])

    new_notes = tnc.get_new_notes(INSTANCE, PROJECT, 42)
    assert [n["id"] for n in new_notes] == [1]


def test_get_new_notes_excludes_given_author(tmp_path, monkeypatch):
    monkeypatch.setattr(gitlab_cache, "CACHE_ROOT", tmp_path)
    gitlab_cache.sync_issue(INSTANCE, PROJECT, 42, {
        "iid": 42,
        "notes": [
            {"id": 1, "body": "human comment", "author": {"username": "someone"}},
            {"id": 2, "body": "loop's own comment", "author": {"username": "encore"}},
        ],
    })

    new_notes = tnc.get_new_notes(INSTANCE, PROJECT, 42, exclude_author="encore")

    assert [n["id"] for n in new_notes] == [1]


def test_main_mark_seen_advances_watermark(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gitlab_cache, "CACHE_ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["track_new_comments.py", "mark-seen", INSTANCE, PROJECT, "42"])
    gitlab_cache.sync_issue(INSTANCE, PROJECT, 42, {
        "iid": 42,
        "notes": [{"id": 1, "body": "first"}],
    })

    tnc.main()

    captured = capsys.readouterr()
    assert "OK" in captured.out
    assert tnc.get_new_notes(INSTANCE, PROJECT, 42) == []
