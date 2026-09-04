import os
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "scripts" / "uninstall.sh"


def copy_script_to(dest_dir):
    """Copies uninstall.sh to <dest_dir>/bin/scripts/uninstall.sh so that,
    when run from there, its own BASH_SOURCE-derived LOOP_DIR resolves to
    dest_dir - simulating a real local clone (a dev checkout, or a genuine
    end-user install) rather than running the repo's own copy in place."""
    target = Path(dest_dir) / "bin" / "scripts" / "uninstall.sh"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, target)
    target.chmod(0o755)
    return target


def run_uninstall(*args, check=True):
    return subprocess.run(
        ["bash", str(SCRIPT), "--skip-hosts", "--skip-service", *args],
        check=check, capture_output=True, text=True,
    )


def run_uninstall_raw(*args, env=None, check=True):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        check=check, capture_output=True, text=True, env=env,
    )


def make_fake_sudo(bin_dir, log_path):
    """See tests/test_setup_nginx.py::make_fake_sudo - same stand-in, used
    here to verify uninstall.sh's nginx cleanup also primes sudo once."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake_sudo = bin_dir / "sudo"
    fake_sudo.write_text(f"""#!/usr/bin/env bash
echo "$*" >> "{log_path}"
if [ "$1" = "-v" ]; then
  exit 0
