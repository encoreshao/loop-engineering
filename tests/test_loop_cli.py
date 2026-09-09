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


def test_run_with_prompt_invokes_a_real_agent_and_persists_it(tmp_path):
    import json
    import os

    path = _write_definition(tmp_path / "loop.yaml")
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("fix the thing")
    results_dir = tmp_path / "results"

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'result': 'done', 'total_cost_usd': 0.05, 'usage': {}, 'modelUsage': {}}))\n"
    )
    fake_claude.chmod(fake_claude.stat().st_mode | 0o111)
    loop_home = tmp_path / "loop-home"  # empty, unset LOOP_ENGINEERING_HOME - ai_cli_config.get_selected_cli()
    # must resolve deterministically to "claude" (its safe-fallback default), never the real
    # machine's ~/.loop-engineering/ai_cli.json, per CLAUDE.md's sandboxed-testing rule.
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "LOOP_ENGINEERING_HOME": str(loop_home)}

    result = subprocess.run(
        [sys.executable, str(CLI), "run", str(path), "--results-dir", str(results_dir),
         "--prompt-file", str(prompt_file)],
        capture_output=True, text=True, env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "completed" in result.stdout.lower()
    written = list(results_dir.glob("*/result.json"))
    assert len(written) == 1
    data = json.loads(written[0].read_text())
    assert data["prompt"] == "fix the thing"
    assert data["definition_path"] == str(path)


def test_run_with_prompt_text_directly_invokes_a_real_agent_and_persists_it(tmp_path):
    import json
    import os

    path = _write_definition(tmp_path / "loop.yaml")
    results_dir = tmp_path / "results"

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'result': 'done', 'total_cost_usd': 0.05, 'usage': {}, 'modelUsage': {}}))\n"
    )
    fake_claude.chmod(fake_claude.stat().st_mode | 0o111)
    loop_home = tmp_path / "loop-home"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "LOOP_ENGINEERING_HOME": str(loop_home)}

    result = subprocess.run(
        [sys.executable, str(CLI), "run", str(path), "--results-dir", str(results_dir),
         "--prompt", "fix the thing directly"],
        capture_output=True, text=True, env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "completed" in result.stdout.lower()
    written = list(results_dir.glob("*/result.json"))
    assert len(written) == 1
    data = json.loads(written[0].read_text())
    assert data["prompt"] == "fix the thing directly"
    assert data["definition_path"] == str(path.resolve())


def test_run_with_prompt_reports_failure_when_agent_fails(tmp_path):
    import os

    path = _write_definition(tmp_path / "loop.yaml")
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("fix the thing")
    results_dir = tmp_path / "results"

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    fake_claude.write_text("#!/usr/bin/env python3\nimport sys\nprint('boom')\nsys.exit(1)\n")
    fake_claude.chmod(fake_claude.stat().st_mode | 0o111)
    loop_home = tmp_path / "loop-home"  # empty, unset LOOP_ENGINEERING_HOME - ai_cli_config.get_selected_cli()
    # must resolve deterministically to "claude" (its safe-fallback default), never the real
    # machine's ~/.loop-engineering/ai_cli.json, per CLAUDE.md's sandboxed-testing rule.
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "LOOP_ENGINEERING_HOME": str(loop_home)}

    result = subprocess.run(
        [sys.executable, str(CLI), "run", str(path), "--results-dir", str(results_dir),
         "--prompt-file", str(prompt_file)],
        capture_output=True, text=True, env=env,
    )

    assert result.returncode == 1


def test_replay_reinvokes_the_agent_and_writes_a_new_run(tmp_path):
    import os

    path = _write_definition(tmp_path / "loop.yaml")
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("fix the thing")
    results_dir = tmp_path / "results"

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'result': 'done', 'total_cost_usd': 0.05, 'usage': {}, 'modelUsage': {}}))\n"
    )
    fake_claude.chmod(fake_claude.stat().st_mode | 0o111)
    loop_home = tmp_path / "loop-home"  # empty, unset LOOP_ENGINEERING_HOME - ai_cli_config.get_selected_cli()
    # must resolve deterministically to "claude" (its safe-fallback default), never the real
    # machine's ~/.loop-engineering/ai_cli.json, per CLAUDE.md's sandboxed-testing rule.
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "LOOP_ENGINEERING_HOME": str(loop_home)}

    run_result = subprocess.run(
        [sys.executable, str(CLI), "run", str(path), "--results-dir", str(results_dir),
         "--prompt-file", str(prompt_file)],
        capture_output=True, text=True, env=env,
    )
    run_id = [line for line in run_result.stdout.splitlines() if "run_id" in line.lower()][0].split()[-1]

    replay_result = subprocess.run(
        [sys.executable, str(CLI), "replay", run_id, "--results-dir", str(results_dir)],
        capture_output=True, text=True, env=env,
    )

    assert replay_result.returncode == 0, replay_result.stdout + replay_result.stderr
    written = sorted(results_dir.glob("*/result.json"))
    assert len(written) == 2  # the original run + replay's new run
    replay_run_ids = {p.parent.name for p in written} - {run_id}
    assert len(replay_run_ids) == 1


