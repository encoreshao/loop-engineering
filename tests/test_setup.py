import json
import os
import subprocess
from datetime import date
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


def test_setup_seeds_scheduler_state_with_todays_date_for_every_loop(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {**os.environ, "HOME": str(fake_home)}
    loops_config_path = tmp_path / "loops.json"
    state_path = tmp_path / "loop_scheduler_state.json"

    run_setup(
        "--loops-config-path", str(loops_config_path),
        "--state-path", str(state_path),
        env=env,
    )

    loops = json.loads(loops_config_path.read_text())
    state = json.loads(state_path.read_text())
    today = date.today().isoformat()
    assert set(state.keys()) == {loop["name"] for loop in loops}
    for name in state:
        assert state[name]["last_attempted_date"] == today


def test_setup_leaves_existing_scheduler_state_untouched(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {**os.environ, "HOME": str(fake_home)}
    loops_config_path = tmp_path / "loops.json"
    state_path = tmp_path / "loop_scheduler_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text('{"already": "seeded"}')

    run_setup(
        "--loops-config-path", str(loops_config_path),
        "--state-path", str(state_path),
        env=env,
    )

    assert state_path.read_text() == '{"already": "seeded"}'


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


def test_setup_creates_loops_config_from_template_when_missing(tmp_path):
    projects_path = tmp_path / "projects.json"
    loops_path = tmp_path / "loops.json"

    run_setup("--config-path", str(projects_path), "--loops-config-path", str(loops_path))

    assert loops_path.exists()
    assert "gitlab-issue-loop" in loops_path.read_text()


def test_setup_leaves_existing_loops_config_untouched(tmp_path):
    loops_path = tmp_path / "loops.json"
    loops_path.write_text('{"already": "configured"}')

    run_setup(
        "--config-path", str(tmp_path / "projects.json"),
        "--loops-config-path", str(loops_path),
    )

    assert loops_path.read_text() == '{"already": "configured"}'


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
