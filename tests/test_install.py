import os
import re
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "scripts" / "install.sh"

FAKE_SETUP_SH = """#!/usr/bin/env bash
set -euo pipefail
OUT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/setup_ran.txt"
printf '%s\\n' "$*" > "$OUT"
"""

FAKE_SETUP_NGINX_SH = """#!/usr/bin/env bash
set -euo pipefail
OUT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/setup_nginx_ran.txt"
printf '%s\\n' "$*" > "$OUT"
exit "${FAKE_SETUP_NGINX_EXIT_CODE:-0}"
"""

# Minimal but valid plists, carrying the same {{PYTHON3}}/{{LOOP_DIR}}
# placeholders as the real launchd/*.plist.template files, so a test can
# assert install.sh actually substitutes them for the installing machine.
DASHBOARD_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.hermes.loop-engineering-dashboard</string>
  <key>ProgramArguments</key>
  <array>
    <string>{{PYTHON3}}</string>
    <string>{{LOOP_DIR}}/bin/web/dashboard_server.py</string>
    <string>{{PORT}}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
</dict>
</plist>
"""

LOOP_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.hermes.loop-engineering</string>
  <key>ProgramArguments</key>
  <array>
    <string>{{LOOP_DIR}}/run-loop.sh</string>
  </array>
  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
"""


def make_origin(tmp_path, with_launchd_fixtures=False):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)

    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)

    setup_sh = seed / "bin" / "scripts" / "setup.sh"
    setup_sh.parent.mkdir(parents=True)
    setup_sh.write_text(FAKE_SETUP_SH)
    setup_sh.chmod(0o755)
    to_add = ["bin/scripts/setup.sh"]

    if with_launchd_fixtures:
        nginx_sh = seed / "bin" / "scripts" / "setup-nginx.sh"
        nginx_sh.write_text(FAKE_SETUP_NGINX_SH)
        nginx_sh.chmod(0o755)
        to_add.append("bin/scripts/setup-nginx.sh")

        launchd_dir = seed / "launchd"
        launchd_dir.mkdir()
        (launchd_dir / "com.hermes.loop-engineering-dashboard.plist.template").write_text(DASHBOARD_PLIST_TEMPLATE)
        (launchd_dir / "com.hermes.loop-engineering.plist.template").write_text(LOOP_PLIST_TEMPLATE)
        to_add.append("launchd")

    subprocess.run(["git", "-C", str(seed), "add", *to_add], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "seed"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "-u", "origin", "main"], check=True, capture_output=True)
    return origin


def make_fake_launchctl(tmp_path, exit_code=0, already_loaded=False):
    """Writes a fake `launchctl` onto its own PATH-prependable dir that just
    logs its arguments, instead of touching this machine's real launchd
    (which would risk colliding with the label of the actual live dashboard
    daemon this repo runs as).

    Real launchctl's `list <label>` fails when the label isn't loaded yet
    and succeeds once it is - install.sh branches on exactly that exit code
    to decide `load -w` (first time) vs `kickstart -k` (already running).
    A fake that returned the same `exit_code` for every subcommand couldn't
    represent "not loaded yet", so `list` is driven by `already_loaded`
    instead; every other subcommand (load, kickstart, ...) still uses
    `exit_code`.
    """
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir(exist_ok=True)
    log_path = tmp_path / "launchctl_calls.txt"
    script = fake_bin / "launchctl"
    list_exit_code = 0 if already_loaded else 1
    script.write_text(f"""#!/usr/bin/env bash
echo "$@" >> {str(log_path)!r}
if [ "$1" = "list" ]; then
  exit {list_exit_code}
fi
exit {exit_code}
""")
    script.chmod(0o755)
    return fake_bin, log_path


def env_with_fake_launchctl(fake_bin, extra=None):
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}
    if extra:
        env.update(extra)
    return env


def run_install(*args, check=True, env=None):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        check=check, capture_output=True, text=True, env=env,
    )


