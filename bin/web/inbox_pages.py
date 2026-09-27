#!/usr/bin/env python3
"""HTML bodies for the dashboard's Inbox Triage pages. Pure functions of
their inputs: dashboard_server.py reads config/status, wraps these in
_render_shell, and owns routing and CSRF. Kept out of dashboard_server.py
to avoid growing that file further. Reuses dashboard_server.py's own CSS
class names (.card, .pill/.pill-*, .btn/.btn-primary/.btn-neutral,
.daemon-action-form, .field-list, .empty-state*, .section-subtitle,
ul.plain) rather than inventing parallel ones - see _STYLE in
dashboard_server.py for what each one looks like. Never renders message
bodies - status and history only ever hold sender, subject, category,
reason, draft link.

The action handlers at the bottom (handle_post, handle_google_callback,
connect_status) are dashboard_server.py's POST/callback back ends: it
does the CSRF check and routing, these do the config/Keychain/OAuth work
and return a flash message. None of them ever puts a token, an OAuth
code, or a Microsoft device_code into a returned message or payload."""
import html
import re
from pathlib import Path

import inbox_config
import mail_auth
import mail_http

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_HISTORY_DIR = REPO_ROOT / "outputs" / "inbox-triage" / "history"
_HISTORY_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9-]+\.md$")

# state -> (label, pill CSS modifier) - same pill-green/pill-blue/pill-red/
# pill-grey vocabulary _status_badge uses in dashboard_server.py, so an
# inbox's chip reads consistently with every other status pill in the app.
_STATE_LABELS = {
    "idle": ("Connected", "pill-green"),
    "running": ("Running", "pill-blue"),
    "failed": ("Failed", "pill-red"),
    "needs_reauth": ("Needs re-auth", "pill-red"),
    None: ("Never run", "pill-grey"),
}

e = html.escape


def _chip(inbox, entry):
    if not inbox.get("enabled", True):
        return "<span class='pill pill-grey'>Paused</span>"
    text, kind = _STATE_LABELS.get(entry.get("state"), ("Unknown", "pill-grey"))
    return f"<span class='pill {kind}'>{e(text)}</span>"


def _post_button(action, label, csrf_input, extra_class=""):
    """One tiny form-with-a-single-button - same shape as every other
    daemon-action-form single-button POST in dashboard_server.py (see
    _run_now_action_html/_stop_action_html), just not tied to a loop
    confirm dialog."""
    cls = f"btn {extra_class}" if extra_class else "btn btn-neutral"
    return (f"<form method='POST' action='{e(action)}' class='daemon-action-form'>{csrf_input}"
            f"<button type='submit' class='{cls}'>{e(label)}</button></form>")


def render_inbox_body(config, status, csrf_input):
    inboxes = config.get("inboxes", [])
    head = (
        "<div class='page-title'><h1>Inbox Triage</h1>"
        "<p class='subtitle'>Status for every connected inbox. Drafts are saved to your mailbox and never "
        "delivered automatically - nothing is archived, deleted, or sent on your behalf. "
        "<a href='/inbox/setup'>Setup</a> &middot; <a href='/inbox/history'>History</a></p></div>"
    )
    if not inboxes:
        return head + (
            "<div class='card'><div class='empty-state'>"
            "<div class='empty-state-icon'><span class='material-symbols-outlined' aria-hidden='true'>inbox</span></div>"
            "<p class='empty-state-message'>No inboxes yet. Connect Gmail or Outlook from Inbox Setup to start "
            "triaging - drafts are saved to your mailbox and never delivered automatically.</p>"
            "<a class='btn btn-primary empty-state-action' href='/inbox/setup'>"
            "<span class='material-symbols-outlined' aria-hidden='true'>settings</span> Set up an inbox</a>"
            "</div></div>"
        )

    run_now = _post_button("/inbox/run-now", "Run now", csrf_input, "btn-primary")
    cards = [
        "<div class='card'><div class='section-header'><h2>Inboxes</h2></div>"
        f"<p class='section-subtitle'>Trigger a triage pass for every connected inbox right now, outside its normal schedule.</p>"
        f"<div class='daemon-action-form'>{run_now}</div></div>"
    ]
    for inbox in inboxes:
        entry = status.get("inboxes", {}).get(inbox["name"], {})
        counts = ", ".join(f"{e(str(k))} {v}" for k, v in (entry.get("counts") or {}).items()) or "none yet"
        urgent_entries = entry.get("urgent") or []
        if urgent_entries:
            items = []
            for item in urgent_entries:
                if item.get("draft_link"):
                    action_html = f"<a href=\"{e(item['draft_link'])}\" target='_blank' rel='noopener'>Open draft &#8599;</a>"
                elif item.get("draft_failed"):
                    action_html = "draft failed - reply manually"
                else:
                    action_html = "reply manually"
                items.append(
                    f"<li><span class='k'>{e(item.get('from', ''))}</span>"
                    f"{e(item.get('subject', ''))} &middot; {action_html}</li>"
                )
            urgent_html = f"<ul class='field-list'>{''.join(items)}</ul>"
        else:
            urgent_html = "<p class='section-subtitle'>Nothing urgent in the last run.</p>"
        error_html = f"<p class='error-text'>{e(str(entry['error']))}</p>" if entry.get("error") else ""
        pause_label = "Resume" if not inbox.get("enabled", True) else "Pause"
        pause_button = _post_button(f"/inbox/inboxes/{inbox['name']}/pause", pause_label, csrf_input)
        cards.append(
            "<div class='card'>"
            f"<div class='section-header'><h2>{e(inbox['label'])}</h2>{_chip(inbox, entry)}</div>"
            f"<p class='section-subtitle'>{e(inbox['account'])} &middot; {e(inbox['provider'].title())} &middot; "
            f"last run {e(str(entry.get('last_run_at') or 'never'))}</p>"
            f"<p>Categories: {counts}</p>{error_html}"
            f"<h3>Urgent</h3>{urgent_html}"
            f"<div class='daemon-action-form'>{pause_button}</div>"
            "</div>"
        )
    return head + "".join(cards)


