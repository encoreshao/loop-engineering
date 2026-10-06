#!/usr/bin/env python3
"""OAuth for the Inbox Triage loop: Gmail via loopback redirect + PKCE,
Outlook via the Microsoft device-code flow. Refresh tokens live only in the
macOS Keychain (via the `security` CLI) - never on disk in plain text and
never passed to the AI. See
docs/superpowers/specs/2026-09-27-inbox-triage-design.md."""
import base64
import hashlib
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.parse

import inbox_config
import mail_http

KEYCHAIN_SERVICE = "loop-engineering.mail"
KEYCHAIN_TIMEOUT_SECONDS = 10
_KEYCHAIN_NOT_FOUND = 44

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
# The Google Calendar connector only ever asks for read access.
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

MS_DEVICE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
MS_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
# Mail.Send is deliberately never requested: an Outlook token issued here
# cannot send mail at all.
OUTLOOK_SCOPE = "offline_access https://graph.microsoft.com/Mail.ReadWrite"

STATE_TTL_SECONDS = 600

_PENDING = {}
_PENDING_LOCK = threading.Lock()
_DEVICE_FLOWS = {}
_DEVICE_LOCK = threading.Lock()


class KeychainError(Exception):
    pass


class ReauthRequired(Exception):
    pass


class AuthFlowError(Exception):
    pass


def sandboxed_service(base):
    """Suffixed whenever LOOP_ENGINEERING_HOME is set, so a dev sandbox or
    the test suite can never read or overwrite real secrets."""
    home = os.environ.get("LOOP_ENGINEERING_HOME")
    if not home:
        return base
    digest = hashlib.sha256(os.path.realpath(home).encode()).hexdigest()[:10]
    return f"{base}.sandbox-{digest}"


def keychain_service():
    return sandboxed_service(KEYCHAIN_SERVICE)


