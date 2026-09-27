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
    assert "Work" in inbox_pages.read_history_file("2026-09-27-w.md", history_dir=tmp_path)
    assert inbox_pages.read_history_file("../../etc/passwd", history_dir=tmp_path) is None
    assert inbox_pages.read_history_file("2026-09-27-missing.md", history_dir=tmp_path) is None


def test_history_list_newest_first(tmp_path):
    for name in ("2026-09-26-w.md", "2026-09-27-w.md"):
        (tmp_path / name).write_text("x")
    body = inbox_pages.render_history_list_body(history_dir=tmp_path)
    assert body.index("2026-09-27-w.md") < body.index("2026-09-26-w.md")


import pytest  # noqa: E402

import mail_auth  # noqa: E402


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox_config, "DEFAULT_CONFIG_PATH", tmp_path / "inboxes.json")
    monkeypatch.setattr(inbox_config, "DEFAULT_OAUTH_PATH", tmp_path / "mail_oauth.json")
    stored, deleted = {}, []
    monkeypatch.setattr(mail_auth, "keychain_set", lambda account, secret: stored.update({account: secret}))
    monkeypatch.setattr(mail_auth, "keychain_delete", lambda account: deleted.append(account))
    mail_auth._PENDING.clear()
    mail_auth._DEVICE_FLOWS.clear()
    return {"stored": stored, "deleted": deleted, "tmp": tmp_path}


def _form(**fields):
    return {k: [v] for k, v in fields.items()}


def _add_inbox(provider="gmail"):
    return inbox_pages.handle_post("/inbox/inboxes", _form(is_new="1", name="w", label="Work", provider=provider,
                                                            account="me@example.com", vip_senders="@vip.com\n\nboss@x.com"), "http://cb")


def test_save_inbox_splits_lines(sandbox):
    result = _add_inbox()
    assert result["ok"] and result["location"] == "/inbox/setup"
    assert inbox_config.get_inbox("w")["vip_senders"] == ["@vip.com", "boss@x.com"]


def test_save_oauth_client_keeps_secret_when_blank(sandbox):
    inbox_pages.handle_post("/inbox/oauth-client", _form(provider="google", client_id="gid", client_secret="s1"), "http://cb")
    inbox_pages.handle_post("/inbox/oauth-client", _form(provider="google", client_id="gid2", client_secret=""), "http://cb")
    assert inbox_config.load_oauth()["google"] == {"client_id": "gid2", "client_secret": "s1"}


def test_connect_gmail_redirects_to_google_with_state(sandbox):
    _add_inbox()
    inbox_config.save_oauth_client("google", "gid", "s")
    result = inbox_pages.handle_post("/inbox/inboxes/w/connect", {}, "http://127.0.0.1:8420/oauth/google/callback")
    assert result["redirect"].startswith("https://accounts.google.com/")
    assert len(mail_auth._PENDING) == 1


def test_connect_without_client_config_explains(sandbox):
    _add_inbox()
    result = inbox_pages.handle_post("/inbox/inboxes/w/connect", {}, "http://cb")
    assert not result["ok"] and "client" in result["message"].lower()


def test_connect_outlook_starts_device_flow(sandbox, monkeypatch):
    _add_inbox("outlook")
    inbox_config.save_oauth_client("microsoft", "mid")
    started = []
    monkeypatch.setattr(mail_auth, "start_device_flow",
                        lambda name, client_id, on_success: started.append((name, client_id)) or {"user_code": "ABCD"})
    result = inbox_pages.handle_post("/inbox/inboxes/w/connect", {}, "http://cb")
    assert result["ok"] and "ABCD" in result["message"]
    assert started == [("w", "mid")]


def test_google_callback_rejects_bad_state(sandbox):
    ok, message = inbox_pages.handle_google_callback({"state": ["nope"], "code": ["c"]})
    assert not ok and "expired" in message.lower()


def test_google_callback_error_param(sandbox):
    ok, message = inbox_pages.handle_google_callback({"error": ["access_denied"]})
    assert not ok and "access_denied" in message


def test_google_callback_success_stores_token_once(sandbox, monkeypatch):
    _add_inbox()
    inbox_config.save_oauth_client("google", "gid", "s")
    state = mail_auth.create_pending_state("w", "ver", "http://cb")
    monkeypatch.setattr(mail_auth, "google_exchange_code", lambda code, verifier, redirect_uri, client: {"access_token": "at", "refresh_token": "rt"})
    monkeypatch.setattr(inbox_pages, "_provider_factory", lambda: (lambda inbox, token, refresh=None: type("P", (), {"profile_address": lambda self: "me@example.com"})()))
    ok, message = inbox_pages.handle_google_callback({"state": [state], "code": ["c"]})
    assert ok and sandbox["stored"] == {"w": "rt"}
    ok, _ = inbox_pages.handle_google_callback({"state": [state], "code": ["c"]})
    assert not ok