def _google_steps(redirect_uri):
    return f"""
<ol class='wizard-steps'>
  <li>Open <a href='https://console.cloud.google.com/projectcreate' target='_blank' rel='noopener'>console.cloud.google.com</a> and create (or pick) a project.</li>
  <li>Enable the <a href='https://console.cloud.google.com/apis/library/gmail.googleapis.com' target='_blank' rel='noopener'>Gmail API</a>.</li>
  <li>Under <em>OAuth consent screen</em>, choose <strong>External</strong>, add yourself as a test user, then click <strong>Publish app</strong> so its status is <strong>In production</strong> - a "Testing" app's sign-in expires every 7 days. An "unverified app" warning during sign-in is expected for personal use.</li>
  <li>Under <em>Credentials &rarr; Create credentials &rarr; OAuth client ID</em>, choose <strong>Desktop app</strong>. Desktop clients accept this dashboard's loopback address automatically: <code>{e(redirect_uri)}</code></li>
  <li>Paste the client ID and client secret below.</li>
</ol>"""


_MICROSOFT_STEPS = """
<ol class='wizard-steps'>
  <li>Open <a href='https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade' target='_blank' rel='noopener'>entra.microsoft.com &rarr; App registrations</a> &rarr; <strong>New registration</strong>.</li>
  <li>Supported account types: <strong>Accounts in any organizational directory and personal Microsoft accounts</strong>. Leave the redirect URI empty.</li>
  <li>Under <em>Authentication</em>, set <strong>Allow public client flows</strong> to <strong>Yes</strong>.</li>
  <li>Under <em>API permissions</em>, add Microsoft Graph delegated permissions <code>Mail.ReadWrite</code> and <code>offline_access</code>. Do not add <code>Mail.Send</code>.</li>
  <li>Paste the <strong>Application (client) ID</strong> below. No secret is needed.</li>
</ol>"""


def _client_form(provider, title, oauth, csrf_input, with_secret):
    client_id = (oauth.get(provider) or {}).get("client_id", "")
    configured = "<span class='pill pill-green'>Saved</span>" if client_id else "<span class='pill pill-grey'>Not set</span>"
    secret = ("<label>Client secret <input type='password' name='client_secret' autocomplete='off' "
              "placeholder='(unchanged unless you type a new one)'></label>") if with_secret else ""
    return (
        f"<form method='POST' action='/inbox/oauth-client' class='stack-form'>{csrf_input}"
        f"<input type='hidden' name='provider' value='{provider}'><h3>{e(title)} {configured}</h3>"
        f"<label>Client ID <input type='text' name='client_id' value=\"{e(client_id)}\" required></label>{secret}"
        f"<button type='submit' class='btn btn-primary'>Save</button></form>"
    )


def _lines(values):
    return e("\n".join(values or []))


