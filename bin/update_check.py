#!/usr/bin/env python3
"""Is there something new on origin? `git fetch`, compare HEAD with
origin/<branch>, cache the answer in outputs/update/status.json for the
dashboard banner and the desktop app's native notification. Never raises:
a failed fetch (offline) records `error` and keeps the last known `behind`."""
import json
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
STATE_ENV = "LOOP_UPDATE_DIR"
CHECK_INTERVAL_SECONDS = 6 * 3600
# The loop rewrites PROGRESS.md on every run; it must not block an upgrade.
IGNORED_DIRTY_FILES = {"PROGRESS.md"}
_background = {"thread": None}


def default_state_dir():
    env = os.environ.get(STATE_ENV)
    return Path(env) if env else REPO_DIR / "outputs" / "update"


def _git(repo, *args, timeout=60, env=None):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout, env=env)


def _non_interactive_env():
    """A background fetch must fail fast, never wait on a credential prompt."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = f"{env.get('GIT_SSH_COMMAND', 'ssh')} -o BatchMode=yes"
    return env


def _out(repo, *args):
    result = _git(repo, *args)
    return result.stdout.strip() if result.returncode == 0 else ""


def read_status(state_dir=None):
    if state_dir is None:
        state_dir = default_state_dir()
    try:
        return json.loads((Path(state_dir) / "status.json").read_text())
    except (OSError, ValueError):
        return {}


def _write(state_dir, name, data):
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / f".{name}.tmp"
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(state_dir / name)


def _safe_write(state_dir, status):
    try:
        _write(state_dir, "status.json", status)
    except OSError as exc:
        status["error"] = status["error"] or str(exc)


def _is_clean(repo):
    # Raw stdout: _out() strips, which would eat the porcelain's leading status column.
    result = _git(repo, "status", "--porcelain", "--untracked-files=no")
    return all(line[3:].strip() in IGNORED_DIRTY_FILES for line in result.stdout.splitlines())


def check(repo=None, state_dir=None, fetch=True):
    if repo is None:
        repo = REPO_DIR
    if state_dir is None:
        state_dir = default_state_dir()
    previous = read_status(state_dir)
    status = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "branch": "", "behind": previous.get("behind", 0), "ahead": previous.get("ahead", 0),
        "commits": previous.get("commits", []), "current": "", "latest_tag": previous.get("latest_tag", ""),
        "new_release": previous.get("new_release", False), "origin_sha": previous.get("origin_sha", ""),
        "clean": True, "can_fast_forward": True, "error": None,
    }
    try:
        branch = _out(repo, "rev-parse", "--abbrev-ref", "HEAD")
        status["branch"] = branch
        status["current"] = _out(repo, "describe", "--tags", "--always")
        status["clean"] = _is_clean(repo)
        if fetch:
            fetched = _git(repo, "fetch", "--tags", "--quiet", "origin", env=_non_interactive_env())
            if fetched.returncode != 0:
                status["error"] = fetched.stderr.strip() or "git fetch failed"
                _safe_write(state_dir, status)
                return status
        remote = f"origin/{branch}"
        if not branch or branch == "HEAD" or _git(repo, "rev-parse", "--verify", "--quiet", remote).returncode != 0:
            status.update(behind=0, ahead=0, commits=[], error=f"no {remote} to compare against")
            _safe_write(state_dir, status)
            return status
        status["behind"] = int(_out(repo, "rev-list", "--count", f"HEAD..{remote}") or 0)
        status["ahead"] = int(_out(repo, "rev-list", "--count", f"{remote}..HEAD") or 0)
        status["origin_sha"] = _out(repo, "rev-parse", remote)
        status["commits"] = [
            {"sha": sha, "subject": subject}
            for sha, _, subject in (line.partition("\t") for line in
                                    _out(repo, "log", "--format=%h%x09%s", "-20", f"HEAD..{remote}").splitlines())
        ]
        status["can_fast_forward"] = _git(repo, "merge-base", "--is-ancestor", "HEAD", remote).returncode == 0
        latest = _out(repo, "describe", "--tags", "--abbrev=0", remote)
        here = _out(repo, "describe", "--tags", "--abbrev=0", "HEAD")
        status["latest_tag"] = latest
        status["new_release"] = bool(latest) and latest != here
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        status["error"] = str(exc)
    _safe_write(state_dir, status)
    return status


def take_notification(state_dir=None):
    """Text for a native notification the first time this origin sha is
    seen as new; None afterwards (and when nothing is new)."""
    if state_dir is None:
        state_dir = default_state_dir()
    status = read_status(state_dir)
    if not status.get("behind") or not status.get("origin_sha"):
        return None
    try:
        seen = json.loads((Path(state_dir) / "notified.json").read_text()).get("sha")
    except (OSError, ValueError):
        seen = None
    if seen == status["origin_sha"]:
        return None
    _write(state_dir, "notified.json", {"sha": status["origin_sha"]})
    what = status["latest_tag"] if status.get("new_release") else f"{status['behind']} new change(s)"
    return f"Loop X update available: {what}. Open Loop X and click Upgrade now."


def start_background(interval=CHECK_INTERVAL_SECONDS, repo=None, state_dir=None):
    """One daemon thread per process: check now, then every `interval`."""
    if _background["thread"] is not None:
        return None

    def loop():
        while True:
            try:
                check(repo=repo, state_dir=state_dir)
            except Exception:  # noqa: BLE001 - one bad check must not end the watcher for good
                pass
            threading.Event().wait(interval)

    thread = threading.Thread(target=loop, daemon=True, name="update-check")
    _background["thread"] = thread
    thread.start()
    return thread
