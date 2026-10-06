#!/usr/bin/env python3
"""Generic chat webhook connector (Feishu, DingTalk, Teams, Discord, other).
The webhook URL is the secret, so no error path may include it."""
import i18n
from connectors import register
from connectors.base import Connector, Field, NOTIFY, Preset, TEST_TIMEOUT_SECONDS
from connectors.slack import _post_json, describe_post_error

_PAYLOADS = {
    "feishu": lambda t: {"msg_type": "text", "content": {"text": t}},
    "dingtalk": lambda t: {"msgtype": "text", "text": {"content": t}},
    "teams": lambda t: {"text": t},
    "discord": lambda t: {"content": t},
    "wecom": lambda t: {"msgtype": "text", "text": {"content": t}},
    "googlechat": lambda t: {"text": t},
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
    brand = "webhook"
    category = "chat"
    description = "Post notifications to Feishu, DingTalk, WeCom, Teams, Discord, Google Chat or any webhook."
    docs_url = ""
    presets = (
        Preset("feishu", "Feishu", "feishu", settings=(("format", "feishu"),),
               docs_url="https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot"),
        Preset("dingtalk", "DingTalk", "dingtalk", settings=(("format", "dingtalk"),),
               docs_url="https://open.dingtalk.com/document/robots/custom-robot-access"),
        Preset("wecom", "WeCom 企业微信", "wecom",
               description="Group bot webhook. Personal WeChat has no bot API.",
               settings=(("format", "wecom"),),
               docs_url="https://developer.work.weixin.qq.com/document/path/91770"),
        Preset("microsoftteams", "Microsoft Teams", "microsoftteams",
               description="Teams Workflows webhooks may require Adaptive Cards.",
               settings=(("format", "teams"),),
               docs_url="https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook"),
        Preset("discord", "Discord", "discord", settings=(("format", "discord"),),
               docs_url="https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks"),
        Preset("googlechat", "Google Chat", "googlechat", settings=(("format", "googlechat"),),
               docs_url="https://developers.google.com/workspace/chat/quickstart/webhooks", category="google"),
        Preset("generic", "Generic webhook", "webhook", settings=(("format", "generic"),)),
    )

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
