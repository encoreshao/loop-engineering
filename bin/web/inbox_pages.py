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

import i18n
import inbox_config
import inbox_seen
import inbox_status
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
_t = i18n.t


def _js_str(text):
    """`text` as a single-quoted JS string literal (translations may carry
    apostrophes)."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'").replace("<", "\\x3c") + "'"


def _chip(inbox, entry):
    if not inbox.get("enabled", True):
        return f"<span class='pill pill-grey'>{e(_t('Paused'))}</span>"
    text, kind = _STATE_LABELS.get(entry.get("state"), ("Unknown", "pill-grey"))
    return f"<span class='pill {kind}'>{e(i18n.t(text))}</span>"


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
        f"<div class='page-title'><h1>{e(_t('Inbox Triage'))}</h1>"
        "<p class='subtitle'>" + e(_t("Status for every connected inbox. Drafts are saved to your mailbox and never "
                                      "delivered automatically - nothing is archived, deleted, or sent on your behalf."))
        + f" <a href='/inbox/setup'>{e(_t('Setup'))}</a> &middot; <a href='/inbox/history'>{e(_t('History'))}</a></p></div>"
    )
    if not inboxes:
        return head + (
            "<div class='card'><div class='empty-state'>"
            "<div class='empty-state-icon'><span class='material-symbols-outlined' aria-hidden='true'>email</span></div>"
            "<p class='empty-state-message'>"
            + e(_t("No inboxes yet. Connect Gmail or Outlook from Inbox Setup to start "
                   "triaging - drafts are saved to your mailbox and never delivered automatically."))
            + "</p>"
            "<a class='btn btn-primary empty-state-action' href='/inbox/setup'>"
            f"<span class='material-symbols-outlined' aria-hidden='true'>settings</span> {e(_t('Set up an inbox'))}</a>"
            "</div></div>"
        )

    run_now = _post_button("/inbox/run-now", _t("Run now"), csrf_input, "btn-primary")
    count = len(inboxes)
    cards = [
        "<div class='card'><div class='inbox-card-head'><div>"
        f"<h2>{e(_t('Inboxes'))} <span class='inbox-count'>{count}</span></h2>"
        "<p class='section-subtitle'>"
        + e(_t("Trigger a triage pass for every connected inbox right now, outside its normal schedule."))
        + "</p></div>"
        f"<div class='daemon-action-form'>{run_now}</div></div></div>"
    ]
    for inbox in inboxes:
        cards.append(_inbox_status_card(inbox, status.get("inboxes", {}).get(inbox["name"], {}), csrf_input))
    return head + "<div class='grid'>" + "".join(cards) + "</div>"


def _format_run_time(value):
    """ISO timestamp -> "YYYY-MM-DD HH:MM" (seconds/offset dropped); any
    other string is shown as-is."""
    if not value:
        return None
    text = str(value)
    if len(text) >= 16 and text[10] == "T":
        return f"{text[:10]} {text[11:16]}"
    return text


def _category_label(key):
    if str(key).lower() == "fyi":
        return "FYI"
    return str(key).replace("_", " ").replace("-", " ").strip().capitalize()


def _inbox_status_card(inbox, entry, csrf_input):
    """One inbox on the Inbox Triage page: label/state/Pause header, an
    account meta line, category counts as tiles, then the Urgent list."""
    pause_label = _t("Resume") if not inbox.get("enabled", True) else _t("Pause")
    pause_button = _post_button(f"/inbox/inboxes/{inbox['name']}/pause", pause_label, csrf_input)
    last_run = _format_run_time(entry.get("last_run_at"))
    meta = f"{e(inbox['account'])} &middot; {e(inbox['provider'].title())} &middot; " + (
        e(_t("last run {when}", when=last_run)) if last_run else e(_t("not run yet")))

    counts = entry.get("counts") or {}
    if counts:
        tiles = "".join(
            f"<div class='inbox-stat'><span class='inbox-stat-value'>{e(str(v))}</span>"
            f"<span class='inbox-stat-label'>{e(_category_label(k))}</span></div>"
            for k, v in counts.items()
        )
        counts_html = f"<div class='inbox-stats'>{tiles}</div>"
    else:
        counts_html = ("<p class='section-subtitle'>"
                       + e(_t("No triage runs yet - counts per category appear after the first run.")) + "</p>")

    urgent_entries = entry.get("urgent") or []
    if urgent_entries:
        items = []
        for item in urgent_entries:
            if item.get("draft_link"):
                action_html = (f"<a class='btn btn-neutral' href=\"{e(item['draft_link'])}\" target='_blank' "
                               f"rel='noopener'>{e(_t('Open draft'))} &#8599;</a>")
            elif item.get("draft_failed"):
                action_html = f"<span class='pill pill-red'>{e(_t('Draft failed - reply manually'))}</span>"
            else:
                action_html = f"<span class='pill pill-grey'>{e(_t('Reply manually'))}</span>"
            items.append(
                "<li><div class='inbox-urgent-text'>"
                f"<span class='inbox-urgent-subject'>{e(item.get('subject', ''))}</span>"
                f"<span class='inbox-urgent-from'>{e(item.get('from', ''))}</span></div>"
                f"{action_html}</li>"
            )
        urgent_html = f"<ul class='inbox-urgent-list'>{''.join(items)}</ul>"
    else:
        urgent_html = f"<p class='section-subtitle'>{e(_t('Nothing urgent in the last run.'))}</p>"
    error_html = f"<div class='flash flash-danger'>{e(str(entry['error']))}</div>" if entry.get("error") else ""

    return (
        "<div class='card'>"
        "<div class='inbox-card-head'><div>"
        f"<div class='section-header'><h2>{e(inbox['label'])}</h2>{_chip(inbox, entry)}</div>"
        f"<p class='section-subtitle'>{meta}</p></div>"
        f"<div class='daemon-action-form'>{pause_button}</div></div>"
        f"{error_html}"
        f"<div class='inbox-section'><h3>{e(_t('Categories'))}</h3>{counts_html}</div>"
        f"<div class='inbox-section'><h3>{e(_t('Urgent'))} <span class='inbox-count'>{len(urgent_entries)}</span></h3>{urgent_html}</div>"
        "</div>"
    )


def _google_steps(redirect_uri):
    # Google Cloud console UI names (<em>/<strong> pieces) stay in English -
    # they're what the user has to find on Google's own pages.
    step1 = _t("Open {link} and create (or pick) a project.", link=(
        "<a href='https://console.cloud.google.com/projectcreate' target='_blank' rel='noopener'>"
        "console.cloud.google.com</a>"))
    step2 = _t("Enable the {link}.", link=(
        "<a href='https://console.cloud.google.com/apis/library/gmail.googleapis.com' target='_blank' "
        "rel='noopener'>Gmail API</a>"))
    step3 = _t(
        "Under {consent}, choose {external}, add yourself as a test user, then click {publish} so its status is "
        "{production} - a \"Testing\" app's sign-in expires every 7 days. An \"unverified app\" warning during "
        "sign-in is expected for personal use.",
        consent="<em>OAuth consent screen</em>", external="<strong>External</strong>",
        publish="<strong>Publish app</strong>", production="<strong>In production</strong>")
    step4 = _t(
        "Under {credentials}, choose {desktop}. Desktop clients accept this dashboard's loopback address "
        "automatically: {uri}",
        credentials="<em>Credentials &rarr; Create credentials &rarr; OAuth client ID</em>",
        desktop="<strong>Desktop app</strong>", uri=f"<code>{e(redirect_uri)}</code>")
    step5 = _t("Paste the client ID and client secret below.")
    return f"""
