#!/usr/bin/env python3
"""GitLab connector. External accounts come from ~/.gitlab/config.json
(owned by the gitlab-config skill); this type can also be created natively
for an instance you don't want in that shared file."""
import i18n
from connectors import register
from connectors.base import (
    Connector, Field, ISSUES, MERGE_REQUESTS, PIPELINES, TEST_TIMEOUT_SECONDS,
    describe_http_error,
)


@register
class GitLabConnector(Connector):
    type = "gitlab"
    label = "GitLab"
    icon = "code"
    capabilities = frozenset({ISSUES, MERGE_REQUESTS, PIPELINES})
    fields = (Field("url", "Instance URL", kind="url", default="https://gitlab.com"),)
    secret_label = "Personal access token"

    def base_url(self):
        return self.settings["url"].rstrip("/")

    def api(self, method, path, timeout=30, **kw):
        return self.http(method, f"{self.base_url()}/api/v4{path}",
                         headers={"PRIVATE-TOKEN": self.secret or ""}, timeout=timeout, **kw)

    def test(self):
        try:
            me = self.api("GET", "/user", timeout=TEST_TIMEOUT_SECONDS, max_attempts=1)
            return True, i18n.t("Connected as {name}", name=me.get("username", "?"))
        except Exception as exc:  # noqa: BLE001 - test() must never raise
            return False, describe_http_error(exc)
