import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "scripts" / "setup-nginx.sh"


def run_setup_nginx(*args, check=True):
    return subprocess.run(
        ["bash", str(SCRIPT), "--skip-brew-install", "--skip-hosts", "--skip-service", *args],
        check=check, capture_output=True, text=True,
    )


def run_setup_nginx_raw(*args, env=None, check=True):
    return subprocess.run(
        ["bash", str(SCRIPT), "--skip-brew-install", *args],
        check=check, capture_output=True, text=True, env=env,
    )


def make_fake_sudo(bin_dir, log_path):
    """A stand-in for real sudo that just runs the command it's given and
    logs its argv, so tests can assert priming (`sudo -v`) happens exactly
    once and before any command that actually needs the elevated ticket -
    without ever prompting for or requiring a real password."""
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


def test_setup_nginx_writes_server_conf_with_domain_and_port(tmp_path):
    servers_dir = tmp_path / "servers"

    run_setup_nginx("--servers-dir", str(servers_dir), "--domain", "loop.local", "--port", "8420")

    conf = (servers_dir / "loop.local.conf").read_text()
    assert "server_name  loop.local;" in conf
    assert "proxy_pass         http://127.0.0.1:8420;" in conf
    assert "listen       80;" in conf


def test_setup_nginx_uses_custom_domain_and_port(tmp_path):
    servers_dir = tmp_path / "servers"

    run_setup_nginx("--servers-dir", str(servers_dir), "--domain", "myloop.test", "--port", "9001")

    conf = (servers_dir / "myloop.test.conf").read_text()
    assert "server_name  myloop.test;" in conf
    assert "proxy_pass         http://127.0.0.1:9001;" in conf


def test_setup_nginx_is_idempotent(tmp_path):
    servers_dir = tmp_path / "servers"

    run_setup_nginx("--servers-dir", str(servers_dir))
    first = (servers_dir / "loop.local.conf").read_text()
    run_setup_nginx("--servers-dir", str(servers_dir))
    second = (servers_dir / "loop.local.conf").read_text()

    assert first == second
    assert len(list(servers_dir.iterdir())) == 1


def test_setup_nginx_rejects_unknown_flag():
    result = run_setup_nginx("--not-a-real-flag", check=False)

    assert result.returncode != 0


def test_setup_nginx_primes_sudo_once_before_hosts_edit(tmp_path):
    hosts_file = tmp_path / "hosts"
    hosts_file.write_text("127.0.0.1\tlocalhost\n")
    bin_dir = tmp_path / "fakebin"
    log_path = tmp_path / "sudo.log"
    make_fake_sudo(bin_dir, log_path)

    result = run_setup_nginx_raw(
        "--servers-dir", str(tmp_path / "servers"),
        "--hosts-file", str(hosts_file),
        "--skip-service",
        env=fake_sudo_env(bin_dir),
    )

    calls = log_path.read_text().strip().splitlines()
    assert calls[0] == "-v"
    assert "loop.local" in hosts_file.read_text()
    assert result.returncode == 0


def test_setup_nginx_does_not_invoke_sudo_when_nothing_needs_it(tmp_path):
    hosts_file = tmp_path / "hosts"
    hosts_file.write_text("127.0.0.1\tloop.local\n")
    bin_dir = tmp_path / "fakebin"
    log_path = tmp_path / "sudo.log"
    make_fake_sudo(bin_dir, log_path)

    run_setup_nginx_raw(
        "--servers-dir", str(tmp_path / "servers"),
        "--hosts-file", str(hosts_file),
        "--skip-service",
        env=fake_sudo_env(bin_dir),
    )

    assert not log_path.exists()