<ol class='wizard-steps'>
  <li>{step1}</li>
  <li>{step2}</li>
  <li>{step3}</li>
  <li>{step4}</li>
  <li>{step5}</li>
</ol>"""


def _microsoft_steps():
    # Microsoft Entra UI names (<em>/<strong> pieces) stay in English -
    # they're what the user has to find on Microsoft's own pages.
    step1 = _t("Open {link} &rarr; {button}.", link=(
        "<a href='https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade' "
        "target='_blank' rel='noopener'>entra.microsoft.com &rarr; App registrations</a>"),
        button="<strong>New registration</strong>")
    step2 = _t("Supported account types: {types}. Leave the redirect URI empty.",
               types="<strong>Accounts in any organizational directory and personal Microsoft accounts</strong>")
    step3 = _t("Under {section}, set {setting} to {value}.", section="<em>Authentication</em>",
               setting="<strong>Allow public client flows</strong>", value="<strong>Yes</strong>")
    step4 = _t("Under {section}, add Microsoft Graph delegated permissions {read_write} and {offline}. "
               "Do not add {send}.",
               section="<em>API permissions</em>", read_write="<code>Mail.ReadWrite</code>",
               offline="<code>offline_access</code>", send="<code>Mail.Send</code>")
    step5 = _t("Paste the {field} below. No secret is needed.",
               field="<strong>Application (client) ID</strong>")
    return f"""
