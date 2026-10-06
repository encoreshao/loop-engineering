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


def test_registry_lists_all_shipped_types():
    import connectors
    connectors.get_type  # noqa: B018
    connectors._load_all()
    assert set(connectors.CONNECTOR_TYPES) == {"gitlab", "github", "slack", "webhook", "rss", "jira", "linear", "mailbox", "notion", "telegram", "google_calendar"}


def test_describe_http_error_branches():
    assert base.describe_http_error(mail_http.AuthExpired(401, "", "u")) == "Token rejected (401) - check it hasn't expired"
    assert base.describe_http_error(mail_http.MailHTTPError(500, "boom", "u")) == "HTTP 500: boom"
    assert base.describe_http_error(mail_http.MailHTTPError(500, "x" * 500, "u")) == "HTTP 500: " + "x" * 200
    assert base.describe_http_error(mail_http.MailHTTPError(None, "timed out", "u")) == "Network error: timed out"
    assert base.describe_http_error(ValueError("x")) == "Request failed (ValueError)"


def test_email_field_validation():
    from connectors import base
    class E(base.Connector):
        type = "e"; label = "E"; fields = (base.Field("email", "Email", kind="email"),)
    assert E.validate({"email": "nope"}, None, is_new=True) == ["Email must be an email address"]
    assert E.validate({"email": "a@b"}, None, is_new=True) == ["Email must be an email address"]
    assert E.validate({"email": "a@b.co"}, None, is_new=True) == []


def test_preset_is_frozen_and_hashable_and_defaults_exist():
    from connectors import base
    p = base.Preset("k", "L", "brand", settings=(("format", "x"),))
    assert hash(p) and dict(p.settings) == {"format": "x"}
    assert base.DOCS == "docs"
    assert base.Field("a", "A").placeholder == ""
    c = base.Connector
    assert (c.brand, c.description, c.docs_url, c.category, c.presets) == ("", "", "", "other", ())
