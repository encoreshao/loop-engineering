#!/usr/bin/env python3
"""Notion connector (internal integration token)."""
import i18n
from connectors import register
from connectors.base import Connector, DOCS, TEST_TIMEOUT_SECONDS, describe_http_error

API_URL = "https://api.notion.com/v1"
API_VERSION = "2022-06-28"


@register
class NotionConnector(Connector):
    type = "notion"
    label = "Notion"
    icon = "description"
    capabilities = frozenset({DOCS})
    fields = ()
    secret_label = "Integration token"
    brand = "notion"
    category = "knowledge"
    description = "Read pages and databases shared with a Notion integration."
    docs_url = "https://developers.notion.com/docs/create-a-notion-integration"

    def api(self, method, path, timeout=30, **kw):
        return self.http(method, f"{API_URL}{path}", token=self.secret,
                         headers={"Notion-Version": API_VERSION}, timeout=timeout, **kw)

    def test(self):
        try:
            me = self.api("GET", "/users/me", timeout=TEST_TIMEOUT_SECONDS, max_attempts=1)
            return True, i18n.t("Connected as {name}", name=me.get("name", "?"))
        except Exception as exc:  # noqa: BLE001 - test() must never raise
            return False, describe_http_error(exc)