<ol class='wizard-steps'>
  <li>{step1}</li>
  <li>{step2}</li>
  <li>{step3}</li>
  <li>{step4}</li>
  <li>{step5}</li>
</ol>"""


def _client_form(provider, title, oauth, csrf_input, with_secret):
    client_id = (oauth.get(provider) or {}).get("client_id", "")
    configured = (f"<span class='pill pill-green'>{e(_t('Saved'))}</span>" if client_id
                  else f"<span class='pill pill-grey'>{e(_t('Not set'))}</span>")
    secret = (f"<label>{e(_t('Client secret'))} <input type='password' name='client_secret' autocomplete='off' "
              f"placeholder='{e(_t('(unchanged unless you type a new one)'))}'></label>") if with_secret else ""
    return (
        f"<form method='POST' action='/inbox/oauth-client' class='stack-form'>{csrf_input}"
        f"<input type='hidden' name='provider' value='{provider}'><h3>{e(title)} {configured}</h3>"
        f"<label>{e(_t('Client ID'))} <input type='text' name='client_id' value=\"{e(client_id)}\" required></label>{secret}"
        f"<button type='submit' class='btn btn-primary'>{e(_t('Save'))}</button></form>"
    )


def _lines(values):
    return e("\n".join(values or []))


_PROVIDER_OPTIONS = (("gmail", "Gmail"), ("outlook", "Outlook"))

# (key, label, Material Symbols glyph) - every glyph must be in
# dashboard_server._MATERIAL_SYMBOLS_ICON_NAMES or it renders as tofu.
_SETUP_TABS = (
    ("inboxes", "Inboxes", "email"),
    ("add", "Add inbox", "add"),
    ("gmail", "Gmail app", "settings"),
    ("outlook", "Outlook app", "settings"),
)


def _plain_select(name, options, selected, empty_label=None):
    """Fallback for render_setup_body's `select_html` - a native <select>
    with the same (name, options, selected, empty_label) contract as
    dashboard_server._custom_select, so this module stays testable on its
    own without importing dashboard_server."""
    pairs = ([("", empty_label)] if empty_label is not None else []) + [
        o if isinstance(o, tuple) else (o, o) for o in options]
    selected = selected or ""
    tags = "".join(f"<option value='{e(v)}'{' selected' if v == selected else ''}>{e(l)}</option>" for v, l in pairs)
    return f"<select name='{e(name)}'>{tags}</select>"


def _section(title, inner):
    return f"<div class='inbox-section'><h3>{e(title)}</h3>{inner}</div>"


def _field(label, control):
    """A labelled custom dropdown. A <div>, not a <label>: a label forwards
    clicks on the dropdown's menu options back to its trigger button."""
    return f"<div class='stack-field'><span>{label}</span>{control}</div>"


