#!/usr/bin/env python3
"""Linear connector (GraphQL, personal API key sent raw)."""
import i18n
from connectors import register
from connectors.base import (
    Connector, ISSUES, TEST_TIMEOUT_SECONDS, describe_http_error,
)

API_URL = "https://api.linear.app/graphql"


@register
class LinearConnector(Connector):
    type = "linear"
    label = "Linear"
    icon = "task_alt"
    capabilities = frozenset({ISSUES})
    fields = ()
    secret_label = "API key"

    def api(self, query, variables=None, timeout=30):
        body = {"query": query}
        if variables:
            body["variables"] = variables
        return self.http("POST", API_URL, json_body=body,
                         headers={"Authorization": self.secret or ""}, timeout=timeout)

    def test(self):
        try:
            data = self.api("{ viewer { id name } }", timeout=TEST_TIMEOUT_SECONDS)
            viewer = (data.get("data") or {}).get("viewer") or {}
            return True, i18n.t("Connected as {name}", name=viewer.get("name", "?"))
        except Exception as exc:  # noqa: BLE001 - test() must never raise
            return False, describe_http_error(exc)
