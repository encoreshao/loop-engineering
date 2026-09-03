import sys
from pathlib import Path

GITLAB_CONFIG_SCRIPTS = Path.home() / ".encore-skills" / "skills" / "gitlab-config" / "scripts"
sys.path.insert(0, str(GITLAB_CONFIG_SCRIPTS))
import gitlab_cache  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import project_memory as pm  # noqa: E402

INSTANCE = "acme"
PROJECT = "acme/brightleaf/harbor"


def test_get_learnings_returns_empty_list_when_none_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(gitlab_cache, "CACHE_ROOT", tmp_path)

    assert pm.get_learnings(INSTANCE, PROJECT) == []


def test_add_learning_persists_and_is_retrievable(tmp_path, monkeypatch):
    monkeypatch.setattr(gitlab_cache, "CACHE_ROOT", tmp_path)

    pm.add_learning(INSTANCE, PROJECT, "rspec flakes on spec/models/user_spec.rb", issue_iid=42, tags=["flaky-test"])

    learnings = pm.get_learnings(INSTANCE, PROJECT)
    assert len(learnings) == 1
    assert learnings[0]["lesson"] == "rspec flakes on spec/models/user_spec.rb"
    assert learnings[0]["issue_iid"] == 42
    assert learnings[0]["tags"] == ["flaky-test"]


def test_add_learning_appends_without_dropping_prior_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(gitlab_cache, "CACHE_ROOT", tmp_path)

    pm.add_learning(INSTANCE, PROJECT, "first lesson")
    pm.add_learning(INSTANCE, PROJECT, "second lesson")

    learnings = pm.get_learnings(INSTANCE, PROJECT)
    assert [entry["lesson"] for entry in learnings] == ["first lesson", "second lesson"]


def test_add_learning_survives_alongside_other_project_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(gitlab_cache, "CACHE_ROOT", tmp_path)
    gitlab_cache.annotate_project(INSTANCE, PROJECT, "some_other_key", "some_other_value")

    pm.add_learning(INSTANCE, PROJECT, "a lesson")

    project = gitlab_cache.get_project(INSTANCE, PROJECT)
    assert project["_memory"]["some_other_key"] == "some_other_value"
    assert len(project["_memory"]["fix_learnings"]) == 1