def _inbox_fields(inbox, is_new, select_html, slack_bundles):
    readonly = "" if is_new else " readonly"
    bundles = list(slack_bundles or [])
    if inbox.get("slack_bundle") and inbox["slack_bundle"] not in bundles:
        # A saved bundle since removed from gitlab.json stays selectable,
        # so saving an unrelated field doesn't silently clear it.
        bundles.append(inbox["slack_bundle"])
    vip_label = _t("VIP senders (one per line; {example} allowed)", example="<code>@domain.com</code>")
    account = (
        f"<label>{e(_t('Name (slug, fixed once created)'))} <input type='text' name='name' value=\"{e(inbox['name'])}\"{readonly} required pattern='[a-z0-9][a-z0-9-]*'></label>"
        + _field(e(_t("Provider")), select_html("provider", _PROVIDER_OPTIONS, inbox["provider"]))
        + f"<label>{e(_t('Account email'))} <input type='email' name='account' value=\"{e(inbox['account'])}\" required></label>"
        f"<label>{e(_t('Label'))} <input type='text' name='label' value=\"{e(inbox['label'])}\" required></label>"
    )
    triage = (
        f"<label>{e(_t('What counts as urgent'))} <textarea name='urgent_brief' rows='2'>{e(inbox.get('urgent_brief', ''))}</textarea></label>"
        f"<label>{vip_label} <textarea name='vip_senders' rows='2'>{_lines(inbox.get('vip_senders'))}</textarea></label>"
        f"<label>{e(_t('Never send to the AI (one per line)'))} <textarea name='exclude_senders' rows='2'>{_lines(inbox.get('exclude_senders'))}</textarea></label>"
    )
    notifications = _field(e(_t("Slack bundle")), select_html("slack_bundle", bundles, inbox.get("slack_bundle") or "",
                                                              empty_label=_t("(use default webhook)")))
    return (_section(_t("Account"), account) + _section(_t("Triage rules"), triage)
            + _section(_t("Notifications"), notifications))


def _inbox_form(inbox, csrf_input, select_html, slack_bundles, form_id, footer=""):
    is_new = inbox is None
    inbox = inbox or {"name": "", "label": "", "provider": "gmail", "account": "", "urgent_brief": "",
                      "vip_senders": [], "exclude_senders": [], "slack_bundle": None}
    return (
        f"<form method='POST' action='/inbox/inboxes' class='stack-form' id='{e(form_id)}'>{csrf_input}"
        f"<input type='hidden' name='is_new' value='{'1' if is_new else '0'}'>"
        f"{_inbox_fields(inbox, is_new, select_html, slack_bundles)}{footer}</form>"
    )


def _state_chips(inbox, entry):
    text, kind = _STATE_LABELS.get(entry.get("state"), ("Unknown", "pill-grey"))
    chips = f"<span class='pill {kind}'>{e(i18n.t(text))}</span>"
    if not inbox.get("enabled", True):
        chips += f" <span class='pill pill-grey'>{e(_t('Paused'))}</span>"
    return chips


def _inbox_card(inbox, entry, csrf_input, select_html, slack_bundles):
    """One existing inbox: the edit form (Account/Triage rules/
    Notifications), then Connection, then a Save/Delete footer. Every
    action button targets its own empty CSRF form via form= (the Topic
    Settings trick), so no <form> is ever nested inside another."""
    name = inbox["name"]
    edit_id, delete_id = f"inbox-edit-{name}", f"inbox-delete-{name}"
    hidden_forms = "".join(
        f"<form method='POST' action='/inbox/inboxes/{e(name)}/{verb}' id='inbox-{verb}-{e(name)}' hidden>{csrf_input}</form>"
        for verb in ("connect", "test", "disconnect", "delete")
    )
    buttons = "".join(
        f"<button type='submit' form='inbox-{verb}-{e(name)}' class='btn btn-neutral'>{e(label)}</button>"
        for verb, label in (("connect", _t("Connect")), ("test", _t("Test connection")),
                            ("disconnect", _t("Disconnect")))
    )
    device_prompt = _t("Go to {link} and enter {code}",
                       link="<a class='device-uri' target='_blank' rel='noopener'></a>",
                       code="<code class='device-code'></code>")
    device = (
        f"<div class='device-flow' data-inbox='{e(name)}' hidden><p>{device_prompt}</p>"
        "<p class='device-message section-subtitle'></p></div>"
    ) if inbox["provider"] == "outlook" else ""
    confirm = e(_t("Delete inbox {label}? This removes its settings, sign-in and saved state.", label=inbox['label']),
                quote=True)
    footer = (
        "<div class='inbox-card-footer'>"
        f"<button type='submit' form='{e(edit_id)}' class='btn btn-primary'>"
        f"<span class='material-symbols-outlined' aria-hidden='true'>save</span> {e(_t('Save'))}</button>"
        f"<button type='submit' form='{e(delete_id)}' class='btn btn-warning' data-confirm=\"{confirm}\">"
        f"<span class='material-symbols-outlined' aria-hidden='true'>delete</span> {e(_t('Delete'))}</button>"
        "</div>"
    )
    return (
        "<div class='card'>"
        f"<div class='section-header'><h2>{e(inbox['label'])}</h2>{_state_chips(inbox, entry)}</div>"
        f"{_inbox_form(inbox, csrf_input, select_html, slack_bundles, edit_id)}"
        + _section(_t("Connection"), f"<div class='daemon-action-form'>{buttons}</div>{device}")
        + f"{footer}{hidden_forms}</div>"
    )


