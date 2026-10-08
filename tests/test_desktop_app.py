import http.server
import plistlib
import socket
import subprocess
import threading
from pathlib import Path

import pytest

import desktop_app

REPO = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = REPO / "bin" / "scripts" / "build_macos_app.sh"


@pytest.fixture
def live_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_find_free_port_is_bindable():
    port = desktop_app.find_free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def test_is_dashboard_up(live_server):
    assert desktop_app.is_dashboard_up(live_server) is True
    assert desktop_app.is_dashboard_up(free_port()) is False


def test_installed_dashboard_port_reads_plist(tmp_path):
    plist = tmp_path / "launchd" / "com.hermes.loop-engineering-dashboard.plist"
    plist.parent.mkdir()
    plist.write_bytes(plistlib.dumps({"ProgramArguments": ["python3", "x/dashboard_server.py", "48500"]}))
    assert desktop_app.installed_dashboard_port(tmp_path) == 48500


def test_installed_dashboard_port_missing_or_malformed(tmp_path):
    assert desktop_app.installed_dashboard_port(tmp_path) is None
    plist = tmp_path / "launchd" / "com.hermes.loop-engineering-dashboard.plist"
    plist.parent.mkdir()
    plist.write_bytes(plistlib.dumps({"ProgramArguments": ["python3", "x/dashboard_server.py"]}))
    assert desktop_app.installed_dashboard_port(tmp_path) is None


def test_resolve_attaches_to_running_dashboard(live_server, tmp_path):
    started = []
    url = desktop_app.resolve_dashboard_url(
        candidate_ports=[free_port(), live_server], start_embedded=lambda: started.append(1) or 1
    )
    assert url == f"http://127.0.0.1:{live_server}/"
    assert started == []


def test_resolve_starts_embedded_when_nothing_running():
    calls = []

    def start():
        calls.append(1)
        return 4321

    url = desktop_app.resolve_dashboard_url(candidate_ports=[free_port()], start_embedded=start)
    assert url == "http://127.0.0.1:4321/"
    assert calls == [1]


def test_wait_for_dashboard(live_server):
    assert desktop_app.wait_for_dashboard(live_server, timeout=2) is True
    assert desktop_app.wait_for_dashboard(free_port(), timeout=0.3) is False


def test_build_script_dry_run_makes_bundle(tmp_path):
    out = tmp_path / "out"
    result = subprocess.run(
        ["bash", str(BUILD_SCRIPT), "--output-dir", str(out), "--skip-venv", "--skip-icon"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    app = out / "Loop X.app"
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleName"] == "Loop X"
    assert info["CFBundleExecutable"] == "loop-x"
    launcher = app / "Contents" / "MacOS" / "loop-x"
    assert launcher.stat().st_mode & 0o111
    text = launcher.read_text()
    assert str(REPO) in text
    assert "desktop_app.py" in text
    assert "com.hermes" not in info["CFBundleIdentifier"]


def test_build_script_creates_desktop_shortcut(tmp_path):
    out, desktop = tmp_path / "out", tmp_path / "Desktop"
    result = subprocess.run(
        ["bash", str(BUILD_SCRIPT), "--output-dir", str(out), "--skip-venv", "--skip-icon", "--desktop-dir", str(desktop)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    link = desktop / "Loop X"
    assert link.is_symlink()
    assert link.resolve() == (out / "Loop X.app").resolve()