def _security(args, stdin=None):
    try:
        return subprocess.run(["/usr/bin/security", *args], input=stdin, capture_output=True, text=True,
                              timeout=KEYCHAIN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise KeychainError("Keychain did not answer in time - is it locked?") from None
    except FileNotFoundError:
        raise KeychainError("macOS `security` CLI not found") from None


def keychain_get(account, service=None):
    service = service or keychain_service()
    result = _security(["find-generic-password", "-s", service, "-a", account, "-w"])
    if result.returncode == 0:
        return result.stdout.strip() or None
    if result.returncode == _KEYCHAIN_NOT_FOUND:
        return None
    raise KeychainError(f"Keychain read failed (exit {result.returncode}): {result.stderr.strip()}")


def _security_quote(value):
    """A double-quoted argument for `security -i`'s own command-line
    tokenizer, which honours backslash escapes inside double quotes
    (verified: `"a b\\"c\\\\d"` is read as `a b"c\\d`). A line break would
    end the command and start another, so it is refused outright."""
    if "\n" in value or "\r" in value or "\x00" in value:
        raise KeychainError("Refusing to store a Keychain value containing a line break")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def keychain_set(account, secret, service=None):
    """The token goes to `security -i` on stdin, never in argv: argv is
    visible to every local user via `ps`, and Outlook rotates its refresh
    token on every refresh, so it would be exposed over and over.
    `security -i` exits non-zero when the command it read fails."""
    service = service or keychain_service()
    command = " ".join(["add-generic-password", "-U", "-s", _security_quote(service),
                        "-a", _security_quote(account), "-w", _security_quote(secret),
                        "-T", "/usr/bin/security"])
    result = _security(["-i"], stdin=command + "\n")
    if result.returncode != 0:
        raise KeychainError(f"Keychain write failed (exit {result.returncode}): {result.stderr.strip()}")


def keychain_delete(account, service=None):
    service = service or keychain_service()
    result = _security(["delete-generic-password", "-s", service, "-a", account])
    if result.returncode not in (0, _KEYCHAIN_NOT_FOUND):
        raise KeychainError(f"Keychain delete failed (exit {result.returncode}): {result.stderr.strip()}")


def make_pkce():
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def create_pending_state(target, verifier, redirect_uri, now=None, kind="inbox"):
    """Mint a single-use sign-in `state` bound to `target` - an inbox name
    (kind "inbox") or a connector id (kind "connector"). The entry keeps the
    legacy `inbox` key so the inbox callback reads it unchanged."""
    now = time.time() if now is None else now
    state = secrets.token_urlsafe(32)
    with _PENDING_LOCK:
        for key in [k for k, v in _PENDING.items() if now - v["created"] > STATE_TTL_SECONDS]:
            del _PENDING[key]
        _PENDING[state] = {"inbox": target, "kind": kind, "target": target, "verifier": verifier,
                           "redirect_uri": redirect_uri, "created": now}
    return state


def peek_pending_kind(state, now=None, include_expired=False):
    """The `kind` of a live pending state without consuming it (None when
    unknown or expired), so the callback can pick a handler first. With
    include_expired the kind of a known-but-expired state is returned too,
    so its own handler can report the expiry; it is still never consumed."""
    now = time.time() if now is None else now
    with _PENDING_LOCK:
        entry = _PENDING.get(state or "")
    if entry is None or (not include_expired and now - entry["created"] > STATE_TTL_SECONDS):
        return None
    return entry.get("kind", "inbox")


def consume_pending_state(state, now=None):
    now = time.time() if now is None else now
    with _PENDING_LOCK:
        entry = _PENDING.pop(state or "", None)
    if entry is None or now - entry["created"] > STATE_TTL_SECONDS:
        return None
    return entry


def google_auth_url(client_id, redirect_uri, state, challenge, login_hint="", scope=None):
    if scope is None:
        scope = GMAIL_SCOPE
    params = {
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": scope, "access_type": "offline", "prompt": "consent",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _oauth_error(exc):
    try:
        return json.loads(exc.body).get("error", "")
    except (ValueError, AttributeError):
        return ""


def google_exchange_code(code, verifier, redirect_uri, client, token_url=None):
    if token_url is None:
        token_url = GOOGLE_TOKEN_URL
    try:
        tokens = mail_http.request_json("POST", token_url, form={
            "code": code, "code_verifier": verifier, "redirect_uri": redirect_uri,
            "client_id": client["client_id"], "client_secret": client["client_secret"],
            "grant_type": "authorization_code",
        })
    except mail_http.MailHTTPError as exc:
        raise AuthFlowError(f"Google rejected the sign-in ({_oauth_error(exc) or exc.status})") from None
    if not tokens.get("refresh_token"):
        raise AuthFlowError("Google returned no refresh token - remove the app's access at myaccount.google.com/permissions and connect again")
    return tokens


def google_refresh(refresh_token, client, token_url=None):
    if token_url is None:
        token_url = GOOGLE_TOKEN_URL
    try:
        tokens = mail_http.request_json("POST", token_url, form={
            "refresh_token": refresh_token, "client_id": client["client_id"],
            "client_secret": client["client_secret"], "grant_type": "refresh_token",
        })
    except mail_http.MailHTTPError as exc:
        if exc.status in (400, 401):
            raise ReauthRequired(f"Google refresh failed ({_oauth_error(exc) or exc.status})") from None
        raise
    return tokens["access_token"]


def ms_start_device_code(client_id, device_url=None):
    if device_url is None:
        device_url = MS_DEVICE_URL
    try:
        return mail_http.request_json("POST", device_url, form={"client_id": client_id, "scope": OUTLOOK_SCOPE})
    except mail_http.MailHTTPError as exc:
        raise AuthFlowError(f"Microsoft rejected the device-code request ({_oauth_error(exc) or exc.status})") from None


_MS_POLL_ERRORS = {
    "authorization_pending": "pending", "slow_down": "slow_down",
    "expired_token": "expired", "bad_verification_code": "expired",
    "authorization_declined": "declined",
}
# Statuses that indicate a transient blip (rate limit, network error, or a
# 5xx from Microsoft) rather than an actual poll outcome - one blip shouldn't
# end a 15-minute device-code flow, so these are treated as "pending" too.
_MS_POLL_TRANSIENT_STATUSES = {None, 429, 500, 502, 503, 504}


def ms_poll_once(client_id, device_code, token_url=None):
    if token_url is None:
        token_url = MS_TOKEN_URL
    try:
        tokens = mail_http.request_json("POST", token_url, form={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id, "device_code": device_code,
        }, max_attempts=1)
    except mail_http.MailHTTPError as exc:
        mapped = _MS_POLL_ERRORS.get(_oauth_error(exc))
        if mapped:
            return mapped, None
        if exc.status in _MS_POLL_TRANSIENT_STATUSES:
            return "pending", None
        raise AuthFlowError(f"Microsoft sign-in failed ({_oauth_error(exc) or exc.status})") from None
    return "success", tokens


def ms_refresh(refresh_token, client_id, token_url=None):
    if token_url is None:
        token_url = MS_TOKEN_URL
    try:
        tokens = mail_http.request_json("POST", token_url, form={
            "grant_type": "refresh_token", "client_id": client_id,
            "refresh_token": refresh_token, "scope": OUTLOOK_SCOPE,
        })
    except mail_http.MailHTTPError as exc:
        if exc.status in (400, 401):
            raise ReauthRequired(f"Microsoft refresh failed ({_oauth_error(exc) or exc.status})") from None
        raise
    return tokens["access_token"], tokens.get("refresh_token")


def _set_flow(inbox_name, **fields):
    with _DEVICE_LOCK:
        _DEVICE_FLOWS.setdefault(inbox_name, {}).update(fields)


def device_flow_status(inbox_name):
    with _DEVICE_LOCK:
        return dict(_DEVICE_FLOWS.get(inbox_name, {"state": "none"}))


def start_device_flow(inbox_name, client_id, on_success, run_in_background=None, sleep=None, now=None):
    """Starts the Microsoft device-code flow and polls it in the background.
    `on_success(tokens)` stores the token (and may raise AuthFlowError, e.g.
    on an account mismatch, which is surfaced as a failed flow)."""
    if run_in_background is None:
        run_in_background = lambda fn: threading.Thread(target=fn, daemon=True).start()  # noqa: E731
    if sleep is None:
        sleep = time.sleep
    if now is None:
        now = time.monotonic
    info = ms_start_device_code(client_id)
    _set_flow(inbox_name, state="pending", user_code=info["user_code"],
              verification_uri=info["verification_uri"], message="Waiting for you to sign in")

    def poll():
        interval = int(info.get("interval", 5))
        deadline = now() + int(info.get("expires_in", 900))
        while now() < deadline:
            sleep(interval)
            try:
                outcome, tokens = ms_poll_once(client_id, info["device_code"])
            except Exception as exc:  # noqa: BLE001 - any failure here must not kill the poll thread silently
                _set_flow(inbox_name, state="failed", message=str(exc) or type(exc).__name__)
                return
            if outcome == "pending":
                continue
            if outcome == "slow_down":
                interval += 5
                continue
            if outcome == "success":
                try:
                    on_success(tokens)
                except Exception as exc:  # noqa: BLE001 - same as above: report, don't crash the thread
                    _set_flow(inbox_name, state="failed", message=str(exc) or type(exc).__name__)
                    return
                _set_flow(inbox_name, state="connected", message="Connected")
                return
            _set_flow(inbox_name, state="failed",
                      message="The code expired - start again" if outcome == "expired" else "Sign-in was declined")
            return
        _set_flow(inbox_name, state="failed", message="The code expired - start again")

    run_in_background(poll)
    return info


def get_access_token(inbox, oauth=None):
    if oauth is None:
        oauth = inbox_config.load_oauth()
    refresh_token = keychain_get(inbox["name"])
    if not refresh_token:
        raise ReauthRequired(f"{inbox['name']} is not connected")
    if inbox["provider"] == "gmail":
        client = oauth.get("google") or {}
        if not client.get("client_id") or not client.get("client_secret"):
            raise ReauthRequired("Google OAuth client is not configured")
        return google_refresh(refresh_token, client)
    client_id = (oauth.get("microsoft") or {}).get("client_id")
    if not client_id:
        raise ReauthRequired("Microsoft OAuth client is not configured")
    access_token, new_refresh = ms_refresh(refresh_token, client_id)
    if new_refresh and new_refresh != refresh_token:
        keychain_set(inbox["name"], new_refresh)
    return access_token


def refresh_access_token(inbox, oauth=None):
    return get_access_token(inbox, oauth=oauth)