def render_setup_body(config, oauth, csrf_input, redirect_uri, status=None, select_html=None,
                      slack_bundles=None, active_tab=None):
    """Inbox Setup, as Settings-page-style tabs (the data-tabs markup
    render_general_settings_page uses; _render_shell's script switches
    them). `select_html` is dashboard_server._custom_select, injected
    because this module can't import dashboard_server; None falls back to
    a native <select>. `active_tab` defaults to "inboxes" when any inbox
    exists, else "gmail" (first step of setting one up)."""
    if select_html is None:
        select_html = _plain_select
    status = status or {"inboxes": {}}
    inboxes = config.get("inboxes", [])
    if active_tab not in {key for key, _label, _icon in _SETUP_TABS}:
        active_tab = "inboxes" if inboxes else "gmail"

    if inboxes:
        inboxes_panel = "".join(
            _inbox_card(inbox, status.get("inboxes", {}).get(inbox["name"], {}), csrf_input, select_html, slack_bundles)
            for inbox in inboxes
        )
    else:
        inboxes_panel = (
            "<div class='card'><div class='empty-state'>"
            "<div class='empty-state-icon'><span class='material-symbols-outlined' aria-hidden='true'>email</span></div>"
            "<p class='empty-state-message'>"
            + e(_t("No inboxes yet. Save the Gmail or Outlook app credentials first, then add an inbox."))
            + "</p>"
            "<a class='btn btn-primary empty-state-action' href='/inbox/setup?tab=add'>"
            f"<span class='material-symbols-outlined' aria-hidden='true'>add</span> {e(_t('Add inbox'))}</a>"
            "</div></div>"
        )
    add_footer = ("<div class='inbox-card-footer'><button type='submit' class='btn btn-primary'>"
                  f"<span class='material-symbols-outlined' aria-hidden='true'>add</span> {e(_t('Add inbox'))}</button></div>")
    add_panel = (
        f"<div class='card'><div class='section-header'><h2>{e(_t('Add an inbox'))}</h2></div>"
        "<p class='section-subtitle'>"
        + e(_t("Connect, Test connection and Disconnect appear on the Inboxes tab after saving."))
        + "</p>"
        f"{_inbox_form(None, csrf_input, select_html, slack_bundles, 'inbox-add-form', add_footer)}</div>"
    )
    gmail_panel = (
        f"<div class='card'><div class='section-header'><h2>{e(_t('Connect Gmail'))}</h2></div>"
        + _google_steps(redirect_uri)
        + _client_form("google", _t("Google OAuth client"), oauth, csrf_input, with_secret=True) + "</div>"
    )
    outlook_panel = (
        f"<div class='card'><div class='section-header'><h2>{e(_t('Connect Outlook'))}</h2></div>"
        + _microsoft_steps()
        + _client_form("microsoft", _t("Microsoft app"), oauth, csrf_input, with_secret=False) + "</div>"
    )
    panels = {"inboxes": inboxes_panel, "add": add_panel, "gmail": gmail_panel, "outlook": outlook_panel}

    tab_buttons = "".join(
        f"<button type='button' class='tab-button{' is-active' if key == active_tab else ''}' "
        f"data-tab-target='{key}' role='tab' aria-selected='{'true' if key == active_tab else 'false'}'>"
        f"<span class='material-symbols-outlined' aria-hidden='true'>{icon}</span>{e(i18n.t(label))}</button>"
        for key, label, icon in _SETUP_TABS
    )
    tab_panels = "".join(
        f"<div data-tab-panel='{key}'{'' if key == active_tab else ' hidden'}>{panels[key]}</div>"
        for key, _label, _icon in _SETUP_TABS
    )
    return (
        f"<div class='page-title'><h1>{e(_t('Inbox Setup'))}</h1>"
        "<p class='subtitle'>"
        + e(_t("Connect a Gmail or Outlook inbox for the loop to triage. "
               "Nothing is ever sent, archived, or deleted automatically."))
        + "</p></div>"
        f"<div data-tabs><div class='tab-list' role='tablist'>{tab_buttons}</div>{tab_panels}</div>"
        + _DEVICE_FLOW_SCRIPT
        .replace("'Connected'", _js_str(_t("Connected")))
        .replace("'Sign-in failed - click Connect again'", _js_str(_t("Sign-in failed - click Connect again")))
    )


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
        return (f"<h1>{e(_t('Inbox Triage history'))}</h1><div class='grid'><div class='card'>"
                f"<p class='section-subtitle'>{e(_t('No runs yet.'))}</p></div></div>")
    items = "".join(f"<li><a href='/inbox/history/{e(n)}'>{e(n)}</a></li>" for n in files)
    return f"<h1>{e(_t('Inbox Triage history'))}</h1><div class='grid'><div class='card'><ul class='plain'>{items}</ul></div></div>"


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


