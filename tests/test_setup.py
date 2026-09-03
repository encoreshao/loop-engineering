import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "scripts" / "setup.sh"


def run_setup(*args, check=True, env=None):
    return subprocess.run(
        ["bash", str(SCRIPT), "--skip-skills-install", *args],
        check=check, capture_output=True, text=True, env=env,
    )


def test_setup_creates_projects_config_from_template_when_missing(tmp_path):
    config_path = tmp_path / ".loop-engineering" / "projects.json"

    run_setup("--config-path", str(config_path))

    assert config_path.exists()
    assert "YOUR_GITLAB_USERNAME" in config_path.read_text()


def test_setup_substitutes_home_into_worktree_root(tmp_path):
    config_path = tmp_path / ".loop-engineering" / "projects.json"
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {**os.environ, "HOME": str(fake_home)}

    run_setup("--config-path", str(config_path), env=env)

    config = json.loads(config_path.read_text())
    assert config["worktree_root"] == f"{fake_home}/.loop-engineering/worktrees"
    assert "{{HOME}}" not in config_path.read_text()


def test_setup_leaves_existing_projects_config_untouched(tmp_path):
    config_path = tmp_path / "projects.json"
    config_path.write_text('{"already": "configured"}')

    run_setup("--config-path", str(config_path))

    assert config_path.read_text() == '{"already": "configured"}'


def test_setup_rejects_unknown_flag():
    result = run_setup("--not-a-real-flag", check=False)

    assert result.returncode != 0


def test_setup_creates_topics_config_from_template_when_missing(tmp_path):
    projects_path = tmp_path / "projects.json"
    topics_path = tmp_path / "topics.json"

    run_setup("--config-path", str(projects_path), "--topics-config-path", str(topics_path))

    assert topics_path.exists()
    assert "ai-news" in topics_path.read_text()


def test_setup_leaves_existing_topics_config_untouched(tmp_path):
    projects_path = tmp_path / "projects.json"
    topics_path = tmp_path / "topics.json"
    topics_path.write_text('[{"already": "configured"}]')

    run_setup("--config-path", str(projects_path), "--topics-config-path", str(topics_path))

    assert topics_path.read_text() == '[{"already": "configured"}]'


def test_setup_creates_ai_cli_config_from_template_when_missing(tmp_path):
    projects_path = tmp_path / "projects.json"
    ai_cli_path = tmp_path / "ai_cli.json"

    run_setup("--config-path", str(projects_path), "--ai-cli-config-path", str(ai_cli_path))

    assert ai_cli_path.exists()
    assert json.loads(ai_cli_path.read_text()) == {"cli": "claude"}


def test_setup_leaves_existing_ai_cli_config_untouched(tmp_path):
    projects_path = tmp_path / "projects.json"
    ai_cli_path = tmp_path / "ai_cli.json"
    ai_cli_path.write_text('{"cli": "codex"}')

    run_setup("--config-path", str(projects_path), "--ai-cli-config-path", str(ai_cli_path))

    assert ai_cli_path.read_text() == '{"cli": "codex"}'
