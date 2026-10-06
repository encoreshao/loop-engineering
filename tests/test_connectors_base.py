import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from connectors import base
import mail_http


class Demo(base.Connector):
    type = "demo"
    label = "Demo"
    icon = "hub"
    capabilities = frozenset({base.ISSUES})
    fields = (base.Field("url", "URL", kind="url"), base.Field("team", "Team", required=False))
    secret_label = "Token"


def test_validate_requires_required_fields():
    assert Demo.validate({"url": ""}, "t", is_new=True) == ["URL is required"]


def test_validate_rejects_non_http_url():
    assert Demo.validate({"url": "file:///etc/passwd"}, "t", is_new=True) == ["URL must start with http:// or https://"]


def test_validate_requires_secret_only_when_new():
    assert Demo.validate({"url": "https://x"}, "", is_new=True) == ["Token is required"]
    assert Demo.validate({"url": "https://x"}, "", is_new=False) == []


def test_validate_select_must_be_an_option():
    class S(Demo):
        fields = (base.Field("format", "Format", kind="select", options=("a", "b")),)
    assert S.validate({"format": "c"}, "t", is_new=True) == ["Format must be one of: a, b"]


def test_base_test_not_implemented():
    ok, msg = Demo({"id": "d", "settings": {}}).test()
    assert ok is False and "not supported" in msg


@pytest.mark.xfail(strict=True, reason="types land in Task 4")
def test_registry_lists_all_shipped_types():
    import connectors
    connectors.get_type  # noqa: B018
    connectors._load_all()
    assert set(connectors.CONNECTOR_TYPES) == {"gitlab", "github", "slack", "webhook", "rss", "jira", "linear", "mailbox"}


def test_describe_http_error_branches():
    assert base.describe_http_error(mail_http.AuthExpired(401, "", "u")) == "Token rejected (401) - check it hasn't expired"
    assert base.describe_http_error(mail_http.MailHTTPError(500, "boom", "u")) == "HTTP 500: boom"
    assert base.describe_http_error(mail_http.MailHTTPError(500, "x" * 500, "u")) == "HTTP 500: " + "x" * 200
    assert base.describe_http_error(mail_http.MailHTTPError(None, "timed out", "u")) == "Network error: timed out"
    assert base.describe_http_error(ValueError("x")) == "ValueError: x"
