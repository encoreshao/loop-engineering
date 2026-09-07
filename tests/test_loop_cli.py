import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "bin" / "loop_cli.py"
TEMPLATES_DIR = REPO_ROOT / "templates"
_TEMPLATE_NAMES = sorted(p.parent.name for p in TEMPLATES_DIR.glob("*/loop.yaml"))


def _run(*args):
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True)


def _write_definition(path, **overrides):
    data = {
        "name": "cli-test-loop",
        "version": 1,
        "trigger": {"type": "manual"},
        "goal": {"type": "self_check"},
        "actions": ["run_tests"],
        "verification": {"required": ["tests"]},
        "verifiers": [{"name": "tests", "type": "command", "command": "true"}],
        "stop_conditions": {
            "max_iterations": 1,
            "max_runtime_minutes": 5,
            "max_cost_usd": 1,
            "no_progress_iterations": 1,
        },
        "retry": {"enabled": False, "max_attempts": 1},
        "human_gates": [],
    }
    data.update(overrides)
    path.write_text(yaml.safe_dump(data))
    return path


def test_init_creates_loop_yaml(tmp_path):
    result = _run("init", "--dir", str(tmp_path))

    assert result.returncode == 0
    created = tmp_path / ".loop" / "loop.yaml"
    assert created.exists()
    data = yaml.safe_load(created.read_text())
    assert data["name"]
    assert data["trigger"]["type"]
    assert data["goal"]["type"]


def test_init_refuses_to_overwrite_without_force(tmp_path):
    _run("init", "--dir", str(tmp_path))

    result = _run("init", "--dir", str(tmp_path))

    assert result.returncode != 0
    assert "exists" in (result.stdout + result.stderr).lower()


def test_init_force_overwrites(tmp_path):
    _run("init", "--dir", str(tmp_path))

    result = _run("init", "--dir", str(tmp_path), "--force")

    assert result.returncode == 0


def test_init_gitlab_issue_template(tmp_path):
    result = _run("init", "--dir", str(tmp_path), "--template", "gitlab-issue")

    assert result.returncode == 0
    data = yaml.safe_load((tmp_path / ".loop" / "loop.yaml").read_text())
    assert data["goal"]["type"] == "issue_resolution"


def test_init_unknown_template_lists_choices(tmp_path):
    result = _run("init", "--dir", str(tmp_path), "--template", "does-not-exist")

    assert result.returncode != 0
    assert "generic" in result.stderr


@pytest.mark.parametrize("template_name", _TEMPLATE_NAMES)
def test_every_template_inits_validates_and_audits_cleanly(tmp_path, template_name):
    init_result = _run("init", "--dir", str(tmp_path), "--template", template_name)
    assert init_result.returncode == 0

    loop_yaml = tmp_path / ".loop" / "loop.yaml"

    validate_result = _run("validate", str(loop_yaml))
    assert validate_result.returncode == 0, validate_result.stdout + validate_result.stderr

    audit_result = _run("audit", str(loop_yaml))
    assert audit_result.returncode == 0, audit_result.stdout + audit_result.stderr
    assert "FAIL" not in audit_result.stdout


def test_validate_valid_definition(tmp_path):
    path = _write_definition(tmp_path / "loop.yaml")

    result = _run("validate", str(path))

    assert result.returncode == 0
    assert "valid" in result.stdout.lower()


def test_validate_invalid_definition(tmp_path):
    path = tmp_path / "loop.yaml"
    path.write_text(yaml.safe_dump({"name": "broken", "version": 1}))

    result = _run("validate", str(path))

    assert result.returncode == 1


def test_audit_delegates_and_reports_score(tmp_path):
    path = _write_definition(tmp_path / "loop.yaml")

    result = _run("audit", str(path))

    assert "Loop Ready Score" in result.stdout


def test_run_completes_and_persists_result(tmp_path):
    path = _write_definition(tmp_path / "loop.yaml")
    results_dir = tmp_path / "results"

    result = _run("run", str(path), "--results-dir", str(results_dir))

    assert result.returncode == 0
    assert "completed" in result.stdout.lower()
    written = list(results_dir.glob("*/result.json"))
    assert len(written) == 1


def test_run_reports_failure_exit_code(tmp_path):
    path = _write_definition(
        tmp_path / "loop.yaml", verifiers=[{"name": "tests", "type": "command", "command": "false"}]
    )
    results_dir = tmp_path / "results"

    result = _run("run", str(path), "--results-dir", str(results_dir))

    assert result.returncode == 1


def test_status_reports_latest_run(tmp_path):
    path = _write_definition(tmp_path / "loop.yaml")
    results_dir = tmp_path / "results"
    _run("run", str(path), "--results-dir", str(results_dir))

    result = _run("status", "--results-dir", str(results_dir))

    assert result.returncode == 0
    assert "cli-test-loop" in result.stdout


def test_status_no_runs_yet(tmp_path):
    results_dir = tmp_path / "results"

    result = _run("status", "--results-dir", str(results_dir))

    assert result.returncode == 0
    assert "no runs" in result.stdout.lower()


def test_inspect_shows_iteration_detail(tmp_path):
    path = _write_definition(tmp_path / "loop.yaml")
    results_dir = tmp_path / "results"
    run_result = _run("run", str(path), "--results-dir", str(results_dir))
    run_id = [line for line in run_result.stdout.splitlines() if "run_id" in line.lower()][0]

    result = _run("inspect", run_id.split()[-1], "--results-dir", str(results_dir))

    assert result.returncode == 0
    assert "tests" in result.stdout


def test_inspect_unknown_run_id(tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()

    result = _run("inspect", "run_does_not_exist", "--results-dir", str(results_dir))

    assert result.returncode == 1


def test_cost_aggregates_across_runs(tmp_path):
    path = _write_definition(tmp_path / "loop.yaml")
    results_dir = tmp_path / "results"
    _run("run", str(path), "--results-dir", str(results_dir))
    _run("run", str(path), "--results-dir", str(results_dir))

    result = _run("cost", "--results-dir", str(results_dir))

    assert result.returncode == 0
    assert "Runs" in result.stdout
    assert "2" in result.stdout


def test_doctor_reports_health(tmp_path):
    path = _write_definition(tmp_path / "loop.yaml")

    result = _run("doctor", str(path))

    assert result.returncode == 0
    assert "Loop Health" in result.stdout


def test_replay_reads_the_same_run_as_inspect(tmp_path):
    path = _write_definition(tmp_path / "loop.yaml")
    results_dir = tmp_path / "results"
    run_result = _run("run", str(path), "--results-dir", str(results_dir))
    run_id = [line for line in run_result.stdout.splitlines() if "run_id" in line.lower()][0].split()[-1]

    result = _run("replay", run_id, "--results-dir", str(results_dir))

    assert result.returncode == 0
    assert "tests" in result.stdout
