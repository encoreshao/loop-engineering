import glob
import json
import os
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "scripts" / "setup.sh"


def run_setup(*args, check=True, env=None):
    env_arg = env
    # Never run against the real $HOME: setup.sh scaffolds files under it.
    env = {**os.environ, **(env or {})}
    temp_home = None
    if "HOME" not in (env_arg or {}):
        temp_home = tempfile.mkdtemp(prefix="setup-home-")
        env["HOME"] = temp_home
    # Keep pyenv shims (resolved via $HOME/.pyenv) working under a fake HOME.
    env.setdefault("PYENV_ROOT", str(Path(os.path.expanduser("~")) / ".pyenv"))
    try:
        return subprocess.run(
            ["bash", str(SCRIPT), "--skip-skills-install", *args],
            check=check, capture_output=True, text=True, env=env,
        )
    finally:
        if temp_home is not None:
            shutil.rmtree(temp_home, ignore_errors=True)


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
    assert "gitlab-loop" in loops_path.read_text()


def test_setup_leaves_existing_loops_config_untouched(tmp_path):
    loops_path = tmp_path / "loops.json"
    loops_path.write_text('{"already": "configured"}')
    (tmp_path / "state.json").write_text("{}")

    run_setup(
        "--config-path", str(tmp_path / "projects.json"),
        "--loops-config-path", str(loops_path),
        "--state-path", str(tmp_path / "state.json"),
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


def test_setup_creates_inboxes_config_from_template_when_missing(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {**os.environ, "HOME": str(fake_home)}
    projects_path = tmp_path / "projects.json"

    run_setup("--config-path", str(projects_path), env=env)

    inboxes_path = fake_home / ".loop-engineering" / "inboxes.json"
    template_path = Path(__file__).resolve().parent.parent / "config" / "inboxes.json.template"
    assert inboxes_path.exists()
    assert inboxes_path.read_bytes() == template_path.read_bytes()


def test_setup_leaves_existing_inboxes_config_untouched(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {**os.environ, "HOME": str(fake_home)}
    projects_path = tmp_path / "projects.json"
    inboxes_path = fake_home / ".loop-engineering" / "inboxes.json"
    inboxes_path.parent.mkdir(parents=True, exist_ok=True)
    inboxes_path.write_text('{"already": "configured"}')

    run_setup("--config-path", str(projects_path), env=env)

    assert inboxes_path.read_text() == '{"already": "configured"}'


def test_setup_creates_connectors_config_from_template_when_missing(tmp_path):
    projects_path = tmp_path / "projects.json"
    connectors_path = tmp_path / "connectors.json"

    run_setup("--config-path", str(projects_path), "--connectors-config-path", str(connectors_path))

    assert json.loads(connectors_path.read_text()) == []


def test_setup_leaves_existing_connectors_config_untouched(tmp_path):
    projects_path = tmp_path / "projects.json"
    connectors_path = tmp_path / "connectors.json"
    connectors_path.write_text('[{"id": "keep"}]')

    run_setup("--config-path", str(projects_path), "--connectors-config-path", str(connectors_path))

    assert connectors_path.read_text() == '[{"id": "keep"}]'


def test_run_setup_never_touches_real_home(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    run_setup("--config-path", str(tmp_path / "projects.json"), env={"HOME": str(fake_home)})

    base = fake_home / ".loop-engineering"
    assert (base / "connectors.json").exists() and (base / "inboxes.json").exists()


def test_run_setup_defaults_to_temp_home(tmp_path, monkeypatch):
    seen = {}
    real_run = subprocess.run

    def spy(cmd, **kw):
        seen["home"] = kw["env"]["HOME"]
        result = real_run(cmd, **kw)
        # Checked here: run_setup removes its temp HOME once setup.sh exits.
        seen["scaffolded"] = (Path(seen["home"]) / ".loop-engineering" / "connectors.json").exists()
        return result

    monkeypatch.setattr(subprocess, "run", spy)
    run_setup("--config-path", str(tmp_path / "projects.json"))
    assert seen["home"] != str(Path.home())
    assert seen["scaffolded"]


def test_run_setup_cleans_up_its_temporary_home(tmp_path):
    pattern = str(Path(tempfile.gettempdir()) / "setup-home-*")
    before = set(glob.glob(pattern))
    run_setup("--config-path", str(tmp_path / "projects.json"))
    assert set(glob.glob(pattern)) == before
