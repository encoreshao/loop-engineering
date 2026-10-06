#!/usr/bin/env python3
"""Jira Cloud connector (email + API token, HTTP Basic)."""
import base64

import i18n
from connectors import register
from connectors.base import (
    Connector, Field, ISSUES, TEST_TIMEOUT_SECONDS, describe_http_error,
)


@register
class JiraConnector(Connector):
    type = "jira"
    label = "Jira"
    icon = "task_alt"
    capabilities = frozenset({ISSUES})
    fields = (
        Field("site_url", "Site URL", kind="url"),
        Field("email", "Account email"),
    )
    secret_label = "API token"

    def api(self, method, path, timeout=30, **kw):
        creds = f"{self.settings.get('email', '')}:{self.secret or ''}".encode("utf-8")
        headers = {"Authorization": "Basic " + base64.b64encode(creds).decode("ascii"),
                   "Accept": "application/json"}
        return self.http(method, f"{self.settings['site_url'].rstrip('/')}{path}",
                         headers=headers, timeout=timeout, **kw)

    def test(self):
        try:
            me = self.api("GET", "/rest/api/3/myself", timeout=TEST_TIMEOUT_SECONDS, max_attempts=1)
            return True, i18n.t("Connected as {name}", name=me.get("displayName", "?"))
        except Exception as exc:  # noqa: BLE001 - test() must never raise
            return False, describe_http_error(exc)
