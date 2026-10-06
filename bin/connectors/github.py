#!/usr/bin/env python3
"""GitHub connector (github.com or GitHub Enterprise)."""
import i18n
from connectors import register
from connectors.base import (
    Connector, Field, ISSUES, MERGE_REQUESTS, PIPELINES, TEST_TIMEOUT_SECONDS,
    describe_http_error,
)


@register
class GitHubConnector(Connector):
    type = "github"
    label = "GitHub"
    icon = "code"
    capabilities = frozenset({ISSUES, MERGE_REQUESTS, PIPELINES})
    fields = (
        Field("api_url", "API URL", kind="url", default="https://api.github.com"),
        Field("username", "Username", required=False),
    )
    secret_label = "Personal access token"

    def api(self, method, path, timeout=30, **kw):
        base = self.settings["api_url"].rstrip("/")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        return self.http(method, f"{base}{path}", token=self.secret, headers=headers,
                         timeout=timeout, **kw)

    def test(self):
        try:
            me = self.api("GET", "/user", timeout=TEST_TIMEOUT_SECONDS)
            return True, i18n.t("Connected as {name}", name=me.get("login", "?"))
        except Exception as exc:  # noqa: BLE001 - test() must never raise
            return False, describe_http_error(exc)
