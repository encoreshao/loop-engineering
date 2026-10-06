#!/usr/bin/env python3
"""Google Calendar connector (read-only). The stored secret is a Google
refresh token obtained through the dashboard's Connect with Google flow
(auth = "oauth_google"); the OAuth client is the one saved on the Inbox
Triage setup page. No message built here ever contains the refresh token
or an access token."""
import urllib.parse

import i18n
from connectors import register
from connectors.base import CALENDAR, Connector, Field, TEST_TIMEOUT_SECONDS, describe_http_error

API_URL = "https://www.googleapis.com/calendar/v3"
_REDACTED = "<redacted>"


def _default_client():
    import inbox_config
    return (inbox_config.load_oauth() or {}).get("google") or {}


def _default_refresh(refresh_token, client):
    import mail_auth
    return mail_auth.google_refresh(refresh_token, client)


@register
class GoogleCalendarConnector(Connector):
    type = "google_calendar"
    label = "Google Calendar"
    icon = "calendar_month"
    capabilities = frozenset({CALENDAR})
    auth = "oauth_google"
    secret_label = None
    fields = (Field("calendar_id", "Calendar ID", default="primary", required=True, placeholder="primary",
                    help="Use primary for your main calendar, or a calendar's ID from its Google Calendar settings"),)
    brand = "googlecalendar"
    category = "google"
    description = "Read your calendar events (read-only)."
    docs_url = "https://support.google.com/calendar/answer/37082"

    def __init__(self, account, secret=None, http=None, client_fn=None, refresh_fn=None):
        super().__init__(account, secret=secret, http=http)
        self.client_fn = client_fn or _default_client
        self.refresh_fn = refresh_fn or _default_refresh

    def _calendar_path(self):
        calendar_id = (self.settings.get("calendar_id") or "primary").strip() or "primary"
        return f"/calendars/{urllib.parse.quote(calendar_id, safe='')}"

    def _scrub(self, text, access_token=None):
        text = str(text)
        for token in (self.secret, access_token):
            if token:
                text = text.replace(str(token), _REDACTED)
        return text

    def _access_token(self):
        """(token, None) or (None, failure message)."""
        if not self.secret:
            return None, i18n.t("Not connected - click Connect with Google")
        client = self.client_fn() or {}
        if not client.get("client_id") or not client.get("client_secret"):
            return None, i18n.t("Add a Google OAuth client on the Inbox Triage setup page first")
        import mail_auth
        try:
            tokens = self.refresh_fn(self.secret, client)
        except mail_auth.ReauthRequired:
            return None, i18n.t("Google sign-in expired or was revoked - click Reconnect")
        except Exception as exc:  # noqa: BLE001 - never raise out of a probe
            return None, self._scrub(describe_http_error(exc))
        token = tokens.get("access_token") if isinstance(tokens, dict) else tokens
        if not token:
            return None, i18n.t("Google returned no access token - click Reconnect")
        return str(token), None

    def _get(self, path, token, timeout=30, **kw):
        return self.http("GET", f"{API_URL}{path}", token=token, timeout=timeout, **kw)

    def test(self):
        token, error = self._access_token()
        if error:
            return False, error
        try:
            cal = self._get(self._calendar_path(), token, timeout=TEST_TIMEOUT_SECONDS, max_attempts=1)
        except Exception as exc:  # noqa: BLE001 - test() must never raise
            return False, self._scrub(describe_http_error(exc), token)
        summary = cal.get("summary") if isinstance(cal, dict) else None
        return True, i18n.t("Connected: {summary}", summary=self._scrub(summary or "?", token))

    def list_events(self, time_min_iso, time_max_iso, max_results=50):
        """Events between two RFC 3339 instants, expanded and in start order.
        Raises ConnectorError (token-free message) on failure."""
        from connectors.base import ConnectorError
        token, error = self._access_token()
        if error:
            raise ConnectorError(error)
        query = urllib.parse.urlencode({
            "timeMin": time_min_iso, "timeMax": time_max_iso, "maxResults": int(max_results),
            "singleEvents": "true", "orderBy": "startTime",
        })
        try:
            data = self._get(f"{self._calendar_path()}/events?{query}", token)
        except Exception as exc:  # noqa: BLE001 - re-raised without token-bearing detail
            raise ConnectorError(self._scrub(describe_http_error(exc), token)) from None
        events = []
        for item in (data.get("items") if isinstance(data, dict) else None) or []:
            if not isinstance(item, dict):
                continue
            start, end = item.get("start") or {}, item.get("end") or {}
            attendees = item.get("attendees")
            events.append({
                "summary": str(item.get("summary") or ""),
                "start": str(start.get("dateTime") or start.get("date") or ""),
                "end": str(end.get("dateTime") or end.get("date") or ""),
                "html_link": str(item.get("htmlLink") or ""),
                "attendees_count": len(attendees) if isinstance(attendees, list) else 0,
            })
        return events
