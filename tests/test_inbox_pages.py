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
    assert "/loops/inbox-triage-loop?view=setup" in body and "No inboxes yet" in body


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

import inbox_seen  # noqa: E402
import inbox_status  # noqa: E402
import mail_auth  # noqa: E402


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox_config, "DEFAULT_CONFIG_PATH", tmp_path / "inboxes.json")
    monkeypatch.setattr(inbox_config, "DEFAULT_OAUTH_PATH", tmp_path / "mail_oauth.json")
    monkeypatch.setattr(inbox_status, "DEFAULT_STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(inbox_seen, "DEFAULT_STATE_DIR", tmp_path / "state")
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
    assert result["ok"] and result["location"] == "/loops/inbox-triage-loop?view=setup&tab=inboxes"
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


def test_save_existing_inbox_keeps_custom_categories(sandbox):
    custom = [{"key": "urgent", "label": "Loop/Urgent", "description": "today", "draft": True},
              {"key": "fyi", "label": "Loop/FYI", "description": "rest", "draft": False}]
    inbox_config.upsert_inbox({"name": "w", "label": "Work", "provider": "gmail", "account": "me@example.com",
                               "categories": custom}, is_new=True)
    result = inbox_pages.handle_post("/inbox/inboxes", _form(is_new="0", name="w", label="Work 2", provider="gmail",
                                                             account="me@example.com"), "http://cb")
    assert result["ok"], result
    saved = inbox_config.get_inbox("w")
    assert saved["label"] == "Work 2" and saved["categories"] == custom


def test_connect_outlook_message_uses_verification_uri(sandbox, monkeypatch):
    _add_inbox("outlook")
    inbox_config.save_oauth_client("microsoft", "mid")
    monkeypatch.setattr(mail_auth, "start_device_flow",
                        lambda name, client_id, on_success: {"user_code": "ABCD", "device_code": "SECRET-DC",
                                                             "verification_uri": "https://login.example/device"})
    result = inbox_pages.handle_post("/inbox/inboxes/w/connect", {}, "http://cb")
    assert "https://login.example/device" in result["message"] and "SECRET-DC" not in result["message"]
    monkeypatch.setattr(mail_auth, "start_device_flow", lambda name, client_id, on_success: {"user_code": "ABCD"})
    assert "microsoft.com/devicelogin" in inbox_pages.handle_post("/inbox/inboxes/w/connect", {}, "http://cb")["message"]


def test_google_callback_provider_changed_returns_failure(sandbox, monkeypatch):
    _add_inbox()
    inbox_config.save_oauth_client("google", "gid", "s")
    state = mail_auth.create_pending_state("w", "ver", "http://cb")
    monkeypatch.setattr(mail_auth, "google_exchange_code", lambda code, verifier, redirect_uri, client: {"access_token": "at", "refresh_token": "rt"})

    def factory(inbox, token, refresh=None):
        raise ValueError("Unknown provider 'outlook'")

    monkeypatch.setattr(inbox_pages, "_provider_factory", lambda: factory)
    ok, message = inbox_pages.handle_google_callback({"state": [state], "code": ["c"]})
    assert not ok and "provider" in message.lower()
    assert sandbox["stored"] == {}


def test_google_callback_http_error_returns_failure(sandbox, monkeypatch):
    import mail_http
    _add_inbox()
    inbox_config.save_oauth_client("google", "gid", "s")
    state = mail_auth.create_pending_state("w", "ver", "http://cb")

    def exchange(code, verifier, redirect_uri, client):
        raise mail_http.MailHTTPError(None, "", "https://oauth2.googleapis.com/token")

    monkeypatch.setattr(mail_auth, "google_exchange_code", exchange)
    ok, message = inbox_pages.handle_google_callback({"state": [state], "code": ["c"]})
    assert not ok and "oauth2.googleapis.com" in message
    assert sandbox["stored"] == {}


def test_device_flow_script_clears_code_line_on_failure():
    script = inbox_pages._DEVICE_FLOW_SCRIPT
    assert "s.state === 'failed'" in script


def test_post_actions_with_a_malformed_config_flash_an_error(sandbox):
    (sandbox["tmp"] / "inboxes.json").write_text("{not json")
    for path, form in (("/inbox/inboxes", _form(is_new="1", name="w", label="W", provider="gmail", account="me@example.com")),
                       ("/inbox/inboxes/w/pause", _form()), ("/inbox/inboxes/w/delete", _form())):
        result = inbox_pages.handle_post(path, form, "http://cb")
        assert result["ok"] is False and "inboxes.json" in result["message"], (path, result)


def test_delete_forgets_the_inbox_status_and_seen_state(sandbox):
    from datetime import datetime, timezone
    _add_inbox()
    inbox_status.write("w", "failed", error="old")
    inbox_status.write("other", "idle")
    now = datetime(2026, 9, 27, tzinfo=timezone.utc)
    inbox_seen.record("w", [{"id": "m1", "date": now.isoformat()}], now)
    inbox_seen.record("other", [{"id": "m2", "date": now.isoformat()}], now)
    assert inbox_pages.handle_post("/inbox/inboxes/w/delete", _form(), "http://cb")["ok"]
    assert list(inbox_status.read()["inboxes"]) == ["other"]
    assert inbox_seen.load("w") == {"high_water": None, "seen": {}}
    assert not (sandbox["tmp"] / "state" / "w.json").exists()
    assert inbox_seen.load("other")["seen"]


def test_disconnect_keeps_status_and_seen_state(sandbox):
    from datetime import datetime, timezone
    _add_inbox()
    inbox_status.write("w", "idle")
    now = datetime(2026, 9, 27, tzinfo=timezone.utc)
    inbox_seen.record("w", [{"id": "m1", "date": now.isoformat()}], now)
    assert inbox_pages.handle_post("/inbox/inboxes/w/disconnect", _form(), "http://cb")["ok"]
    assert "w" in inbox_status.read()["inboxes"] and inbox_seen.load("w")["seen"]


# ---- Inbox Setup tabs / sectioned cards ----

from html.parser import HTMLParser  # noqa: E402


class _FormNesting(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.max_depth = 0
        self.forms = 0

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            self.depth += 1
            self.forms += 1
            self.max_depth = max(self.max_depth, self.depth)

    def handle_endtag(self, tag):
        if tag == "form":
            self.depth -= 1


def _fake_select(name, options, selected, empty_label=None):
    pairs = ([("", empty_label)] if empty_label is not None else []) + [
        o if isinstance(o, tuple) else (o, o) for o in options]
    items = "".join(f"<i data-value='{v}'{' sel' if v == (selected or '') else ''}>{l}</i>" for v, l in pairs)
    return f"<div class='custom-select' data-name='{name}'>{items}</div>"


def _setup(config=CONFIG, **kwargs):
    return inbox_pages.render_setup_body(config, {}, CSRF, "http://127.0.0.1:1/cb", **kwargs)


def _panel(body, key):
    return body.split(f"data-tab-panel='{key}'")[1].split("data-tab-panel=")[0]


def test_setup_tabs_in_order_with_icons():
    body = _setup()
    assert "<div data-tabs>" in body and "class='tab-list' role='tablist'" in body
    keys = [chunk.split("'")[0] for chunk in body.split("data-tab-target='")[1:]]
    assert keys == ["inboxes", "add", "gmail", "outlook"]
    tab_list = body.split("class='tab-list'")[1].split("</div>")[0]
    for label in ("Inboxes", "Add inbox", "Gmail app", "Outlook app"):
        assert label in tab_list
    assert "aria-hidden='true'>email</span>Inboxes" in tab_list
    assert "aria-hidden='true'>add</span>Add inbox" in tab_list


def _active(body):
    return body.split("tab-button is-active' data-tab-target='")[1].split("'")[0]


def test_setup_default_tab_is_inboxes_when_any_exist_else_gmail():
    assert _active(_setup()) == "inboxes"
    assert "data-tab-panel='inboxes'>" in _setup() and "data-tab-panel='gmail' hidden>" in _setup()
    empty = {"default_categories": [], "inboxes": []}
    assert _active(_setup(empty)) == "gmail"
    assert "data-tab-panel='inboxes' hidden>" in _setup(empty)


def test_setup_active_tab_param_selects_and_unknown_falls_back():
    for key in ("inboxes", "add", "gmail", "outlook"):
        body = _setup(active_tab=key)
        assert _active(body) == key and f"data-tab-panel='{key}'>" in body
    assert _active(_setup(active_tab="bogus")) == "inboxes"


def test_setup_never_nests_forms():
    config = {"default_categories": [], "inboxes": [INBOX, dict(INBOX, name="o", provider="outlook", label="O")]}
    for select_html in (None, _fake_select):
        parser = _FormNesting()
        parser.feed(_setup(config, select_html=select_html, slack_bundles=["b1"]))
        assert parser.forms > 0 and parser.max_depth == 1 and parser.depth == 0


def test_setup_inbox_card_sections_and_buttons():
    body = _panel(_setup(status={"inboxes": {"w": {"state": "needs_reauth"}}}), "inboxes")
    assert "Work &lt;Gmail&gt;" in body and "Needs re-auth" in body
    positions = [body.index(h) for h in (">Account<", ">Triage rules<", ">Notifications<", ">Connection<")]
    assert positions == sorted(positions)
    for verb in ("connect", "test", "disconnect", "delete"):
        assert f"action='/inbox/inboxes/w/{verb}'" in body
    assert 'data-confirm="Delete inbox Work &lt;Gmail&gt;? This removes its settings, sign-in and saved state."' in body
    assert body.index("Save</button>") < body.index("Delete</button>")
    save_btn = body.split("Save</button>")[0].rsplit("<button", 1)[1]
    assert "btn-primary" in save_btn and "form='inbox-edit-w'" in save_btn
    delete_btn = body.split("Delete</button>")[0].rsplit("<button", 1)[1]
    assert "btn-warning" in delete_btn and "form='inbox-delete-w'" in delete_btn


def test_setup_inbox_card_shows_paused_pill_when_disabled():
    config = {"default_categories": [], "inboxes": [dict(INBOX, enabled=False)]}
    body = _panel(_setup(config, status={"inboxes": {"w": {"state": "idle"}}}), "inboxes")
    assert "Paused" in body and "Connected" in body


def test_setup_inboxes_tab_empty_state_links_to_add():
    body = _panel(_setup({"default_categories": [], "inboxes": []}), "inboxes")
    assert "href='/loops/inbox-triage-loop?view=setup&tab=add'" in body


def test_setup_add_tab_has_editable_name_and_no_connection():
    body = _panel(_setup(), "add")
    assert "name='name' value=\"\" required" in body and "readonly" not in body
    assert "Add inbox</button>" in body and ">Connection<" not in body
    assert "sign in from the Inboxes tab" in body
    assert ">Account<" in body and ">Triage rules<" in body and ">Notifications<" in body


def test_setup_oauth_tabs_keep_wizard_content():
    body = _setup()
    assert "console.cloud.google.com" in _panel(body, "gmail") and "Connect Gmail" in _panel(body, "gmail")
    assert "entra.microsoft.com" in _panel(body, "outlook") and "Connect Outlook" in _panel(body, "outlook")


def test_setup_provider_and_bundle_use_injected_custom_select():
    config = {"default_categories": [], "inboxes": [dict(INBOX, provider="outlook", slack_bundle="b2")]}
    body = _setup(config, select_html=_fake_select, slack_bundles=["b1", "b2"])
    card = _panel(body, "inboxes")
    assert "<select" not in card
    assert "data-name='provider'" in card and "<i data-value='outlook' sel>Outlook</i>" in card
    assert "<i data-value='gmail'>Gmail</i>" in card
    assert "data-name='slack_bundle'" in card and "<i data-value='b2' sel>b2</i>" in card
    assert "<i data-value=''>(use default webhook)</i>" in card
    add = _panel(body, "add")
    assert "<i data-value='' sel>(use default webhook)</i>" in add and "<i data-value='gmail' sel>Gmail</i>" in add


def test_setup_keeps_an_unknown_saved_bundle_selectable():
    config = {"default_categories": [], "inboxes": [dict(INBOX, slack_bundle="gone")]}
    card = _panel(_setup(config, select_html=_fake_select, slack_bundles=["b1"]), "inboxes")
    assert "<i data-value='gone' sel>gone</i>" in card


def test_setup_fallback_select_without_injected_renderer():
    card = _panel(_setup(slack_bundles=["b1"]), "inboxes")
    assert "<select name='provider'>" in card and "<option value='gmail' selected>Gmail</option>" in card
    assert "<select name='slack_bundle'>" in card and "<option value='' selected>(use default webhook)</option>" in card


def test_post_locations_carry_the_tab(sandbox, monkeypatch):
    bad_add = inbox_pages.handle_post("/inbox/inboxes", _form(is_new="1", name="Bad Name", label="", provider="gmail",
                                                              account="x"), "http://cb")
    assert not bad_add["ok"] and bad_add["location"] == "/loops/inbox-triage-loop?view=setup&tab=add"
    assert _add_inbox()["location"] == "/loops/inbox-triage-loop?view=setup&tab=inboxes"
    edit = inbox_pages.handle_post("/inbox/inboxes", _form(is_new="0", name="w", label="W", provider="gmail",
                                                           account="me@example.com"), "http://cb")
    assert edit["location"] == "/loops/inbox-triage-loop?view=setup&tab=inboxes"
    monkeypatch.setattr(mail_auth, "get_access_token", lambda inbox: (_ for _ in ()).throw(mail_auth.ReauthRequired("x")))
    for verb in ("connect", "test", "disconnect"):
        assert inbox_pages.handle_post(f"/inbox/inboxes/w/{verb}", {}, "http://cb")["location"] == "/loops/inbox-triage-loop?view=setup&tab=inboxes"
    google = inbox_pages.handle_post("/inbox/oauth-client", _form(provider="google", client_id="g", client_secret="s"), "http://cb")
    assert google["location"] == "/loops/inbox-triage-loop?view=setup&tab=gmail"
    ms = inbox_pages.handle_post("/inbox/oauth-client", _form(provider="microsoft", client_id="m"), "http://cb")
    assert ms["location"] == "/loops/inbox-triage-loop?view=setup&tab=outlook"
    assert inbox_pages.handle_post("/inbox/inboxes/w/pause", {}, "http://cb")["location"] == "/loops/inbox-triage-loop"
    assert inbox_pages.handle_post("/inbox/inboxes/w/delete", {}, "http://cb")["location"] == "/loops/inbox-triage-loop?view=setup&tab=inboxes"


def test_inbox_empty_state_uses_email_icon():
    body = inbox_pages.render_inbox_body({"default_categories": [], "inboxes": []}, {"inboxes": {}}, CSRF)
    assert "aria-hidden='true'>email</span>" in body and ">inbox</span>" not in body


def test_inbox_body_spaces_cards_in_a_grid_and_shows_counts_as_tiles():
    status = {"inboxes": {"w": {"state": "ok", "last_run_at": "2026-09-28T09:00:12",
                                "counts": {"needs_reply": 5, "fyi": 14},
                                "urgent": [{"from": "a@x.com", "subject": "S1", "draft_failed": True},
                                           {"from": "b@x.com", "subject": "S2"}]}}}
    body = inbox_pages.render_inbox_body(CONFIG, status, CSRF)
    assert "<div class='grid'>" in body
    assert body.count("class='inbox-stat'") == 2
    assert "<span class='inbox-stat-value'>5</span><span class='inbox-stat-label'>Needs reply</span>" in body
    assert "Urgent <span class='inbox-count'>2</span>" in body
    assert "inbox-stat-label'>FYI</span>" in body
    assert "2026-09-28 09:00" in body and "T09:00:12" not in body


def test_inbox_body_no_counts_yet_says_so():
    body = inbox_pages.render_inbox_body(CONFIG, {"inboxes": {}}, CSRF)
    assert "No triage runs yet" in body and "last run never" not in body


def _labels_of(form_html):
    import re
    return re.findall(r"<label>(.*?)<(?:input|textarea)", form_html, re.S)


def test_setup_field_labels_are_one_element_each():
    """Every label's caption is a single <span>, so a flex-column label
    can't break a caption like "VIP senders ... @domain.com ..." into
    several lines."""
    add = _panel(_setup(), "add")
    labels = _labels_of(add)
    assert labels
    for caption in labels:
        assert caption.startswith("<span class='field-label'>") and caption.count("<span class='field-label'>") == 1, caption


def test_setup_triage_fields_explain_themselves_with_hints():
    add = _panel(_setup(), "add")
    assert "<span class='field-label'>VIP senders</span>" in add
    assert "<code>@domain.com</code>" in add.split("VIP senders", 1)[1].split("</label>", 1)[0]
    assert "<span class='field-label'>Private senders</span>" in add
    assert "field-hint" in add.split("Inbox ID", 1)[1].split("</label>", 1)[0]


def test_drafts_body_lists_saved_drafts_escaped_newest_first():
    records = [
        {"inbox_label": "Work <Gmail>", "from": "Bob <b@x.com>", "subject": "Re: <Plan>", "date": "2026-09-27",
         "category": "urgent", "body": "Hi Bob,\nOn <it>.", "draft_link": "https://mail/1", "saved_at": "2026-09-27T09:00:00+00:00"},
        {"inbox_label": "Home", "from": "c", "subject": "Older", "date": "d", "category": "fyi",
         "body": "x", "draft_link": None, "saved_at": "2026-09-26T09:00:00+00:00"}]
    body = inbox_pages.render_drafts_body(records)
    assert "Work &lt;Gmail&gt;" in body and "Re: &lt;Plan&gt;" in body and "On &lt;it&gt;." in body
    assert "<it>" not in body and "href=\"https://mail/1\"" in body
    assert body.index("Re: &lt;Plan&gt;") < body.index("Older")


def test_drafts_body_empty_state():
    assert "No drafts saved yet" in inbox_pages.render_drafts_body([])


def test_drafts_body_ignores_unsafe_link():
    body = inbox_pages.render_drafts_body([{"subject": "s", "body": "b", "draft_link": "javascript:alert(1)"}])
    assert "javascript:" not in body
