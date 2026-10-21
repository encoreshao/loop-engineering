#!/usr/bin/env python3
"""One-click upgrade of the macOS app: fetch, pull (ff-only), rebuild app +
DMG into a staging dir, swap the installed app, restart the dashboard
daemon (never the scheduler), relaunch. Progress goes to
<state>/progress.json after every change so the /update page can show it;
the run is detached (start_detached) so it outlives the app window.

    python3 bin/update_runner.py run [--repo DIR] [--state-dir DIR] [--install-target APP]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_check  # noqa: E402

REPO_DIR = Path(__file__).resolve().parent.parent
APP_NAME = "Loop X"
STEPS = ["fetch", "pull", "build", "install", "restart_dashboard", "relaunch"]
LOG_TAIL_LINES = 40


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _state(state_dir):
    return Path(state_dir) if state_dir is not None else update_check.default_state_dir()


def _write_progress(state_dir, progress):
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / ".progress.tmp"
    tmp.write_text(json.dumps(progress, indent=2))
    tmp.replace(state_dir / "progress.json")


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _fresh_progress(state="starting"):
    return {"state": state, "pid": os.getpid(), "started": _now(), "finished": None, "error": None,
            "from": "", "to": "",
            "steps": [{"name": n, "status": "pending", "started": None, "finished": None, "log": ""} for n in STEPS]}


def read_progress(state_dir=None):
    try:
        progress = json.loads((_state(state_dir) / "progress.json").read_text())
    except (OSError, ValueError):
        return {}
    if progress.get("state") in ("starting", "running") and not _pid_alive(progress.get("pid")):
        progress["state"] = "failed"
        progress["error"] = "Upgrade was interrupted"
    return progress


def record_app_path(path, state_dir=None):
    state = _state(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    (state / "app_path.txt").write_text(str(path))


def resolve_install_target(repo=None, state_dir=None):
    repo = Path(repo) if repo is not None else REPO_DIR
    try:
        recorded = Path((_state(state_dir) / "app_path.txt").read_text().strip())
        if recorded.suffix == ".app" and recorded.is_dir():
            return recorded
    except OSError:
        pass
    return repo / "dist" / f"{APP_NAME}.app"


def _lock_is_live(state):
    try:
        return _pid_alive((state / "upgrade.lock").read_text().strip())
    except OSError:
        return False


def _acquire_lock(state):
    state.mkdir(parents=True, exist_ok=True)
    lock = state / "upgrade.lock"
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            if _lock_is_live(state):
                return False
            lock.unlink(missing_ok=True)  # stale
    return False


def swap_app(new_app, target):
    """Replace `target` with `new_app`, crash-safely.

    The new bundle is first copied NEXT TO the target (same volume, so the
    final step is an atomic rename, never a cross-volume copy that a sleep or
    power loss can cut in half). The old app is renamed to `<name>.old` and
    kept: deleting the bundle of the app that is still running is what
    cleanup_old() does after the relaunch. A leftover `.old` with no target
    is the last good app of an interrupted earlier swap and is restored, not
    deleted."""
    new_app, target = Path(new_app), Path(target)
    old = target.with_name(target.name + ".old")
    incoming = target.with_name("." + target.name + ".new")
    target.parent.mkdir(parents=True, exist_ok=True)
    if old.exists():
        if target.exists():
            shutil.rmtree(old)
        else:
            os.rename(old, target)
    shutil.rmtree(incoming, ignore_errors=True)
    try:
        shutil.copytree(new_app, incoming, symlinks=True)
    except Exception:
        shutil.rmtree(incoming, ignore_errors=True)
        raise
    had_old = target.exists()
    if had_old:
        os.rename(target, old)
    try:
        os.rename(incoming, target)
    except Exception:
        if had_old:
            os.rename(old, target)
        shutil.rmtree(incoming, ignore_errors=True)
        raise


def cleanup_old(target):
    """Remove the previous app kept by swap_app - only while a current app exists."""
    target = Path(target)
    old = target.with_name(target.name + ".old")
    if old.exists() and target.exists():
        shutil.rmtree(old, ignore_errors=True)


# The app launcher's own PATH: a launchd-started dashboard has almost none, and
# the build then picks /usr/bin/python3 (3.9), which cannot run this repo.
_COMMAND_PATH = "$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _command_env():
    env = update_check._non_interactive_env()
    env["PATH"] = os.path.expandvars(_COMMAND_PATH.replace("$HOME", str(Path.home())))
    return env


def build_python():
    """The interpreter the dashboard itself runs on (its base interpreter when
    inside a venv - the app's own venv is about to be replaced)."""
    return getattr(sys, "_base_executable", None) or sys.executable


def _real_run_cmd(argv, cwd, on_line):
    proc = subprocess.Popen([str(a) for a in argv], cwd=str(cwd), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, env=_command_env())
    for line in proc.stdout:
        on_line(line.rstrip("\n"))
    return proc.wait()


def run(repo=None, state_dir=None, install_target=None, run_cmd=None):
    repo = Path(repo) if repo is not None else REPO_DIR
    state = _state(state_dir)
    run_cmd = run_cmd or _real_run_cmd
    progress = _fresh_progress("running")

    def save():
        _write_progress(state, progress)

    def refuse(message):
        progress.update(state="refused", error=message, finished=_now())
        save()
        return progress

    if not _acquire_lock(state):
        # Do not write progress.json: it belongs to the run that holds the lock.
        return {**progress, "state": "refused", "error": "An upgrade is already running"}
    try:
        status = update_check.check(repo=repo, state_dir=state, fetch=True)
        progress["from"] = status.get("current", "")
        if not status.get("clean", True):
            return refuse("You have local changes to tracked files; commit or stash them, then retry")
        if not status.get("can_fast_forward", True):
            return refuse("This checkout has diverged from origin and cannot fast-forward; "
                          "resolve it with git, then retry")
        target = Path(install_target) if install_target else resolve_install_target(repo, state)
        staging = state / "staging"
        steps = {s["name"]: s for s in progress["steps"]}
        save()

        def run_step(name, fn):
            step = steps[name]
            step.update(status="running", started=_now())
            lines = []

            def on_line(line):
                lines.append(line)
                step["log"] = "\n".join(lines[-LOG_TAIL_LINES:])
                save()

            save()
            try:
                fn(on_line)
            except Exception as exc:  # noqa: BLE001 - reported to the user, run stops
                step.update(status="failed", finished=_now())
                progress.update(state="failed", error=f"{name}: {exc}", finished=_now())
                save()
                return False
            step.update(status="done", finished=_now())
            save()
            return True

        def cmd(argv, on_line):
            rc = run_cmd(argv, repo, on_line)
            if rc != 0:
                raise RuntimeError(f"exited with status {rc}")

        def do_build(on_line):
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            cmd(["bash", repo / "bin" / "scripts" / "build_macos_app.sh", "--dmg", "--python", build_python(),
                 "--output-dir", staging], on_line)

        def do_install(on_line):
            on_line(f"Installing to {target}")
            swap_app(staging / f"{APP_NAME}.app", target)
            dmg = staging / f"{APP_NAME}.dmg"
            if dmg.exists():
                (repo / "dist").mkdir(exist_ok=True)
                shutil.copy2(dmg, repo / "dist" / dmg.name)
            shutil.rmtree(staging, ignore_errors=True)

        plan = [
            ("fetch", lambda ol: cmd(["git", "-C", repo, "fetch", "--tags", "origin"], ol)),
            ("pull", lambda ol: cmd(["git", "-C", repo, "merge", "--ff-only", "--autostash",
                                      f"origin/{status['branch']}"], ol)),
            ("build", do_build),
            ("install", do_install),
            ("restart_dashboard", lambda ol: cmd(["bash", repo / "bin" / "scripts" / "restart-daemons.sh"], ol)),
        ]
        for name, fn in plan:
            if not run_step(name, fn):
                return progress
        after = update_check.check(repo=repo, state_dir=state, fetch=False)
        progress["to"] = after.get("current", "")
        steps["relaunch"].update(status="done", started=_now(), finished=_now(), log="Relaunching Loop X")
        progress.update(state="done", finished=_now())
        save()
    finally:
        (state / "upgrade.lock").unlink(missing_ok=True)

    # Last action: the old window goes away, the new app opens on /update.
    run_cmd(["osascript", "-e", f'tell application "{APP_NAME}" to quit'], repo, lambda _l: None)
    _wait_until_quit(run_cmd, repo)
    run_cmd(["open", target], repo, lambda _l: None)
    cleanup_old(target)
    return progress


def _wait_until_quit(run_cmd, repo, attempts=30):
    """Poll until the old app has really exited (an `open` while it is still
    quitting just re-activates it, which then exits and leaves no app running)."""
    for _ in range(attempts):
        lines = []
        run_cmd(["osascript", "-e", f'application "{APP_NAME}" is running'], repo, lines.append)
        if any(line.strip() == "false" for line in lines):
            return
        time.sleep(0.5)


def start_detached(repo=None, state_dir=None, popen=None):
    repo = Path(repo) if repo is not None else REPO_DIR
    state = _state(state_dir)
    popen = popen or subprocess.Popen
    if _lock_is_live(state):
        progress = read_progress(state) or _fresh_progress()
        progress.update(state="refused", error="An upgrade is already running")
        return progress
    progress = _fresh_progress("starting")
    _write_progress(state, progress)
    proc = popen([sys.executable, str(Path(__file__).resolve()), "run", "--repo", str(repo),
                  "--state-dir", str(state)],
                 cwd=str(repo), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                 start_new_session=True)
    child_pid = getattr(proc, "pid", None)
    if child_pid:
        # So a runner that dies before its first save reads as failed, not "running" forever.
        progress["pid"] = child_pid
        _write_progress(state, progress)
    return progress


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--repo")
    parser.add_argument("--state-dir")
    parser.add_argument("--install-target")
    args = parser.parse_args(argv)
    progress = run(repo=args.repo, state_dir=args.state_dir, install_target=args.install_target)
    return 0 if progress["state"] == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
