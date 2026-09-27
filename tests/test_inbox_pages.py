import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin" / "web"))
import inbox_config  # noqa: E402
import inbox_pages  # noqa: E402

INBOX = {"name": "w", "label": "Work <Gmail>", "provider": "gmail", "account": "me@example.com", "enabled": True,
         "urgent_brief": "", "vip_senders": [], "exclude_senders": [], "categories": None, "slack_bundle": None}
CONFIG = {"default_categories": inbox_config.DEFAULT_CATEGORIES, "inboxes": [INBOX]}
CSRF = "<input type='hidden' name='csrf_token' value=\"T\">"


def test_inbox_body_empty_state_links_to_setup():
    body = inbox_pages.render_inbox_body({"default_categories": [], "inboxes": []}, {"inboxes": {}}, CSRF)
    assert "/inbox/setup" in body and "No inboxes yet" in body


def test_inbox_body_escapes_and_shows_status_and_urgent():
    status = {"inboxes": {"w": {"state": "needs_reauth", "last_run_at": "2026-09-27T09:00:00+00:00",
                                "counts": {"urgent": 1}, "error": "expired",
                                "urgent": [{"from": "<script>x</script>", "subject": "Down", "draft_link": "https://mail/1",
                                            "needs_manual_reply": False, "draft_failed": False}]}}}
    body = inbox_pages.render_inbox_body(CONFIG, status, CSRF)
    assert "Work &lt;Gmail&gt;" in body and "<script>x</script>" not in body
    assert "Needs re-auth" in body
    assert "href=\"https://mail/1\"" in body
    assert "action='/inbox/run-now'" in body and "action='/inbox/inboxes/w/pause'" in body
    assert body.count("name='csrf_token'") >= 2


def test_setup_body_has_wizard_steps_forms_and_redirect_uri():
    body = inbox_pages.render_setup_body(CONFIG, {"google": {"client_id": "gid", "client_secret": "s"}}, CSRF,
                                         "http://127.0.0.1:8420/oauth/google/callback")
    assert "console.cloud.google.com" in body and "entra.microsoft.com" in body
    assert "In production" in body and "Allow public client flows" in body
    assert "http://127.0.0.1:8420/oauth/google/callback" in body
    assert "action='/inbox/oauth-client'" in body and "action='/inbox/inboxes'" in body
    assert "action='/inbox/inboxes/w/connect'" in body and "action='/inbox/inboxes/w/test'" in body
    assert "value=\"gid\"" in body and "value=\"s\"" not in body  # never echo the secret back


def test_setup_body_existing_inbox_name_is_readonly():
    body = inbox_pages.render_setup_body(CONFIG, {}, CSRF, "http://127.0.0.1:1/cb")
    assert "name='name' value=\"w\" readonly" in body


def test_history_file_rejects_traversal(tmp_path):
    (tmp_path / "2026-09-27-w.md").write_text("# Work\n\n| a | b |\n")
    assert "Work" in inbox_pages.render_history_file_body("2026-09-27-w.md", history_dir=tmp_path)
    assert inbox_pages.render_history_file_body("../../etc/passwd", history_dir=tmp_path) is None
    assert inbox_pages.render_history_file_body("2026-09-27-missing.md", history_dir=tmp_path) is None


def test_history_list_newest_first(tmp_path):
    for name in ("2026-09-26-w.md", "2026-09-27-w.md"):
        (tmp_path / name).write_text("x")
    body = inbox_pages.render_history_list_body(history_dir=tmp_path)
    assert body.index("2026-09-27-w.md") < body.index("2026-09-26-w.md")