def _inbox_form(inbox, csrf_input):
    is_new = inbox is None
    inbox = inbox or {"name": "", "label": "", "provider": "gmail", "account": "", "urgent_brief": "",
                       "vip_senders": [], "exclude_senders": [], "slack_bundle": None}
    readonly = "" if is_new else " readonly"
    options = "".join(
        f"<option value='{p}'{' selected' if inbox['provider'] == p else ''}>{p.title()}</option>"
        for p in ("gmail", "outlook")
    )
    return (
        f"<form method='POST' action='/inbox/inboxes' class='stack-form'>{csrf_input}"
        f"<input type='hidden' name='is_new' value='{'1' if is_new else '0'}'>"
        f"<label>Name (slug, fixed once created) <input type='text' name='name' value=\"{e(inbox['name'])}\"{readonly} required pattern='[a-z0-9][a-z0-9-]*'></label>"
        f"<label>Label <input type='text' name='label' value=\"{e(inbox['label'])}\" required></label>"
        f"<label>Provider <select name='provider'>{options}</select></label>"
        f"<label>Account email <input type='email' name='account' value=\"{e(inbox['account'])}\" required></label>"
        f"<label>What counts as urgent <textarea name='urgent_brief' rows='2'>{e(inbox.get('urgent_brief', ''))}</textarea></label>"
        f"<label>VIP senders (one per line; <code>@domain.com</code> allowed) <textarea name='vip_senders' rows='2'>{_lines(inbox.get('vip_senders'))}</textarea></label>"
        f"<label>Never send to the AI (one per line) <textarea name='exclude_senders' rows='2'>{_lines(inbox.get('exclude_senders'))}</textarea></label>"
        f"<label>Slack bundle (blank = default webhook) <input type='text' name='slack_bundle' value=\"{e(inbox.get('slack_bundle') or '')}\"></label>"
        f"<button type='submit' class='btn btn-primary'>{'Add inbox' if is_new else 'Save'}</button></form>"
    )


def render_setup_body(config, oauth, csrf_input, redirect_uri):
    parts = [
        "<div class='page-title'><h1>Inbox Setup</h1>"
        "<p class='subtitle'>Connect a Gmail or Outlook inbox for the loop to triage. "
        "Nothing is ever sent, archived, or deleted automatically.</p></div>",
        "<div class='card'><div class='section-header'><h2>Connect Gmail</h2></div>",
        _google_steps(redirect_uri),
        _client_form("google", "Google OAuth client", oauth, csrf_input, with_secret=True),
        "</div>",
        "<div class='card'><div class='section-header'><h2>Connect Outlook</h2></div>",
        _MICROSOFT_STEPS,
        _client_form("microsoft", "Microsoft app", oauth, csrf_input, with_secret=False),
        "</div>",
    ]
    for inbox in config.get("inboxes", []):
        name = inbox["name"]
        actions = "".join(
            _post_button(f"/inbox/inboxes/{name}/{verb}", label, csrf_input)
            for verb, label in (("connect", "Connect"), ("test", "Test connection"),
                                ("disconnect", "Disconnect"), ("delete", "Delete"))
        )
        device = (
            f"<div class='device-flow' data-inbox='{e(name)}' hidden><p>Go to "
            "<a class='device-uri' target='_blank' rel='noopener'></a> and enter "
            "<code class='device-code'></code></p><p class='device-message section-subtitle'></p></div>"
        ) if inbox["provider"] == "outlook" else ""
        parts.append(
            f"<div class='card'><div class='section-header'><h2>{e(inbox['label'])}</h2></div>"
            f"{_inbox_form(inbox, csrf_input)}"
            f"<div class='daemon-action-form'>{actions}</div>{device}</div>"
        )
    parts.append(
        f"<div class='card'><div class='section-header'><h2>Add an inbox</h2></div>{_inbox_form(None, csrf_input)}</div>"
    )
    parts.append(_DEVICE_FLOW_SCRIPT)
    return "".join(parts)


_DEVICE_FLOW_SCRIPT = """
<script>
document.querySelectorAll('.device-flow').forEach(function (box) {
  var inbox = box.getAttribute('data-inbox');
  function poll() {
    fetch('/inbox/connect/status?inbox=' + encodeURIComponent(inbox)).then(function (r) { return r.json(); }).then(function (s) {
      if (s.state === 'none') { return; }
      box.hidden = false;
      box.querySelector('.device-uri').textContent = s.verification_uri || '';
      box.querySelector('.device-uri').href = s.verification_uri || '#';
      box.querySelector('.device-code').textContent = s.user_code || '';
      box.querySelector('.device-message').textContent = s.message || '';
      if (s.state === 'pending') { setTimeout(poll, 3000); }
      if (s.state === 'connected' || s.state === 'failed') {
        // The code is spent either way - drop the stale "Go to ... enter code" line.
        box.querySelector('p').hidden = true;
        box.querySelector('.device-message').textContent =
          s.state === 'connected' ? 'Connected' : (s.message || 'Sign-in failed - click Connect again');
      }
    }).catch(function () {});
  }
  poll();
});
</script>"""


