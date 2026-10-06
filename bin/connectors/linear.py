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
    brand = "linear"
    category = "tracking"
    description = "Read issues from Linear with a personal API key."
    docs_url = "https://linear.app/docs/api-and-webhooks"

    def api(self, query, variables=None, timeout=30, **kw):
        body = {"query": query}
        if variables:
            body["variables"] = variables
        return self.http("POST", API_URL, json_body=body,
                         headers={"Authorization": self.secret or ""}, timeout=timeout, **kw)

    def test(self):
        try:
            data = self.api("{ viewer { id name } }", timeout=TEST_TIMEOUT_SECONDS, max_attempts=1)
            if data.get("errors"):
                first = data["errors"][0]
                detail = first.get("message", "?") if isinstance(first, dict) else first
                return False, i18n.t("Linear returned an error: {detail}", detail=detail)
            viewer = (data.get("data") or {}).get("viewer") or {}
            return True, i18n.t("Connected as {name}", name=viewer.get("name", "?"))
        except Exception as exc:  # noqa: BLE001 - test() must never raise
            return False, describe_http_error(exc)
