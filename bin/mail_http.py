#!/usr/bin/env python3
"""The one HTTP helper every Inbox Triage mail call goes through (OAuth
token endpoints, Gmail, Microsoft Graph): JSON in/out, bearer auth,
exponential backoff on 429/5xx/network errors (honouring Retry-After),
and a distinct AuthExpired on 401 so callers can refresh once."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

BACKOFF_SECONDS = (1, 4, 16)
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRY_AFTER = 60


class MailHTTPError(Exception):
    def __init__(self, status, body, url):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status} from {urllib.parse.urlsplit(url).netloc}{urllib.parse.urlsplit(url).path}")


class AuthExpired(MailHTTPError):
    pass


def _retry_delay(retry_after, attempt):
    try:
        return min(int(retry_after), _MAX_RETRY_AFTER)
    except (TypeError, ValueError):
        return BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]


def request_json(method, url, token=None, json_body=None, form=None, headers=None,
                 sleep=None, max_attempts=3, timeout=30):
    if sleep is None:
        sleep = time.sleep
    all_headers = {"Accept": "application/json"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        all_headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        all_headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        all_headers["Authorization"] = f"Bearer {token}"
    all_headers.update(headers or {})

    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(url, data=data, method=method, headers=all_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code == 401:
                raise AuthExpired(401, body, url) from None
            if exc.code in _RETRY_STATUSES and attempt < max_attempts:
                sleep(_retry_delay(exc.headers.get("Retry-After"), attempt))
                continue
            raise MailHTTPError(exc.code, body, url) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt < max_attempts:
                sleep(_retry_delay(None, attempt))
                continue
            raise MailHTTPError(None, str(getattr(exc, "reason", exc)), url) from None
    raise AssertionError("unreachable")