def render_history_list_body(history_dir=None):
    if history_dir is None:
        history_dir = DEFAULT_HISTORY_DIR
    files = sorted((p.name for p in Path(history_dir).glob("*.md") if _HISTORY_NAME_RE.match(p.name)), reverse=True)
    if not files:
        return "<h1>Inbox Triage history</h1><div class='grid'><div class='card'><p class='section-subtitle'>No runs yet.</p></div></div>"
    items = "".join(f"<li><a href='/inbox/history/{e(n)}'>{e(n)}</a></li>" for n in files)
    return f"<h1>Inbox Triage history</h1><div class='grid'><div class='card'><ul class='plain'>{items}</ul></div></div>"


def read_history_file(name, history_dir=None):
    """Validated read of one saved Inbox Triage run's raw markdown -
    `name` must match `_HISTORY_NAME_RE` (rejects path traversal and any
    other unexpected filename) and must exist under `history_dir`, else
    None. Deliberately returns raw text, not rendered HTML:
    dashboard_server.py owns rendering it (via render_markdown, same
    .markdown-wrapped pattern as /history/<name> and
    /topic-monitor/history/<name>) since inbox_pages.py can't import
    dashboard_server.py without a circular import."""
    if history_dir is None:
        history_dir = DEFAULT_HISTORY_DIR
    if not _HISTORY_NAME_RE.match(name or ""):
        return None
    path = Path(history_dir) / name
    if not path.is_file():
        return None
    return path.read_text()


# ---- Actions (POST back ends, the Google callback, device-flow status) ----

# The only device-flow fields the browser ever sees. mail_auth's own
# device_flow_status already omits device_code, but whitelisting here keeps
# that guarantee local to the one function that feeds the JSON endpoint.
_DEVICE_STATUS_FIELDS = ("state", "user_code", "verification_uri", "message")


def _provider_factory():
    import mail_providers
    return mail_providers.get_provider


def _value(form, key, default=""):
    return (form.get(key) or [default])[0]


def _split_lines(text):
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _result(ok, message, location="/inbox/setup"):
    return {"ok": ok, "message": message, "location": location}


def store_tokens_for(inbox, tokens, provider_factory=None):
    """Verifies the signed-in mailbox is the inbox's configured account,
    then stores the refresh token in the Keychain. Raises
    mail_auth.AuthFlowError (and saves nothing) on an account mismatch."""
    if provider_factory is None:
        provider_factory = _provider_factory()
    address = provider_factory(inbox, tokens["access_token"]).profile_address()
    if address != inbox["account"].strip().lower():
        raise mail_auth.AuthFlowError(f"Signed in as {address}, expected {inbox['account']} - nothing was saved")
    mail_auth.keychain_set(inbox["name"], tokens["refresh_token"])


def _connect(inbox, redirect_uri):
    oauth = inbox_config.load_oauth()
    if inbox["provider"] == "gmail":
        client = oauth.get("google") or {}
        if not client.get("client_id") or not client.get("client_secret"):
            return _result(False, "Save the Google OAuth client ID and secret first")
        verifier, challenge = mail_auth.make_pkce()
        state = mail_auth.create_pending_state(inbox["name"], verifier, redirect_uri)
        return {"redirect": mail_auth.google_auth_url(client["client_id"], redirect_uri, state, challenge, inbox["account"])}
    client_id = (oauth.get("microsoft") or {}).get("client_id")
    if not client_id:
        return _result(False, "Save the Microsoft app's client ID first")
    try:
        info = mail_auth.start_device_flow(inbox["name"], client_id,
                                           on_success=lambda tokens: store_tokens_for(inbox, tokens))
    except (mail_auth.AuthFlowError, mail_http.MailHTTPError) as exc:
        return _result(False, str(exc))
    # Only user_code/verification_uri go into the flash - never info["device_code"].
    uri = info.get("verification_uri") or "microsoft.com/devicelogin"
    return _result(True, f"Enter code {info['user_code']} at {uri} to finish connecting {inbox['label']}")


