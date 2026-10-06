#!/usr/bin/env python3
"""Generic chat webhook connector (Feishu, DingTalk, Teams, Discord, other).
The webhook URL is the secret, so no error path may include it."""
import i18n
from connectors import register
from connectors.base import Connector, Field, NOTIFY, TEST_TIMEOUT_SECONDS
from connectors.slack import _post_json, describe_post_error

_PAYLOADS = {
    "feishu": lambda t: {"msg_type": "text", "content": {"text": t}},
    "dingtalk": lambda t: {"msgtype": "text", "text": {"content": t}},
    "teams": lambda t: {"text": t},
    "discord": lambda t: {"content": t},
    "generic": lambda t: {"text": t},
}


@register
class WebhookConnector(Connector):
    type = "webhook"
    label = "Chat webhook"
    icon = "webhook"
    capabilities = frozenset({NOTIFY})
    fields = (
        Field("format", "Payload format", kind="select", default="generic",
              options=tuple(_PAYLOADS), help="Message shape the receiving service expects"),
    )
    secret_label = "Webhook URL"

    def __init__(self, account, secret=None, http=None):
        super().__init__(account, secret=secret, http=http or _post_json)

    def send(self, text, blocks=None):
        """`blocks` (Slack Block Kit) is accepted for notify.py's uniform
        send(text, blocks=...) call and ignored - these services take text."""
        fmt = self.settings.get("format") or "generic"
        payload = _PAYLOADS.get(fmt, _PAYLOADS["generic"])(text)
        return self.http("POST", self.secret, json_body=payload, timeout=TEST_TIMEOUT_SECONDS)

    def test(self):
        try:
            self.send("Loop X connector test ✅")
            return True, i18n.t("Test message sent")
        except Exception as exc:  # noqa: BLE001 - test() must never raise
            return False, describe_post_error(exc)
