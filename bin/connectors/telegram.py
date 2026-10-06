#!/usr/bin/env python3
"""Telegram bot connector. The bot token sits in the request URL path, so
every error path scrubs it: whatever the HTTP layer raises is replaced with a
MailHTTPError carrying only a status code and redacted text, and any token
echoed in Telegram's own error description is removed."""
import json

import i18n
import mail_http
from connectors import register
from connectors.base import Connector, Field, NOTIFY, TEST_TIMEOUT_SECONDS

_REDACTED = "<redacted>"


@register
class TelegramConnector(Connector):
    type = "telegram"
    label = "Telegram bot"
    icon = "send"
    capabilities = frozenset({NOTIFY})
    fields = (
        Field("chat_id", "Chat ID", placeholder="-1001234567890",
              help="Chat or channel id the bot posts to"),
    )
    secret_label = "Bot token"
    brand = "telegram"
    category = "chat"
    description = "Post notifications to a Telegram chat or channel through a bot."
    docs_url = "https://core.telegram.org/bots/tutorial#obtain-your-bot-token"

    def _scrub(self, text):
        text = str(text)
        for secret in {self.secret or "", (self.secret or "").split(":")[-1]}:
            if len(secret) >= 4:
                text = text.replace(secret, _REDACTED)
        return text

    def send(self, text, blocks=None, _probe=False):
        """`blocks` is accepted for the uniform send(text, blocks=...) call
        and ignored. Never raises anything that mentions the token. Only the
        connection test uses the short, single-attempt probe settings."""
        url = f"https://api.telegram.org/bot{self.secret}/sendMessage"
        try:
            resp = self.http("POST", url, json_body={"chat_id": self.settings.get("chat_id", ""),
                                                     "text": str(text)[:4096]},
                             **({"timeout": TEST_TIMEOUT_SECONDS, "max_attempts": 1} if _probe else {}))
        except Exception as exc:  # noqa: BLE001 - the exception may quote the URL
            status = getattr(exc, "status", None)
            body = _REDACTED
            if isinstance(exc, mail_http.MailHTTPError) and isinstance(status, int):
                try:
                    desc = json.loads(exc.body).get("description")
                    if isinstance(desc, str) and desc:
                        body = self._scrub(desc)
                except Exception:  # noqa: BLE001 - non-JSON body: keep it redacted
                    pass
            raise mail_http.MailHTTPError(status if isinstance(status, int) else None,
                                          body, _REDACTED) from None
        if isinstance(resp, dict) and resp.get("ok") is False:
            code = resp.get("error_code")
            raise mail_http.MailHTTPError(code if isinstance(code, int) else None,
                                          self._scrub(resp.get("description", "")), _REDACTED)
        return resp

    def test(self):
        try:
            self.send("Loop X connector test ✅", _probe=True)
            return True, i18n.t("Test message sent")
        except mail_http.MailHTTPError as exc:
            detail = exc.body if exc.body != _REDACTED else ""
            if detail:
                return False, i18n.t("Telegram returned an error: {detail}", detail=str(detail)[:200])
            if isinstance(exc.status, int):
                return False, i18n.t("HTTP {status}", status=exc.status)
            return False, i18n.t("Could not reach Telegram")
        except Exception:  # noqa: BLE001 - test() must never raise, nor leak the token
            return False, i18n.t("Could not reach Telegram")