def test_store_tokens_rejects_wrong_account(sandbox):
    factory = lambda inbox, token, refresh=None: type("P", (), {"profile_address": lambda self: "other@x.com"})()  # noqa: E731
    with pytest.raises(mail_auth.AuthFlowError, match="other@x.com"):
        inbox_pages.store_tokens_for({"name": "w", "provider": "gmail", "account": "me@example.com"},
                                     {"access_token": "at", "refresh_token": "rt"}, provider_factory=factory)
    assert sandbox["stored"] == {}


def test_disconnect_delete_pause_and_unknown(sandbox):
    _add_inbox()
    assert inbox_pages.handle_post("/inbox/inboxes/w/pause", {}, "http://cb")["ok"]
    assert inbox_config.get_inbox("w")["enabled"] is False
    assert inbox_pages.handle_post("/inbox/inboxes/w/disconnect", {}, "http://cb")["ok"]
    assert sandbox["deleted"] == ["w"]
    assert inbox_pages.handle_post("/inbox/inboxes/w/delete", {}, "http://cb")["ok"]
    assert sandbox["deleted"] == ["w", "w"]
    assert inbox_pages.handle_post("/inbox/nope", {}, "http://cb") is None
    assert not inbox_pages.handle_post("/inbox/inboxes/ghost/connect", {}, "http://cb")["ok"]


def test_test_connection_reports_mismatch_and_success(sandbox, monkeypatch):
    _add_inbox()
    monkeypatch.setattr(mail_auth, "get_access_token", lambda inbox: "at")
    for address, ok in (("me@example.com", True), ("x@y.com", False)):
        monkeypatch.setattr(inbox_pages, "_provider_factory",
                            lambda address=address: (lambda inbox, token, refresh=None: type("P", (), {"profile_address": lambda self: address})()))
        assert inbox_pages.handle_post("/inbox/inboxes/w/test", {}, "http://cb")["ok"] is ok


def test_connect_status_passthrough(sandbox):
    assert inbox_pages.connect_status("w") == {"state": "none"}


def test_connect_outlook_never_exposes_device_code(sandbox, monkeypatch):
    _add_inbox("outlook")
    inbox_config.save_oauth_client("microsoft", "mid")
    monkeypatch.setattr(mail_auth, "start_device_flow",
                        lambda name, client_id, on_success: {"user_code": "ABCD", "device_code": "SECRET-DC",
                                                             "verification_uri": "https://microsoft.com/devicelogin"})
    result = inbox_pages.handle_post("/inbox/inboxes/w/connect", {}, "http://cb")
    assert "SECRET-DC" not in repr(result)


def test_connect_status_only_exposes_whitelisted_fields(sandbox, monkeypatch):
    monkeypatch.setattr(mail_auth, "device_flow_status",
                        lambda name: {"state": "pending", "user_code": "ABCD", "verification_uri": "https://v",
                                      "message": "Waiting", "device_code": "SECRET-DC"})
    assert inbox_pages.connect_status("w") == {"state": "pending", "user_code": "ABCD",
                                               "verification_uri": "https://v", "message": "Waiting"}


def test_google_callback_message_never_carries_tokens_or_code(sandbox, monkeypatch):
    _add_inbox()
    inbox_config.save_oauth_client("google", "gid", "s")
    state = mail_auth.create_pending_state("w", "ver", "http://cb")
    monkeypatch.setattr(mail_auth, "google_exchange_code",
                        lambda code, verifier, redirect_uri, client: {"access_token": "AT-SECRET", "refresh_token": "RT-SECRET"})
    monkeypatch.setattr(inbox_pages, "_provider_factory", lambda: (lambda inbox, token, refresh=None: type("P", (), {"profile_address": lambda self: "other@x.com"})()))
    ok, message = inbox_pages.handle_google_callback({"state": [state], "code": ["CODE-SECRET"]})
    assert not ok and "other@x.com" in message
    for secret in ("AT-SECRET", "RT-SECRET", "CODE-SECRET", state):
        assert secret not in message
    assert sandbox["stored"] == {}


def test_device_flow_script_shows_connected_on_success():
    assert "s.state === 'connected'" in inbox_pages._DEVICE_FLOW_SCRIPT
    assert "Connected" in inbox_pages._DEVICE_FLOW_SCRIPT