def _test_connection(inbox):
    try:
        token = mail_auth.get_access_token(inbox)
        address = _provider_factory()(inbox, token).profile_address()
    except (mail_auth.ReauthRequired, mail_auth.KeychainError) as exc:
        return _result(False, f"{inbox['label']}: {exc}")
    except Exception as exc:  # noqa: BLE001 - network/API errors shown to the user, not raised into the handler
        return _result(False, f"{inbox['label']}: {type(exc).__name__}: {exc}")
    if address != inbox["account"].strip().lower():
        return _result(False, f"{inbox['label']}: signed in as {address}, expected {inbox['account']}")
    return _result(True, f"{inbox['label']}: connected as {address}")


def handle_post(path, form, redirect_uri):
    """Back end for every POST under /inbox/ except /inbox/run-now (which
    dashboard_server.py handles itself). Returns {"ok", "message",
    "location"} for a flash redirect, {"redirect": url} for an off-site
    redirect (Google sign-in), or None for an unknown path (404)."""
    if path == "/inbox/oauth-client":
        provider = _value(form, "provider")
        secret = _value(form, "client_secret")
        if provider == "google" and not secret:
            secret = (inbox_config.load_oauth().get("google") or {}).get("client_secret", "")
        ok, message = inbox_config.save_oauth_client(provider, _value(form, "client_id"), secret)
        return _result(ok, message)
    if path == "/inbox/inboxes":
        fields = {
            "name": _value(form, "name"), "label": _value(form, "label"), "provider": _value(form, "provider"),
            "account": _value(form, "account"), "urgent_brief": _value(form, "urgent_brief"),
            "vip_senders": _split_lines(_value(form, "vip_senders")),
            "exclude_senders": _split_lines(_value(form, "exclude_senders")),
            "slack_bundle": _value(form, "slack_bundle"), "enabled": True,
        }
        ok, message = inbox_config.upsert_inbox(fields, is_new=_value(form, "is_new") == "1")
        return _result(ok, message)
    match = re.match(r"^/inbox/inboxes/([a-z0-9][a-z0-9-]*)/(connect|disconnect|test|pause|delete)$", path)
    if not match:
        return None
    name, verb = match.groups()
    try:
        inbox = inbox_config.get_inbox(name, inbox_config.DEFAULT_CONFIG_PATH)
    except (KeyError, FileNotFoundError):
        return _result(False, f"No inbox named {name}")
    if verb == "connect":
        return _connect(inbox, redirect_uri)
    if verb == "test":
        return _test_connection(inbox)
    if verb == "pause":
        ok, message = inbox_config.set_enabled(name, not inbox.get("enabled", True))
        return _result(ok, message, location="/inbox")
    try:
        mail_auth.keychain_delete(name)
    except mail_auth.KeychainError as exc:
        return _result(False, str(exc))
    if verb == "disconnect":
        return _result(True, f"Disconnected {inbox['label']}")
    ok, message = inbox_config.delete_inbox(name)
    return _result(ok, message)


def handle_google_callback(query):
    """Google's loopback redirect. The single-use `state` (minted only by
    a CSRF-checked connect POST) is the CSRF defense. Returns (ok,
    message); the message never contains the code or any token."""
    if query.get("error"):
        return False, f"Google sign-in was cancelled ({query['error'][0]})"
    pending = mail_auth.consume_pending_state((query.get("state") or [""])[0])
    if pending is None:
        return False, "That sign-in link expired or was already used - click Connect again"
    try:
        inbox = inbox_config.get_inbox(pending["inbox"], inbox_config.DEFAULT_CONFIG_PATH)
        client = inbox_config.load_oauth().get("google") or {}
        tokens = mail_auth.google_exchange_code((query.get("code") or [""])[0], pending["verifier"],
                                                pending["redirect_uri"], client)
        store_tokens_for(inbox, tokens)
    # ValueError: get_provider's "Unknown provider", e.g. the inbox's provider
    # was changed while this sign-in was pending.
    except (KeyError, ValueError, FileNotFoundError, mail_auth.AuthFlowError, mail_auth.KeychainError,
            mail_http.MailHTTPError) as exc:
        return False, str(exc)
    return True, f"Connected {inbox['label']}"


def connect_status(inbox_name):
    status = mail_auth.device_flow_status(inbox_name)
    return {k: status[k] for k in _DEVICE_STATUS_FIELDS if k in status}
