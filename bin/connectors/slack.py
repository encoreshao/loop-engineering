#!/usr/bin/env python3
"""Slack incoming-webhook connector. The webhook URL is the secret, so no
error path here may ever include it."""
import http.client
import json
import urllib.error
import urllib.request

import i18n
import mail_http
from connectors import register
from connectors.base import Connector, NOTIFY, TEST_TIMEOUT_SECONDS

_REDACTED = "<redacted>"


def _post_json(method, url, json_body=None, timeout=10, **kw):
    """POST JSON to a webhook. Any 2xx is success (Slack replies with plain
    text `ok`, which request_json would choke on); the body is ignored.
    Raises MailHTTPError carrying a redacted URL - never the real one."""
    if not str(url).lower().startswith(("http://", "https://")):
        raise mail_http.MailHTTPError(None, "unsupported URL scheme", _REDACTED)
    data = json.dumps(json_body if json_body is not None else {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", "User-Agent": "LoopX/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            if not 200 <= status < 300:
                raise mail_http.MailHTTPError(status, "", _REDACTED)
            return None
    except urllib.error.HTTPError as exc:
        try:
            excerpt = exc.read(200).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            excerpt = ""
        raise mail_http.MailHTTPError(exc.code, excerpt, _REDACTED) from None
    except (urllib.error.URLError, OSError):
        raise mail_http.MailHTTPError(None, "network error", _REDACTED) from None
    except (ValueError, http.client.HTTPException):
        raise mail_http.MailHTTPError(None, "invalid URL", _REDACTED) from None


def describe_post_error(exc):
    """Translated failure text that never echoes the webhook URL."""
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return i18n.t("HTTP {status}", status=status)
    return i18n.t("Could not reach the webhook")


@register
class SlackConnector(Connector):
    type = "slack"
    label = "Slack"
    icon = "forum"
    capabilities = frozenset({NOTIFY})
    fields = ()
    secret_label = "Webhook URL"
    brand = "slack"
    category = "chat"
    description = "Post notifications to a Slack channel through an incoming webhook."
    docs_url = "https://api.slack.com/messaging/webhooks"

    def __init__(self, account, secret=None, http=None):
        super().__init__(account, secret=secret, http=http or _post_json)

    def send(self, text, blocks=None):
        body = {"text": text}
        if blocks:
            body["blocks"] = blocks
        return self.http("POST", self.secret, json_body=body, timeout=TEST_TIMEOUT_SECONDS)

    def test(self):
        try:
            self.send("Loop X connector test ✅")
            return True, i18n.t("Test message sent")
        except Exception as exc:  # noqa: BLE001 - test() must never raise
            return False, describe_post_error(exc)