def test_replay_reinvokes_agent_when_recorded_prompt_was_an_empty_string(tmp_path):
    import os

    path = _write_definition(tmp_path / "loop.yaml")
    results_dir = tmp_path / "results"

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'result': 'done', 'total_cost_usd': 0.05, 'usage': {}, 'modelUsage': {}}))\n"
    )
    fake_claude.chmod(fake_claude.stat().st_mode | 0o111)
    loop_home = tmp_path / "loop-home"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "LOOP_ENGINEERING_HOME": str(loop_home)}

    run_result = subprocess.run(
        [sys.executable, str(CLI), "run", str(path), "--results-dir", str(results_dir),
         "--prompt", ""],
        capture_output=True, text=True, env=env,
    )
    run_id = [line for line in run_result.stdout.splitlines() if "run_id" in line.lower()][0].split()[-1]

    replay_result = subprocess.run(
        [sys.executable, str(CLI), "replay", run_id, "--results-dir", str(results_dir)],
        capture_output=True, text=True, env=env,
    )

    assert replay_result.returncode == 0, replay_result.stdout + replay_result.stderr
    assert "no prompt recorded" not in replay_result.stdout.lower()
    written = sorted(results_dir.glob("*/result.json"))
    assert len(written) == 2


def test_replay_falls_back_to_inspect_when_no_prompt_was_recorded(tmp_path):
    path = _write_definition(tmp_path / "loop.yaml")
    results_dir = tmp_path / "results"
    run_result = _run("run", str(path), "--results-dir", str(results_dir))
    run_id = [line for line in run_result.stdout.splitlines() if "run_id" in line.lower()][0].split()[-1]

    result = _run("replay", run_id, "--results-dir", str(results_dir))

    assert result.returncode == 0
    assert "no prompt recorded" in result.stdout.lower()
    assert "tests" in result.stdout