# Where an Inbox Setup action lands afterwards - the tab it came from
# (render_setup_body reads ?tab=; _redirect_with_flash appends &flash=).
_INBOXES_TAB = "/inbox/setup?tab=inboxes"
_OAUTH_TABS = {"google": "/inbox/setup?tab=gmail", "microsoft": "/inbox/setup?tab=outlook"}


def _result(ok, message, location=_INBOXES_TAB):
    return {"ok": ok, "message": message, "location": location}


def store_tokens_for(inbox, tokens, provider_factory=None):
    """Verifies the signed-in mailbox is the inbox's configured account,
    then stores the refresh token in the Keychain. Raises
    mail_auth.AuthFlowError (and saves nothing) on an account mismatch."""
    if provider_factory is None:
        provider_factory = _provider_factory()
    address = provider_factory(inbox, tokens["access_token"]).profile_address()
    if address != inbox["account"].strip().lower():
        raise mail_auth.AuthFlowError(_t("Signed in as {address}, expected {account} - nothing was saved",
                                         address=address, account=inbox['account']))
    mail_auth.keychain_set(inbox["name"], tokens["refresh_token"])


def _connect(inbox, redirect_uri):
    oauth = inbox_config.load_oauth()
    if inbox["provider"] == "gmail":
        client = oauth.get("google") or {}
        if not client.get("client_id") or not client.get("client_secret"):
            return _result(False, _t("Save the Google OAuth client ID and secret first"))
        verifier, challenge = mail_auth.make_pkce()
        state = mail_auth.create_pending_state(inbox["name"], verifier, redirect_uri)
        return {"redirect": mail_auth.google_auth_url(client["client_id"], redirect_uri, state, challenge, inbox["account"])}
    client_id = (oauth.get("microsoft") or {}).get("client_id")
    if not client_id:
        return _result(False, _t("Save the Microsoft app's client ID first"))
    try:
        info = mail_auth.start_device_flow(inbox["name"], client_id,
                                           on_success=lambda tokens: store_tokens_for(inbox, tokens))
    except (mail_auth.AuthFlowError, mail_http.MailHTTPError) as exc:
        return _result(False, str(exc))
    # Only user_code/verification_uri go into the flash - never info["device_code"].
    uri = info.get("verification_uri") or "microsoft.com/devicelogin"
    return _result(True, _t("Enter code {code} at {uri} to finish connecting {label}",
                            code=info['user_code'], uri=uri, label=inbox['label']))


