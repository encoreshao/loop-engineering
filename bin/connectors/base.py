#!/usr/bin/env python3
"""Connector base class. A Connector type declares its form fields,
capabilities and a cheap `test()` probe; instances wrap one account (dict from
connectors_config) plus its secret. User-facing messages are built with
i18n.t from literal templates so they are translated per request."""
import i18n
from dataclasses import dataclass

ISSUES = "issues"
MERGE_REQUESTS = "merge_requests"
PIPELINES = "pipelines"
NOTIFY = "notify"
FEED = "feed"
MAIL = "mail"

TEST_TIMEOUT_SECONDS = 10


class ConnectorError(Exception):
    pass


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str = "text"
    required: bool = True
    default: str = ""
    help: str = ""
    options: tuple = ()


class Connector:
    type = ""
    label = ""
    icon = "hub"
    capabilities = frozenset()
    fields = ()
    secret_label = None
    external = False

    def __init__(self, account, secret=None, http=None):
        self.account = account
        self.settings = account.get("settings", {})
        self.secret = secret
        if http is None:
            import mail_http
            http = mail_http.request_json
        self.http = http

    @classmethod
    def validate(cls, settings, secret, is_new):
        errors = []
        for field in cls.fields:
            value = (settings.get(field.key) or "").strip()
            name = i18n.t(field.label)
            if field.required and not value:
                errors.append(i18n.t("{field} is required", field=name))
                continue
            if value and field.kind == "url" and not value.startswith(("http://", "https://")):
                errors.append(i18n.t("{field} must start with http:// or https://", field=name))
            if value and field.kind == "select" and value not in field.options:
                errors.append(i18n.t("{field} must be one of: {options}",
                                     field=name, options=", ".join(field.options)))
        if cls.secret_label and is_new and not (secret or "").strip():
            errors.append(i18n.t("{field} is required", field=i18n.t(cls.secret_label)))
        return errors

    def test(self):
        return False, i18n.t("Testing is not supported for {name}", name=i18n.t(self.label or self.type))


def describe_http_error(exc):
    """One-line, translated description of a failed probe request."""
    import mail_http
    if isinstance(exc, mail_http.AuthExpired):
        return i18n.t("Token rejected (401) - check it hasn't expired")
    if isinstance(exc, mail_http.MailHTTPError):
        if exc.status is None:
            return i18n.t("Network error: {detail}", detail=exc.body)
        return i18n.t("HTTP {status}: {body}", status=exc.status, body=str(exc.body)[:200])
    return i18n.t("{kind}: {detail}", kind=type(exc).__name__, detail=exc)