def test_run_writes_running_status_visible_mid_run_then_finished(tmp_path):
    import json
    import os
    import time as _time

    path = _write_definition(
        tmp_path / "loop.yaml",
        verifiers=[{"name": "tests", "type": "command", "command": "false"}],
        stop_conditions={
            "max_iterations": 5, "max_runtime_minutes": 5, "max_cost_usd": 5, "no_progress_iterations": 5,
        },
        retry={"enabled": True, "max_attempts": 2},
    )
    results_dir = tmp_path / "results"

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, time\n"
        "time.sleep(1.2)\n"
        "print(json.dumps({'result': 'done', 'total_cost_usd': 0.05, 'usage': {}, 'modelUsage': {}}))\n"
    )
    fake_claude.chmod(fake_claude.stat().st_mode | 0o111)
    loop_home = tmp_path / "loop-home"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "LOOP_ENGINEERING_HOME": str(loop_home)}

    proc = subprocess.Popen(
        [sys.executable, str(CLI), "run", str(path), "--results-dir", str(results_dir),
         "--prompt", "try to fix it"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = _time.monotonic() + 10
        seen_running = False
        running_snapshot = None
        while _time.monotonic() < deadline:
            written = list(results_dir.glob("*/result.json"))
            if written:
                data = json.loads(written[0].read_text())
                if data.get("status") == "running":
                    seen_running = True
                    running_snapshot = data
                    break
            _time.sleep(0.05)
        assert seen_running, "never observed an in-progress (status=running) result.json"
        assert running_snapshot["final_state"] == "running"
        assert running_snapshot["stop_reason"] == "running"
    finally:
        stdout, stderr = proc.communicate(timeout=15)

    written = list(results_dir.glob("*/result.json"))
    assert len(written) == 1
    data = json.loads(written[0].read_text())
    assert data["status"] == "finished", stdout + stderr
    assert data["final_state"] == "escalated"
    assert len(data["iterations"]) == 2


def test_run_shows_running_status_immediately_even_for_a_single_iteration_run(tmp_path):
    import json
    import os
    import time as _time

    path = _write_definition(tmp_path / "loop.yaml")  # default: single passing iteration, no retry needed
    results_dir = tmp_path / "results"

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, time\n"
        "time.sleep(1.0)\n"
        "print(json.dumps({'result': 'done', 'total_cost_usd': 0.05, 'usage': {}, 'modelUsage': {}}))\n"
    )
    fake_claude.chmod(fake_claude.stat().st_mode | 0o111)
    loop_home = tmp_path / "loop-home"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "LOOP_ENGINEERING_HOME": str(loop_home)}

    proc = subprocess.Popen(
        [sys.executable, str(CLI), "run", str(path), "--results-dir", str(results_dir),
         "--prompt", "fix it"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = _time.monotonic() + 5
        seen_running_with_zero_iterations = False
        while _time.monotonic() < deadline:
            written = list(results_dir.glob("*/result.json"))
            if written:
                data = json.loads(written[0].read_text())
                if data.get("status") == "running" and data.get("iterations") == []:
                    seen_running_with_zero_iterations = True
                    break
            _time.sleep(0.02)
        assert seen_running_with_zero_iterations, "never observed the initial 0-iteration running snapshot"
    finally:
        proc.communicate(timeout=10)

    written = list(results_dir.glob("*/result.json"))
    data = json.loads(written[0].read_text())
    assert data["status"] == "finished"
    assert data["final_state"] == "completed"


def test_status_shows_running_run_with_iteration_cost_and_budget_bar(tmp_path):
    import json

    results_dir = tmp_path / "results"
    run_dir = results_dir / "run_running_1"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(json.dumps({
        "loop_id": "loop_1",
        "run_id": "run_running_1",
        "definition_name": "live-status-loop",
        "final_state": "running",
        "stop_reason": "running",
        "status": "running",
        "iterations": [
            {
                "iteration": 2,
                "state": "evaluating",
                "verification_results": [],
                "budget": {
                    "iterations": {"status": "warning", "used": 2, "limit": 3},
                    "runtime": {"status": "ok", "used_seconds": 12.0, "limit_seconds": 900},
                    "cost": {"status": "ok", "used_usd": 0.73, "limit_usd": 5},
                    "overall": "warning",
                },
                "progressed": True,
            }
        ],
    }))

    result = _run("status", "--results-dir", str(results_dir))

    assert result.returncode == 0
    assert "live-status-loop" in result.stdout
    assert "Status: RUNNING" in result.stdout
    assert "Iteration: 2/3" in result.stdout
    assert "Cost: $0.73" in result.stdout
    assert "██████░░░░ 67%" in result.stdout


def test_status_running_run_with_zero_iteration_limit_does_not_crash(tmp_path):
    import json

    results_dir = tmp_path / "results"
    run_dir = results_dir / "run_running_zero_limit"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(json.dumps({
        "loop_id": "loop_1",
        "run_id": "run_running_zero_limit",
        "definition_name": "zero-limit-loop",
        "final_state": "running",
        "stop_reason": "running",
        "status": "running",
        "iterations": [
            {
                "iteration": 0,
                "state": "evaluating",
                "verification_results": [],
                "budget": {
                    "iterations": {"status": "warning", "used": 0, "limit": 0},
                    "runtime": {"status": "ok", "used_seconds": 1.0, "limit_seconds": 900},
                    "cost": {"status": "ok", "used_usd": 0.0, "limit_usd": 5},
                    "overall": "warning",
                },
                "progressed": False,
            }
        ],
    }))

    result = _run("status", "--results-dir", str(results_dir))

    assert result.returncode == 0
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
    assert "Iteration: 0" in result.stdout
    assert "/0" not in result.stdout
    assert "Budget:" not in result.stdout


def test_status_finished_run_output_has_no_running_branch(tmp_path):
    path = _write_definition(tmp_path / "loop.yaml")
    results_dir = tmp_path / "results"
    _run("run", str(path), "--results-dir", str(results_dir))

    result = _run("status", "--results-dir", str(results_dir))

    assert result.returncode == 0
    assert "Status: RUNNING" not in result.stdout
    assert "cli-test-loop" in result.stdout