def test_install_clones_repo_and_runs_setup(tmp_path):
    origin = make_origin(tmp_path)
    target = tmp_path / "clone"

    run_install("--repo-url", str(origin), "--dir", str(target))

    assert (target / ".git").exists()
    assert (target / "setup_ran.txt").exists()


def test_install_forwards_setup_flags(tmp_path):
    origin = make_origin(tmp_path)
    target = tmp_path / "clone"

    run_install(
        "--repo-url", str(origin), "--dir", str(target),
        "--skip-skills-install", "--config-path", "/tmp/projects.json",
        "--topics-config-path", "/tmp/topics.json",
    )

    forwarded = (target / "setup_ran.txt").read_text().strip()
    assert forwarded == "--skip-skills-install --config-path /tmp/projects.json --topics-config-path /tmp/topics.json"


def test_install_pulls_latest_when_dir_already_a_clone(tmp_path):
    origin = make_origin(tmp_path)
    target = tmp_path / "clone"

    run_install("--repo-url", str(origin), "--dir", str(target))
    first_commit = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()

    # push a second commit to origin
    seed = tmp_path / "seed"
    (seed / "extra.txt").write_text("more\n")
    subprocess.run(["git", "-C", str(seed), "add", "extra.txt"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "second"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push"], check=True, capture_output=True)

    run_install("--repo-url", str(origin), "--dir", str(target))
    second_commit = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()

    assert second_commit != first_commit
    assert (target / "extra.txt").exists()


def test_install_rejects_existing_non_git_dir(tmp_path):
    origin = make_origin(tmp_path)
    target = tmp_path / "clone"
    target.mkdir()
    (target / "some-other-file.txt").write_text("not a clone\n")

    result = run_install("--repo-url", str(origin), "--dir", str(target), check=False)

    assert result.returncode != 0
    assert not (target / ".git").exists()


def test_install_rejects_unknown_flag():
    result = run_install("--not-a-real-flag", check=False)

    assert result.returncode != 0


def test_install_defaults_dir_to_dot_loop_engineering_under_home(tmp_path):
    origin = make_origin(tmp_path)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {**os.environ, "HOME": str(fake_home)}

    run_install("--repo-url", str(origin), env=env)

    target = fake_home / ".loop-engineering"
    assert (target / ".git").exists()
    assert (target / "setup_ran.txt").exists()


def test_install_renders_launchd_plist_templates_for_this_machine(tmp_path):
    origin = make_origin(tmp_path, with_launchd_fixtures=True)
    target = tmp_path / "clone"
    fake_bin, _ = make_fake_launchctl(tmp_path)

    run_install(
        "--repo-url", str(origin), "--dir", str(target),
        "--launch-agents-dir", str(tmp_path / "LaunchAgents"),
        "--port", "90210",
        env=env_with_fake_launchctl(fake_bin),
    )

    dashboard_plist = (target / "launchd" / "com.hermes.loop-engineering-dashboard.plist").read_text()
    assert "{{" not in dashboard_plist
    assert str(target) in dashboard_plist
    assert shutil.which("python3") in dashboard_plist
    assert "<string>90210</string>" in dashboard_plist

    loop_plist = (target / "launchd" / "com.hermes.loop-engineering.plist").read_text()
    assert "{{" not in loop_plist
    assert str(target) in loop_plist


def test_install_upgrade_does_not_clobber_an_already_rendered_plist(tmp_path):
    """Once launchd/<name>.plist has been rendered from its .plist.template,
    a later --upgrade must leave it alone: the dashboard's Daemons page
    "Save schedule" feature (update_daemon_schedule in dashboard_server.py)
    treats that rendered .plist as its source of truth and rewrites its
    StartCalendarInterval directly. Unconditionally re-rendering it from the
    template on every install/upgrade run - the previous behavior - silently
    reverted any schedule the user had saved from the dashboard back to the
    template's original default the next time --upgrade ran."""
    origin = make_origin(tmp_path, with_launchd_fixtures=True)
    target = tmp_path / "clone"
    fake_bin, _ = make_fake_launchctl(tmp_path)
    launch_agents_dir = tmp_path / "LaunchAgents"

    run_install(
        "--repo-url", str(origin), "--dir", str(target),
        "--launch-agents-dir", str(launch_agents_dir),
        env=env_with_fake_launchctl(fake_bin),
    )

    loop_plist_path = target / "launchd" / "com.hermes.loop-engineering.plist"
    # Simulate a schedule saved from the dashboard's Daemons page: the
    # rendered plist's content no longer matches what a fresh render from
    # the template would produce.
    customized = loop_plist_path.read_text().replace("<false/>", "<true/>")
    assert customized != loop_plist_path.read_text()
    loop_plist_path.write_text(customized)

    run_install(
        "--repo-url", str(origin), "--dir", str(target), "--upgrade",
        "--launch-agents-dir", str(launch_agents_dir),
        env=env_with_fake_launchctl(fake_bin),
    )

    assert loop_plist_path.read_text() == customized


def test_install_runs_nginx_setup_and_loads_dashboard_daemon_by_default(tmp_path):
    origin = make_origin(tmp_path, with_launchd_fixtures=True)
    target = tmp_path / "clone"
    launch_agents_dir = tmp_path / "LaunchAgents"
    fake_bin, launchctl_log = make_fake_launchctl(tmp_path)

    run_install(
        "--repo-url", str(origin), "--dir", str(target),
        "--launch-agents-dir", str(launch_agents_dir),
        env=env_with_fake_launchctl(fake_bin),
    )

    assert (target / "setup_nginx_ran.txt").exists()
    assert (launch_agents_dir / "com.hermes.loop-engineering-dashboard.plist").exists()
    logged = launchctl_log.read_text()
    assert "load" in logged
    assert "-w" in logged


def test_install_reports_loop_local_when_nginx_succeeds(tmp_path):
    origin = make_origin(tmp_path, with_launchd_fixtures=True)
    target = tmp_path / "clone"
    launch_agents_dir = tmp_path / "LaunchAgents"
    fake_bin, _ = make_fake_launchctl(tmp_path)

    result = run_install(
        "--repo-url", str(origin), "--dir", str(target),
        "--launch-agents-dir", str(launch_agents_dir),
        "--port", "84250",
        env=env_with_fake_launchctl(fake_bin),
    )

    assert "http://loop.local" in result.stdout
    assert "http://127.0.0.1:84250" not in result.stdout


def test_install_reports_direct_url_when_nginx_skipped(tmp_path):
    origin = make_origin(tmp_path, with_launchd_fixtures=True)
    target = tmp_path / "clone"
    launch_agents_dir = tmp_path / "LaunchAgents"
    fake_bin, _ = make_fake_launchctl(tmp_path)

    result = run_install(
        "--repo-url", str(origin), "--dir", str(target),
        "--launch-agents-dir", str(launch_agents_dir),
        "--skip-nginx", "--port", "84250",
        env=env_with_fake_launchctl(fake_bin),
    )

    assert "http://127.0.0.1:84250" in result.stdout
    assert "http://loop.local" not in result.stdout


def test_install_reports_direct_url_when_nginx_fails(tmp_path):
    origin = make_origin(tmp_path, with_launchd_fixtures=True)
    target = tmp_path / "clone"
    launch_agents_dir = tmp_path / "LaunchAgents"
    fake_bin, _ = make_fake_launchctl(tmp_path)

    result = run_install(
        "--repo-url", str(origin), "--dir", str(target),
        "--launch-agents-dir", str(launch_agents_dir),
        "--port", "84250",
        env=env_with_fake_launchctl(fake_bin, {"FAKE_SETUP_NGINX_EXIT_CODE": "1"}),
    )

    assert "http://127.0.0.1:84250" in result.stdout
    assert "http://loop.local" not in result.stdout


def test_install_picks_random_port_in_range_when_not_given(tmp_path):
    origin = make_origin(tmp_path, with_launchd_fixtures=True)
    target = tmp_path / "clone"
    launch_agents_dir = tmp_path / "LaunchAgents"
    fake_bin, _ = make_fake_launchctl(tmp_path)

    run_install(
        "--repo-url", str(origin), "--dir", str(target),
        "--launch-agents-dir", str(launch_agents_dir),
        "--skip-nginx",
        env=env_with_fake_launchctl(fake_bin),
    )

    dashboard_plist = (target / "launchd" / "com.hermes.loop-engineering-dashboard.plist").read_text()
    match = re.search(r"<string>(\d{5})</string>", dashboard_plist)
    assert match, "no 5-digit port string found in the rendered dashboard plist"
    port = int(match.group(1))
    assert 48420 <= port <= 48620


def test_install_forwards_port_to_nginx_setup(tmp_path):
    origin = make_origin(tmp_path, with_launchd_fixtures=True)
    target = tmp_path / "clone"
    launch_agents_dir = tmp_path / "LaunchAgents"
    fake_bin, _ = make_fake_launchctl(tmp_path)

    run_install(
        "--repo-url", str(origin), "--dir", str(target),
        "--launch-agents-dir", str(launch_agents_dir),
        "--port", "84250",
        env=env_with_fake_launchctl(fake_bin),
    )

    forwarded = (target / "setup_nginx_ran.txt").read_text().strip()
    assert forwarded == "--port 84250"


def test_install_upgrade_does_not_change_an_already_rendered_port(tmp_path):
    """Same 'don't clobber' rule as the {{PYTHON3}}/{{LOOP_DIR}} placeholders
    and the loop plist's saved schedule (see
    test_install_upgrade_does_not_clobber_an_already_rendered_plist): once
    the dashboard plist has been rendered once with a port baked in, a later
    --upgrade (even with a different --port) must leave that port alone -
    otherwise every upgrade would silently move the dashboard to a new URL."""
    origin = make_origin(tmp_path, with_launchd_fixtures=True)
    target = tmp_path / "clone"
    launch_agents_dir = tmp_path / "LaunchAgents"
    fake_bin, _ = make_fake_launchctl(tmp_path)

    run_install(
        "--repo-url", str(origin), "--dir", str(target),
        "--launch-agents-dir", str(launch_agents_dir),
        "--skip-nginx", "--port", "84250",
        env=env_with_fake_launchctl(fake_bin),
    )

    result = run_install(
        "--repo-url", str(origin), "--dir", str(target), "--upgrade",
        "--launch-agents-dir", str(launch_agents_dir),
        "--skip-nginx", "--port", "84299",
        env=env_with_fake_launchctl(fake_bin),
    )

    dashboard_plist = (target / "launchd" / "com.hermes.loop-engineering-dashboard.plist").read_text()
    assert "<string>84250</string>" in dashboard_plist
    assert "<string>84299</string>" not in dashboard_plist
    assert "http://127.0.0.1:84250" in result.stdout


def test_install_skip_flags_skip_nginx_and_launchd_daemons(tmp_path):
    origin = make_origin(tmp_path, with_launchd_fixtures=True)
    target = tmp_path / "clone"
    launch_agents_dir = tmp_path / "LaunchAgents"
    fake_bin, launchctl_log = make_fake_launchctl(tmp_path)

    run_install(
        "--repo-url", str(origin), "--dir", str(target),
        "--launch-agents-dir", str(launch_agents_dir),
        "--skip-nginx", "--skip-launchd-daemons",
        env=env_with_fake_launchctl(fake_bin),
    )

    assert not (target / "setup_nginx_ran.txt").exists()
    assert not (launch_agents_dir / "com.hermes.loop-engineering-dashboard.plist").exists()
    assert not launchctl_log.exists()


def test_install_nginx_failure_warns_and_continues(tmp_path):
    origin = make_origin(tmp_path, with_launchd_fixtures=True)
    target = tmp_path / "clone"
    launch_agents_dir = tmp_path / "LaunchAgents"
    fake_bin, _ = make_fake_launchctl(tmp_path)

    result = run_install(
        "--repo-url", str(origin), "--dir", str(target),
        "--launch-agents-dir", str(launch_agents_dir),
        env=env_with_fake_launchctl(fake_bin, {"FAKE_SETUP_NGINX_EXIT_CODE": "1"}),
    )

    assert result.returncode == 0
    assert "Warning" in result.stdout + result.stderr
    # nginx failing shouldn't stop the dashboard-daemon step from running
    assert (launch_agents_dir / "com.hermes.loop-engineering-dashboard.plist").exists()


def test_install_dashboard_daemon_load_failure_warns_and_continues(tmp_path):
    origin = make_origin(tmp_path, with_launchd_fixtures=True)
    target = tmp_path / "clone"
    launch_agents_dir = tmp_path / "LaunchAgents"
    fake_bin, _ = make_fake_launchctl(tmp_path, exit_code=1)

    result = run_install(
        "--repo-url", str(origin), "--dir", str(target),
        "--launch-agents-dir", str(launch_agents_dir),
        env=env_with_fake_launchctl(fake_bin),
    )

    assert result.returncode == 0
    assert "Warning" in result.stdout + result.stderr
    # a failed load shouldn't leave a plist behind for launchd to pick up later
    assert not (launch_agents_dir / "com.hermes.loop-engineering-dashboard.plist").exists()


def test_install_refreshes_already_loaded_non_dashboard_daemons(tmp_path):
    """The GitLab loop (or topic monitor) only gets touched by install.sh
    if the user already enabled it from the Daemons page - simulated here
    via already_loaded=True, which makes the fake launchctl's `list`
    succeed for every label, not just the dashboard's."""
    origin = make_origin(tmp_path, with_launchd_fixtures=True)
    target = tmp_path / "clone"
    launch_agents_dir = tmp_path / "LaunchAgents"
    fake_bin, launchctl_log = make_fake_launchctl(tmp_path, already_loaded=True)

    run_install(
        "--repo-url", str(origin), "--dir", str(target),
        "--launch-agents-dir", str(launch_agents_dir),
        "--skip-nginx",
        env=env_with_fake_launchctl(fake_bin),
    )

    assert (launch_agents_dir / "com.hermes.loop-engineering.plist").exists()
    calls = [line.split() for line in launchctl_log.read_text().splitlines()]
    assert ["unload", str(launch_agents_dir / "com.hermes.loop-engineering.plist")] in calls
    assert ["load", "-w", str(launch_agents_dir / "com.hermes.loop-engineering.plist")] in calls
    # refreshed via unload + load -w, never kickstarted - kickstart would
    # trigger a real, out-of-schedule GitLab-loop run. (The dashboard, a
    # different label, is legitimately kickstarted - only the loop's own
    # label must never appear after "kickstart -k".)
    kickstart_targets = [call[2].rsplit("/", 1)[-1] for call in calls if call[:2] == ["kickstart", "-k"]]
    assert "com.hermes.loop-engineering" not in kickstart_targets


def test_install_leaves_not_yet_loaded_non_dashboard_daemons_alone(tmp_path):
    origin = make_origin(tmp_path, with_launchd_fixtures=True)
    target = tmp_path / "clone"
    launch_agents_dir = tmp_path / "LaunchAgents"
    fake_bin, _ = make_fake_launchctl(tmp_path)

    run_install(
        "--repo-url", str(origin), "--dir", str(target),
        "--launch-agents-dir", str(launch_agents_dir),
        "--skip-nginx",
        env=env_with_fake_launchctl(fake_bin),
    )

    # install.sh never auto-starts the GitLab loop/topic monitor - only the
    # dashboard gets a first-time `load -w`.
    assert not (launch_agents_dir / "com.hermes.loop-engineering.plist").exists()
