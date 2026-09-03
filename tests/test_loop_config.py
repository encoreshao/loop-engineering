import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import loop_config


def write_config(path, **overrides):
    config = {
        "gitlab_instance": "acme",
        "assignee_username": "encore",
        "worktree_root": "/tmp/loop-worktrees",
        "projects": {
            "harbor": {
                "project_id": "acme/brightleaf/harbor",
                "local_path": "/tmp/harbor",
                "target_branch": "staging",
                "install_cmd": "npm ci",
                "lint_cmd": "npm run lint",
                "test_cmd": "npm run test",
            },
            "orchard": {
                "project_id": "acme/brightleaf/orchard",
                "local_path": "/tmp/orchard",
                "target_branch": "staging",
                "install_cmd": "yarn install --frozen-lockfile",
                "lint_cmd": "npm run lint",
                "test_cmd": "npm run test",
            },
        },
    }
    config.update(overrides)
    path.write_text(json.dumps(config))
    return path


def test_load_config_reads_file(tmp_path):
    config_path = write_config(tmp_path / "projects.json")

    config = loop_config.load_config(config_path)

    assert config["gitlab_instance"] == "acme"
    assert config["assignee_username"] == "encore"


def test_load_config_missing_file_raises_helpful_error(tmp_path):
    missing_path = tmp_path / "does-not-exist.json"

    with pytest.raises(FileNotFoundError, match="projects.json.template"):
        loop_config.load_config(missing_path)


def test_list_aliases(tmp_path):
    config_path = write_config(tmp_path / "projects.json")

    assert sorted(loop_config.list_aliases(config_path)) == ["harbor", "orchard"]


def test_get_project_returns_full_entry(tmp_path):
    config_path = write_config(tmp_path / "projects.json")

    project = loop_config.get_project("harbor", config_path)

    assert project["project_id"] == "acme/brightleaf/harbor"
    assert project["local_path"] == "/tmp/harbor"
    assert project["test_cmd"] == "npm run test"


def test_get_project_falls_back_to_global_instance_when_not_overridden(tmp_path):
    config_path = write_config(tmp_path / "projects.json")

    project = loop_config.get_project("harbor", config_path)

    assert project["instance"] == "acme"


def test_get_project_uses_its_own_instance_override(tmp_path):
    config = {
        "gitlab_instance": "acme",
        "assignee_username": "encore",
        "worktree_root": "/tmp/loop-worktrees",
        "projects": {
            "harbor": {
                "project_id": "acme/brightleaf/harbor",
                "local_path": "/tmp/harbor",
                "target_branch": "staging",
                "install_cmd": "npm ci",
                "lint_cmd": "npm run lint",
                "test_cmd": "npm run test",
            },
            "other-org-project": {
                "project_id": "other-org/some-project",
                "local_path": "/tmp/other-org-project",
                "target_branch": "main",
                "install_cmd": "npm ci",
                "lint_cmd": "npm run lint",
                "test_cmd": "npm run test",
                "instance": "other-gitlab",
            },
        },
    }
    config_path = tmp_path / "projects.json"
    config_path.write_text(json.dumps(config))

    default_instance_project = loop_config.get_project("harbor", config_path)
    overridden_project = loop_config.get_project("other-org-project", config_path)

    assert default_instance_project["instance"] == "acme"
    assert overridden_project["instance"] == "other-gitlab"


def test_get_worktree_root(tmp_path):
    config_path = write_config(tmp_path / "projects.json")

    assert loop_config.get_worktree_root(config_path) == "/tmp/loop-worktrees"


def test_get_assignee_username(tmp_path):
    config_path = write_config(tmp_path / "projects.json")

    assert loop_config.get_assignee_username(config_path) == "encore"
