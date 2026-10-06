#!/usr/bin/env python3
"""Mailbox connector. Accounts are managed on the Inbox Triage page; this
type only reports whether a refresh token is stored for the inbox."""
import i18n
from connectors import register
from connectors.base import Connector, MAIL


@register
class MailboxConnector(Connector):
    type = "mailbox"
    label = "Mailbox"
    icon = "mail"
    capabilities = frozenset({MAIL})
    fields = ()
    secret_label = None
    external = True
    category = "mail"
    description = "Gmail or Outlook inbox managed on the Inbox Triage page."

    def test(self):
        try:
            import mail_auth
            token = mail_auth.keychain_get(self.account["id"])
        except Exception as exc:  # noqa: BLE001 - test() must never raise
            return False, i18n.t("{kind}: {detail}", kind=type(exc).__name__, detail=exc)
        if token is None:
            return False, i18n.t("Not authorized - finish setup on the Inbox Triage page")
        return True, i18n.t("Authorized")