fi
exec "$@"
""")
    fake_sudo.chmod(0o755)


def fake_sudo_env(bin_dir):
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return env


def run_uninstall_via_stdin(*args, check=True):
    """Runs uninstall.sh the way `curl -fsSL .../uninstall.sh | bash` does:
    piped into bash's stdin rather than passed as a script-file argument.
    BASH_SOURCE[0] is unset in this mode on this machine's bash, which is
    exactly the case the real online uninstaller hits."""
    return subprocess.run(
        ["bash", "-s", "--", "--skip-hosts", "--skip-service", *args],
        input=SCRIPT.read_text(),
        check=check, capture_output=True, text=True,
    )


def make_plist(launchd_dir, name="com.hermes.test.plist"):
    launchd_dir.mkdir(parents=True, exist_ok=True)
    (launchd_dir / name).write_text("<plist/>\n")
    return name


def test_uninstall_unloads_and_removes_installed_plist(tmp_path):
    launchd_dir = tmp_path / "launchd"
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()
    name = make_plist(launchd_dir)
    (launch_agents_dir / name).write_text("<plist/>\n")

    result = run_uninstall(
        "--launchd-dir", str(launchd_dir),
        "--launch-agents-dir", str(launch_agents_dir),
        "--nginx-servers-dir", str(tmp_path / "nginx-servers"),
        "--project-dir", str(tmp_path / "no-such-project-dir"),
    )

    assert not (launch_agents_dir / name).exists()
    assert f"Removed {name}" in result.stdout


def test_uninstall_reports_when_daemon_was_not_installed(tmp_path):
    launchd_dir = tmp_path / "launchd"
    name = make_plist(launchd_dir)

    result = run_uninstall(
        "--launchd-dir", str(launchd_dir),
        "--launch-agents-dir", str(tmp_path / "LaunchAgents"),
        "--nginx-servers-dir", str(tmp_path / "nginx-servers"),
        "--project-dir", str(tmp_path / "no-such-project-dir"),
    )

    assert f"{name} was not installed" in result.stdout


def test_uninstall_removes_nginx_conf(tmp_path):
    servers_dir = tmp_path / "servers"
    servers_dir.mkdir()
    (servers_dir / "loop.local.conf").write_text("server {}\n")
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()

    run_uninstall(
        "--launchd-dir", str(launchd_dir),
        "--launch-agents-dir", str(tmp_path / "LaunchAgents"),
        "--nginx-servers-dir", str(servers_dir),
        "--project-dir", str(tmp_path / "no-such-project-dir"),
    )

    assert not (servers_dir / "loop.local.conf").exists()


def test_uninstall_skip_nginx_leaves_conf_in_place(tmp_path):
    servers_dir = tmp_path / "servers"
    servers_dir.mkdir()
    (servers_dir / "loop.local.conf").write_text("server {}\n")
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()

    run_uninstall(
        "--launchd-dir", str(launchd_dir),
        "--launch-agents-dir", str(tmp_path / "LaunchAgents"),
        "--nginx-servers-dir", str(servers_dir),
        "--project-dir", str(tmp_path / "no-such-project-dir"),
        "--skip-nginx",
    )

    assert (servers_dir / "loop.local.conf").exists()


def test_uninstall_uses_custom_domain_for_nginx_conf(tmp_path):
    servers_dir = tmp_path / "servers"
    servers_dir.mkdir()
    (servers_dir / "myloop.test.conf").write_text("server {}\n")
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()

    run_uninstall(
        "--launchd-dir", str(launchd_dir),
        "--launch-agents-dir", str(tmp_path / "LaunchAgents"),
        "--nginx-servers-dir", str(servers_dir),
        "--nginx-domain", "myloop.test",
        "--project-dir", str(tmp_path / "no-such-project-dir"),
    )

    assert not (servers_dir / "myloop.test.conf").exists()


def test_uninstall_removes_whole_project_dir_by_default(tmp_path):
    # ~/.loop-engineering is also install.sh's default clone target, so a
    # full uninstall removes code, config, and outputs together - that's
    # the whole point of "uninstall".
    project_dir = tmp_path / ".loop-engineering"
    project_dir.mkdir()
    (project_dir / "projects.json").write_text("{}")
    (project_dir / "bin").mkdir()
    (project_dir / "bin" / "loop_config.py").write_text("# code")
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()

    run_uninstall(
        "--launchd-dir", str(launchd_dir),
        "--launch-agents-dir", str(tmp_path / "LaunchAgents"),
        "--nginx-servers-dir", str(tmp_path / "nginx-servers"),
        "--project-dir", str(project_dir),
    )

    assert not project_dir.exists()


def test_uninstall_keep_config_leaves_project_dir_in_place(tmp_path):
    project_dir = tmp_path / ".loop-engineering"
    project_dir.mkdir()
    (project_dir / "projects.json").write_text("{}")
    (project_dir / "bin").mkdir()
    (project_dir / "bin" / "loop_config.py").write_text("# code")
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()

    run_uninstall(
        "--launchd-dir", str(launchd_dir),
        "--launch-agents-dir", str(tmp_path / "LaunchAgents"),
        "--nginx-servers-dir", str(tmp_path / "nginx-servers"),
        "--project-dir", str(project_dir),
        "--keep-config",
    )

    assert project_dir.exists()
    assert (project_dir / "projects.json").exists()
    assert (project_dir / "bin" / "loop_config.py").exists()


def test_uninstall_rejects_unknown_flag():
    result = run_uninstall("--not-a-real-flag", check=False)

    assert result.returncode != 0


def test_uninstall_falls_back_to_known_agent_names_without_local_launchd_dir(tmp_path):
    # Simulates running uninstall.sh via `curl | bash` with no local repo
    # clone: there's no launchd/*.plist to glob, so it must fall back to
    # the known, fixed set of agent names this repo installs.
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()
    known_names = [
        "com.hermes.loop-engineering.plist",
        "com.hermes.loop-engineering-dashboard.plist",
        "com.hermes.loop-engineering-topic-monitor.plist",
    ]
    for name in known_names:
        (launch_agents_dir / name).write_text("<plist/>\n")

    result = run_uninstall(
        "--launchd-dir", str(tmp_path / "no-such-launchd-dir"),
        "--launch-agents-dir", str(launch_agents_dir),
        "--nginx-servers-dir", str(tmp_path / "nginx-servers"),
        "--project-dir", str(tmp_path / "no-such-project-dir"),
    )

    for name in known_names:
        assert not (launch_agents_dir / name).exists()
        assert f"Removed {name}" in result.stdout


def test_uninstall_via_stdin_does_not_hit_unbound_bash_source(tmp_path):
    # Reproduces the real online-uninstall invocation
    # (curl -fsSL .../uninstall.sh | bash): the script has no on-disk path
    # of its own, so BASH_SOURCE[0] is unset under `set -u` and must not
    # blow up computing LOOP_DIR/LAUNCHD_DIR from it.
    project_dir = tmp_path / ".loop-engineering"
    project_dir.mkdir()

    result = run_uninstall_via_stdin(
        "--launch-agents-dir", str(tmp_path / "LaunchAgents"),
        "--nginx-servers-dir", str(tmp_path / "nginx-servers"),
        "--project-dir", str(project_dir),
    )

    assert "unbound variable" not in result.stderr
    assert not project_dir.exists()


def test_uninstall_primes_sudo_once_before_hosts_cleanup(tmp_path):
    hosts_file = tmp_path / "hosts"
    hosts_file.write_text("127.0.0.1\tloop.local\n127.0.0.1\tlocalhost\n")
    bin_dir = tmp_path / "fakebin"
    log_path = tmp_path / "sudo.log"
    make_fake_sudo(bin_dir, log_path)
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()

    result = run_uninstall_raw(
        "--launchd-dir", str(launchd_dir),
        "--launch-agents-dir", str(tmp_path / "LaunchAgents"),
        "--nginx-servers-dir", str(tmp_path / "nginx-servers"),
        "--hosts-file", str(hosts_file),
        "--project-dir", str(tmp_path / "no-such-project-dir"),
        "--skip-service",
        env=fake_sudo_env(bin_dir),
    )

    calls = log_path.read_text().strip().splitlines()
    assert calls[0] == "-v"
    assert "loop.local" not in hosts_file.read_text()
    assert "localhost" in hosts_file.read_text()
    assert result.returncode == 0


def test_uninstall_refuses_default_project_dir_when_run_from_different_clone(tmp_path):
    # The actual incident this guards against: a developer runs their own
    # dev clone's bin/scripts/uninstall.sh bare (no --project-dir), while a
    # separate, real install sits at the default ~/.loop-engineering. Since
    # the script's own location (LOOP_DIR) doesn't match the directory it's
    # about to rm -rf, it must refuse rather than silently deleting the
    # real install.
    home = tmp_path / "home"
    home.mkdir()
    real_install = home / ".loop-engineering"
    real_install.mkdir()
    (real_install / "projects.json").write_text("{}")
    dev_clone = tmp_path / "dev-clone"
    script_path = copy_script_to(dev_clone)
    env = dict(os.environ)
    env["HOME"] = str(home)

    result = subprocess.run(
        ["bash", str(script_path), "--skip-hosts", "--skip-service", "--skip-nginx"],
        env=env, capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert real_install.exists()
    assert (real_install / "projects.json").exists()


def test_uninstall_proceeds_when_run_from_inside_the_real_project_dir(tmp_path):
    # The legitimate documented flow (README: "bin/scripts/uninstall.sh"
    # run bare) must keep working: a real end user runs their install's own
    # copy of the script, from inside the very directory it's uninstalling.
    home = tmp_path / "home"
    home.mkdir()
    real_install = home / ".loop-engineering"
    script_path = copy_script_to(real_install)
    (real_install / "projects.json").write_text("{}")
    env = dict(os.environ)
    env["HOME"] = str(home)

    result = subprocess.run(
        ["bash", str(script_path), "--skip-hosts", "--skip-service", "--skip-nginx"],
        env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert not real_install.exists()


def test_uninstall_allows_default_project_dir_via_curl_style_stdin(tmp_path):
    # The other legitimate documented flow: `curl -fsSL .../uninstall.sh |
    # bash` with no local clone at all (LOOP_DIR empty, BASH_SOURCE unset)
    # must still remove the real default ~/.loop-engineering.
    home = tmp_path / "home"
    home.mkdir()
    real_install = home / ".loop-engineering"
    real_install.mkdir()
    (real_install / "projects.json").write_text("{}")
    env = dict(os.environ)
    env["HOME"] = str(home)

    result = subprocess.run(
        ["bash", "-s", "--", "--skip-hosts", "--skip-service", "--skip-nginx"],
        input=SCRIPT.read_text(), env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert not real_install.exists()


def test_uninstall_does_not_invoke_sudo_when_nothing_needs_it(tmp_path):
    hosts_file = tmp_path / "hosts"
    hosts_file.write_text("127.0.0.1\tlocalhost\n")
    bin_dir = tmp_path / "fakebin"
    log_path = tmp_path / "sudo.log"
    make_fake_sudo(bin_dir, log_path)
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()

    run_uninstall_raw(
        "--launchd-dir", str(launchd_dir),
        "--launch-agents-dir", str(tmp_path / "LaunchAgents"),
        "--nginx-servers-dir", str(tmp_path / "nginx-servers"),
        "--hosts-file", str(hosts_file),
        "--project-dir", str(tmp_path / "no-such-project-dir"),
        "--skip-service",
        env=fake_sudo_env(bin_dir),
    )

    assert not log_path.exists()
