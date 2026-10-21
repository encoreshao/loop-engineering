#!/usr/bin/env python3
"""Native macOS window around the dashboard.

Attaches to an already-running dashboard (the launchd daemon, or a dev
server) when one answers; otherwise serves the dashboard in-process on a
free loopback port. The window is a WKWebView via pywebview, which is not
part of this repo's stdlib-only runtime: `bin/scripts/build_macos_app.sh`
installs it into the bundle's own venv.

    python3 bin/desktop_app.py [--port N] [--title TEXT]
"""
import argparse
import json
import plistlib
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

LOOP_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_PLIST_NAME = "com.hermes.loop-engineering-dashboard.plist"
DEFAULT_DASHBOARD_PORT = 8420
APP_TITLE = "Loop X Engineering"
ICON_PATH = LOOP_DIR / "assets" / "app-icon.png"


def find_free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def is_dashboard_up(port, timeout=0.5):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True  # it answered, just not 2xx
    except (urllib.error.URLError, OSError):
        return False


def wait_for_dashboard(port, timeout=10.0):
    deadline = time.monotonic() + timeout
    while True:
        if is_dashboard_up(port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def installed_dashboard_port(loop_dir=None):
    """Port in this checkout's rendered launchd plist, or None."""
    if loop_dir is None:
        loop_dir = LOOP_DIR
    plist = Path(loop_dir) / "launchd" / DASHBOARD_PLIST_NAME
    try:
        args = plistlib.loads(plist.read_bytes())["ProgramArguments"]
        return int(args[-1])
    except (OSError, KeyError, ValueError, TypeError, IndexError, plistlib.InvalidFileException):
        return None


def resolve_dashboard_url(candidate_ports, start_embedded):
    """URL of the first candidate port that answers, else of a freshly
    started embedded server (start_embedded() returns its port)."""
    for port in candidate_ports:
        if is_dashboard_up(port):
            return f"http://127.0.0.1:{port}/"
    return f"http://127.0.0.1:{start_embedded()}/"


def start_embedded_server():
    sys.path.insert(0, str(LOOP_DIR / "bin" / "web"))
    sys.path.insert(0, str(LOOP_DIR / "bin"))
    import dashboard_server as ds

    ds.ledger.run_startup_backfill()
    port = find_free_port()
    server = ds.ThreadingHTTPServer(("127.0.0.1", port), ds.DashboardHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    wait_for_dashboard(port)
    return port


def running_app_path(prefix=None):
    """`<X>.app` when this process runs from the bundle's own venv
    (<X>.app/Contents/Resources/venv), else None (a dev run from the checkout)."""
    prefix = Path(prefix if prefix is not None else sys.prefix)
    app = prefix.parent.parent.parent
    if prefix.name == "venv" and prefix.parent.name == "Resources" and app.suffix == ".app":
        return app
    return None


def _post_notification(text):
    script = f"display notification {json.dumps(text)} with title \"Loop X\""
    try:
        subprocess.run(["osascript", "-e", script], timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def notify_if_update(notify=None, state_dir=None):
    """Post a native notification the first time a new update is seen."""
    sys.path.insert(0, str(LOOP_DIR / "bin"))
    import update_check

    text = update_check.take_notification(state_dir=state_dir)
    if not text:
        return False
    (notify or _post_notification)(text)
    return True


def start_update_watch(interval=60, state_dir=None, notify=None):
    def loop():
        while True:
            notify_if_update(notify=notify, state_dir=state_dir)
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True, name="update-notify").start()


def post_upgrade_url(url, progress, now=None):
    """Land on /update right after an upgrade relaunched the app, so the user
    sees "Updated to <version>"; any other launch opens the dashboard as usual."""
    from datetime import datetime, timezone

    try:
        finished = datetime.fromisoformat(progress.get("finished") or "")
    except (TypeError, ValueError):
        return url
    now = now or datetime.now(timezone.utc)
    if progress.get("state") == "done" and 0 <= (now - finished).total_seconds() < 600:
        return url.rstrip("/") + "/update"
    return url


def _brand_application(title):
    """Best effort: Dock icon and menu-bar name instead of Python's."""
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle

        info = NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = title
        image = NSImage.alloc().initWithContentsOfFile_(str(ICON_PATH))
        if image is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Loop X Engineering desktop window")
    parser.add_argument("--port", type=int, help="attach to the dashboard on this port (never start one)")
    parser.add_argument("--title", default=APP_TITLE)
    args = parser.parse_args(argv)

    try:
        import webview
    except ImportError:
        print(
            "pywebview is not installed. Run bin/scripts/build_macos_app.sh, or: pip install pywebview",
            file=sys.stderr,
        )
        return 1

    if args.port:
        if not wait_for_dashboard(args.port, timeout=5):
            print(f"No dashboard answering on port {args.port}.", file=sys.stderr)
            return 1
        url = f"http://127.0.0.1:{args.port}/"
    else:
        ports = [p for p in (installed_dashboard_port(), DEFAULT_DASHBOARD_PORT) if p]
        url = resolve_dashboard_url(ports, start_embedded_server)

    _brand_application(args.title)
    sys.path.insert(0, str(LOOP_DIR / "bin"))
    import update_check
    import update_runner

    update_check.start_background()
    url = post_upgrade_url(url, update_runner.read_progress())
    app = running_app_path()
    if app:
        update_runner.record_app_path(app)  # so an upgrade reinstalls over this very app
    start_update_watch()
    webview.create_window(args.title, url, width=1360, height=900, min_size=(900, 600))
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
