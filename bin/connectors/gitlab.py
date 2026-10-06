#!/usr/bin/env python3
"""GitLab connector. External accounts come from ~/.gitlab/config.json
(owned by the gitlab-config skill); this type can also be created natively
for an instance you don't want in that shared file."""
import urllib.error
import urllib.request

import i18n
import mail_http
from connectors import register
from connectors.base import (
    Connector, Field, ISSUES, MERGE_REQUESTS, PIPELINES, TEST_TIMEOUT_SECONDS,
    describe_http_error,
)

_MAX_TEXT_BYTES = 2 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024


@register
class GitLabConnector(Connector):
    type = "gitlab"
    label = "GitLab"
    icon = "code"
    capabilities = frozenset({ISSUES, MERGE_REQUESTS, PIPELINES})
    fields = (Field("url", "Instance URL", kind="url", default="https://gitlab.com",
                placeholder="https://gitlab.com"),)
    secret_label = "Personal access token"
    brand = "gitlab"
    category = "code"
    description = "Read issues, merge requests and pipelines from a GitLab instance."
    docs_url = "https://docs.gitlab.com/ee/user/profile/personal_access_tokens.html"

    def base_url(self):
        return self.settings["url"].rstrip("/")

    def api(self, method, path, timeout=30, **kw):
        return self.http(method, f"{self.base_url()}/api/v4{path}",
                         headers={"PRIVATE-TOKEN": self.secret or ""}, timeout=timeout, **kw)

    def api_text(self, path, timeout=30):
        """GET a plain-text endpoint (e.g. a job trace) and return its LAST
        2 MB (failures sit at the end of a log). Asks for a suffix Range; if
        the server ignores it, streams the body keeping only a rolling tail.
        Errors carry only the status: the URL and token are never put in the
        exception."""
        request = urllib.request.Request(
            f"{self.base_url()}/api/v4{path}", method="GET",
            headers={"PRIVATE-TOKEN": self.secret or "", "Range": f"bytes=-{_MAX_TEXT_BYTES}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                tail = bytearray()
                while True:
                    chunk = response.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    tail += chunk
                    if len(tail) > _MAX_TEXT_BYTES + _CHUNK_BYTES:
                        del tail[:len(tail) - _MAX_TEXT_BYTES]
                return bytes(tail[-_MAX_TEXT_BYTES:]).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 416:  # empty body: nothing to range over
                return ""
            raise mail_http.MailHTTPError(exc.code, "", "") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            raise mail_http.MailHTTPError(None, "", "") from None

    def test(self):
        try:
            me = self.api("GET", "/user", timeout=TEST_TIMEOUT_SECONDS, max_attempts=1)
            return True, i18n.t("Connected as {name}", name=me.get("username", "?"))
        except Exception as exc:  # noqa: BLE001 - test() must never raise
            return False, describe_http_error(exc)
