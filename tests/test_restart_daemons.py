import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "scripts" / "restart-daemons.sh"

DASHBOARD_LABEL = "com.hermes.loop-engineering-dashboard"
SCHEDULER_LABEL = "com.hermes.loop-engineering"


def make_fake_launchctl(tmp_path, loaded_labels=()):
    """Writes a fake `launchctl` onto its own PATH-prependable dir that logs
    every call and answers `list <label>` based on `loaded_labels`, instead
    of touching this machine's real launchd - which could otherwise collide
    with this repo's own real, currently-running daemons (see
    tests/test_install.py's make_fake_launchctl, same shape)."""
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir(exist_ok=True)
    log_path = tmp_path / "launchctl_calls.txt"
    script = fake_bin / "launchctl"
    loaded = " ".join(loaded_labels)
    script.write_text(f"""#!/usr/bin/env bash
echo "$@" >> {str(log_path)!r}
if [ "$1" = "list" ]; then
  for label in {loaded}; do
    if [ "$label" = "$2" ]; then
      exit 0
    fi
  done
  exit 1
fi
exit 0
""")
    script.chmod(0o755)
    return fake_bin, log_path


def env_with_fake_launchctl(fake_bin):
    return {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}


def make_launchd_dir(tmp_path, labels=(DASHBOARD_LABEL, SCHEDULER_LABEL)):
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    for label in labels:
        (launchd_dir / f"{label}.plist").write_text("<plist/>\n")
    return launchd_dir


def run_restart(*args, env, check=True):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        check=check, capture_output=True, text=True, env=env,
    )


def test_restarts_the_dashboard_when_loaded(tmp_path):
    launchd_dir = make_launchd_dir(tmp_path)
    fake_bin, log_path = make_fake_launchctl(tmp_path, loaded_labels=[DASHBOARD_LABEL])

    result = run_restart("--launchd-dir", str(launchd_dir), env=env_with_fake_launchctl(fake_bin))

    calls = log_path.read_text().splitlines()
    assert f"kickstart -k gui/{os.getuid()}/{DASHBOARD_LABEL}" in calls
    # SCHEDULER_LABEL is a substring of DASHBOARD_LABEL, so this must be an
    # exact-line check against `calls`, not a substring search.
    assert f"kickstart -k gui/{os.getuid()}/{SCHEDULER_LABEL}" not in calls
    assert f"{DASHBOARD_LABEL} restarted" in result.stdout
    assert f"{SCHEDULER_LABEL} is not loaded" in result.stdout


def test_skips_daemons_that_are_not_loaded(tmp_path):
    launchd_dir = make_launchd_dir(tmp_path)
    fake_bin, log_path = make_fake_launchctl(tmp_path, loaded_labels=[])

    result = run_restart("--launchd-dir", str(launchd_dir), env=env_with_fake_launchctl(fake_bin))

    assert "kickstart" not in log_path.read_text()
    assert f"{DASHBOARD_LABEL} is not loaded, skipping" in result.stdout
    assert f"{SCHEDULER_LABEL} is not loaded, skipping" in result.stdout


def test_scheduler_not_restarted_without_the_flag_even_when_loaded(tmp_path):
    """The whole point of --with-scheduler being opt-in: kickstarting the
    scheduler forces an immediate poll, which can trigger a real run
    against live GitLab/Slack right now - a manual restart must not do
    that by accident."""
    launchd_dir = make_launchd_dir(tmp_path)
    fake_bin, log_path = make_fake_launchctl(tmp_path, loaded_labels=[SCHEDULER_LABEL])

    result = run_restart("--launchd-dir", str(launchd_dir), env=env_with_fake_launchctl(fake_bin))

    assert f"kickstart -k gui/{os.getuid()}/{SCHEDULER_LABEL}" not in log_path.read_text().splitlines()
    assert f"{SCHEDULER_LABEL} not restarted (pass --with-scheduler" in result.stdout


def test_with_scheduler_flag_also_restarts_the_scheduler_when_loaded(tmp_path):
    launchd_dir = make_launchd_dir(tmp_path)
    fake_bin, log_path = make_fake_launchctl(tmp_path, loaded_labels=[DASHBOARD_LABEL, SCHEDULER_LABEL])

    result = run_restart(
        "--launchd-dir", str(launchd_dir), "--with-scheduler",
        env=env_with_fake_launchctl(fake_bin),
    )

    calls = log_path.read_text().splitlines()
    assert f"kickstart -k gui/{os.getuid()}/{DASHBOARD_LABEL}" in calls
    assert f"kickstart -k gui/{os.getuid()}/{SCHEDULER_LABEL}" in calls
    assert "may have just triggered a live poll/run" in result.stdout


def test_falls_back_to_known_agent_names_without_a_local_launchd_dir(tmp_path):
    fake_bin, log_path = make_fake_launchctl(tmp_path, loaded_labels=[DASHBOARD_LABEL])

    result = run_restart(
        "--launchd-dir", str(tmp_path / "does-not-exist"),
        env=env_with_fake_launchctl(fake_bin),
    )

    assert f"kickstart -k gui/{os.getuid()}/{DASHBOARD_LABEL}" in log_path.read_text().splitlines()
    assert "falling back to this repo's known agent names" in result.stdout


def test_reports_when_launchd_dir_exists_but_is_empty(tmp_path):
    empty_dir = tmp_path / "launchd"
    empty_dir.mkdir()
    fake_bin, log_path = make_fake_launchctl(tmp_path, loaded_labels=[DASHBOARD_LABEL])

    result = run_restart("--launchd-dir", str(empty_dir), env=env_with_fake_launchctl(fake_bin))

    assert not log_path.exists()
    assert f"No *.plist files found in {empty_dir}" in result.stdout


def test_rejects_unknown_flag():
    result = subprocess.run(
        ["bash", str(SCRIPT), "--nope"], capture_output=True, text=True,
    )

    assert result.returncode == 1
    assert "Usage" in result.stderr
