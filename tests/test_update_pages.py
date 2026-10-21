import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin" / "web"))
import update_pages  # noqa: E402

CSRF = "<input type='hidden' name='csrf_token' value=\"T\">"
STEPS = ["fetch", "pull", "build", "install", "restart_dashboard", "relaunch"]
BEHIND = {"behind": 3, "ahead": 0, "branch": "main", "current": "v0.3.0-22-gabc", "latest_tag": "v0.4.0",
          "new_release": True, "clean": True, "can_fast_forward": True, "error": None,
          "commits": [{"sha": "abc1234", "subject": "Fix <b>thing</b>"}]}


def test_chip_only_when_behind():
    assert update_pages.render_chip({"behind": 0}) == ""
    assert "href='/update'" in update_pages.render_chip(BEHIND)


def test_banner_shows_release_commits_escaped_and_one_upgrade_form():
    html = update_pages.render_banner(BEHIND, {}, CSRF)
    assert "v0.4.0" in html and "Fix &lt;b&gt;thing&lt;/b&gt;" in html and "<b>thing</b>" not in html
    assert html.count("action='/update/start'") == 1 and "name='csrf_token'" in html


def test_banner_blocked_when_dirty_or_diverged_explains_why():
    html = update_pages.render_banner({**BEHIND, "clean": False}, {}, CSRF)
    assert "local changes" in html and "action='/update/start'" not in html
    html = update_pages.render_banner({**BEHIND, "can_fast_forward": False}, {}, CSRF)
    assert "diverged" in html and "action='/update/start'" not in html


def test_banner_empty_when_nothing_new_and_no_run():
    assert update_pages.render_banner({"behind": 0}, {}, CSRF) == ""


def test_body_lists_every_step_and_polls_progress_and_survives_fetch_errors():
    progress = {"state": "running", "steps": [{"name": n, "status": "pending", "log": ""} for n in STEPS]}
    html = update_pages.render_update_body(BEHIND, progress, CSRF)
    for label in ("Fetch changes", "Pull latest code", "Build app and DMG", "Install app",
                  "Restart dashboard", "Relaunch Loop X"):
        assert label in html
    assert "/update/progress" in html and ".catch" in html and "setTimeout" in html


def test_body_failed_state_shows_error_and_retry():
    progress = {"state": "failed", "error": "build: exited with status 1", "steps": []}
    html = update_pages.render_update_body(BEHIND, progress, CSRF)
    assert "build: exited with status 1" in html and "action='/update/start'" in html and "Retry" in html


def test_body_escapes_error_and_log():
    progress = {"state": "failed", "error": "<img src=x>", "steps": [
        {"name": "build", "status": "failed", "log": "<script>alert(1)</script>"}]}
    html = update_pages.render_update_body(BEHIND, progress, CSRF)
    assert "<img src=x>" not in html and "<script>alert(1)</script>" not in html


def test_body_done_state_says_updated():
    progress = {"state": "done", "to": "v0.4.0", "steps": []}
    assert "Updated to v0.4.0" in update_pages.render_update_body({"behind": 0}, progress, CSRF)


def test_body_idle_and_up_to_date():
    assert "up to date" in update_pages.render_update_body({"behind": 0}, {}, CSRF).lower()


def test_done_run_does_not_hide_a_newer_update():
    progress = {"state": "done", "to": "v0.3.0", "steps": []}
    html = update_pages.render_update_body(BEHIND, progress, CSRF)
    assert "Updated to" not in html and "action='/update/start'" in html