def _test_connection(inbox):
    try:
        token = mail_auth.get_access_token(inbox)
        address = _provider_factory()(inbox, token).profile_address()
    except (mail_auth.ReauthRequired, mail_auth.KeychainError) as exc:
        return _result(False, f"{inbox['label']}: {exc}")
    except Exception as exc:  # noqa: BLE001 - network/API errors shown to the user, not raised into the handler
        return _result(False, f"{inbox['label']}: {type(exc).__name__}: {exc}")
    if address != inbox["account"].strip().lower():
        return _result(False, _t("{label}: signed in as {address}, expected {account}",
                                 label=inbox['label'], address=address, account=inbox['account']))
    return _result(True, _t("{label}: connected as {address}", label=inbox['label'], address=address))


def handle_post(path, form, redirect_uri):
    """Back end for every POST under /inbox/ except /inbox/run-now (which
    dashboard_server.py handles itself). Returns {"ok", "message",
    "location"} for a flash redirect, {"redirect": url} for an off-site
    redirect (Google sign-in), or None for an unknown path (404). A
    malformed inboxes.json (ValueError from inbox_config) is a flash
    message naming the file, never a 500."""
    try:
        return _handle_post(path, form, redirect_uri)
    except ValueError as exc:
        return _result(False, _t("Could not load {path} - fix or remove that file: {error}",
                                 path=inbox_config.DEFAULT_CONFIG_PATH, error=exc))


def _handle_post(path, form, redirect_uri):
    if path == "/inbox/oauth-client":
        provider = _value(form, "provider")
        secret = _value(form, "client_secret")
        if provider == "google" and not secret:
            secret = (inbox_config.load_oauth().get("google") or {}).get("client_secret", "")
        ok, message = inbox_config.save_oauth_client(provider, _value(form, "client_id"), secret)
        return _result(ok, message, location=_OAUTH_TABS.get(provider, "/inbox/setup"))
    if path == "/inbox/inboxes":
        fields = {
            "name": _value(form, "name"), "label": _value(form, "label"), "provider": _value(form, "provider"),
            "account": _value(form, "account"), "urgent_brief": _value(form, "urgent_brief"),
            "vip_senders": _split_lines(_value(form, "vip_senders")),
            "exclude_senders": _split_lines(_value(form, "exclude_senders")),
            "slack_bundle": _value(form, "slack_bundle"), "enabled": True,
        }
        is_new = _value(form, "is_new") == "1"
        ok, message = inbox_config.upsert_inbox(fields, is_new=is_new)
        # A failed add goes back to the Add inbox tab it was typed on.
        return _result(ok, message, location="/inbox/setup?tab=add" if is_new and not ok else _INBOXES_TAB)
    match = re.match(r"^/inbox/inboxes/([a-z0-9][a-z0-9-]*)/(connect|disconnect|test|pause|delete)$", path)
    if not match:
        return None
    name, verb = match.groups()
    try:
        inbox = inbox_config.get_inbox(name, inbox_config.DEFAULT_CONFIG_PATH)
    except (KeyError, FileNotFoundError):
        return _result(False, _t("No inbox named {name}", name=name))
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
        return _result(True, _t("Disconnected {label}", label=inbox['label']))
    ok, message = inbox_config.delete_inbox(name)
    if ok:
        inbox_status.remove(name)
        inbox_seen.forget(name)
    return _result(ok, message)


def handle_google_callback(query):
    """Google's loopback redirect. The single-use `state` (minted only by
    a CSRF-checked connect POST) is the CSRF defense. Returns (ok,
    message); the message never contains the code or any token."""
    if query.get("error"):
        return False, _t("Google sign-in was cancelled ({error})", error=query['error'][0])
    pending = mail_auth.consume_pending_state((query.get("state") or [""])[0])
    if pending is None:
        return False, _t("That sign-in link expired or was already used - click Connect again")
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
    return True, _t("Connected {label}", label=inbox['label'])


def connect_status(inbox_name):
    status = mail_auth.device_flow_status(inbox_name)
    return {k: status[k] for k in _DEVICE_STATUS_FIELDS if k in status}
