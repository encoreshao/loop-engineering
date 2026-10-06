# tests/test_connectors_types.py
import sys

import pytest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import connectors


class FakeHTTP:
    def __init__(self, response=None, exc=None):
        self.calls = []; self.kw_seen = {}; self.response = response if response is not None else {}; self.exc = exc
    def __call__(self, method, url, token=None, json_body=None, headers=None, timeout=30, **kw):
        self.kw_seen = kw
        self.calls.append({"method": method, "url": url, "token": token, "json": json_body, "headers": headers or {}, "timeout": timeout})
        if self.exc: raise self.exc
        return self.response


def make(type_, settings, secret="s", response=None, exc=None):
    http = FakeHTTP(response, exc)
    cls = connectors.get_type(type_)
    return cls({"id": "x", "type": type_, "settings": settings}, secret=secret, http=http), http


def test_gitlab_test_calls_user_endpoint_with_private_token_header():
    c, http = make("gitlab", {"url": "https://gl.example/"}, response={"username": "enc"})
    assert c.test() == (True, "Connected as enc")
    call = http.calls[0]
    assert call["url"] == "https://gl.example/api/v4/user"
    assert call["headers"]["PRIVATE-TOKEN"] == "s" and call["token"] is None
    assert call["timeout"] == 10


def test_github_test_uses_bearer():
    c, http = make("github", {"api_url": "https://api.github.com", "username": "u"}, response={"login": "u"})
    assert c.test() == (True, "Connected as u")
    assert http.calls[0]["token"] == "s" and http.calls[0]["url"] == "https://api.github.com/user"


def test_jira_uses_basic_auth():
    import base64
    c, http = make("jira", {"site_url": "https://acme.atlassian.net", "email": "a@b.co"}, response={"displayName": "A"})
    assert c.test()[0]
    assert http.calls[0]["headers"]["Authorization"] == "Basic " + base64.b64encode(b"a@b.co:s").decode()


def test_linear_graphql_viewer():
    c, http = make("linear", {}, response={"data": {"viewer": {"name": "A"}}})
    assert c.test() == (True, "Connected as A")
    assert http.calls[0]["headers"]["Authorization"] == "s"   # Linear API keys are sent raw, not Bearer
    assert "viewer" in http.calls[0]["json"]["query"]


def test_slack_test_posts_message_to_secret_url():
    c, http = make("slack", {}, secret="https://hooks.slack.com/services/T/B/X")
    assert c.test()[0]
    assert http.calls[0]["url"] == "https://hooks.slack.com/services/T/B/X"


def test_webhook_feishu_payload_shape():
    c, http = make("webhook", {"format": "feishu"}, secret="https://open.feishu.cn/hook/abc")
    c.send("hello")
    assert http.calls[0]["json"] == {"msg_type": "text", "content": {"text": "hello"}}


def test_webhook_dingtalk_teams_discord_shapes():
    for fmt, expected in [("dingtalk", {"msgtype": "text", "text": {"content": "hi"}}),
                          ("teams", {"text": "hi"}), ("discord", {"content": "hi"}), ("generic", {"text": "hi"})]:
        c, http = make("webhook", {"format": fmt}, secret="https://example.com/h")
        c.send("hi")
        assert http.calls[0]["json"] == expected, fmt


def test_test_never_raises_on_http_error():
    import mail_http
    c, _ = make("github", {"api_url": "https://api.github.com", "username": "u"}, exc=mail_http.MailHTTPError(500, "boom", "https://api.github.com/user"))
    ok, msg = c.test()
    assert ok is False and "500" in msg


def test_auth_expired_message():
    import mail_http
    c, _ = make("gitlab", {"url": "https://gl"}, exc=mail_http.AuthExpired(401, "", "https://gl/api/v4/user"))
    assert c.test() == (False, "Token rejected (401) - check it hasn't expired")


def test_rss_counts_entries(monkeypatch):
    import connectors.rss as rss
    xml = b"<rss><channel><item><title>a</title></item><item><title>b</title></item></channel></rss>"
    monkeypatch.setattr(rss, "_fetch", lambda url, timeout: xml)
    c = connectors.get_type("rss")({"id": "r", "settings": {"feeds": "https://a/feed\nhttps://b/feed"}})
    assert c.test() == (True, "2 feeds, 4 entries")
    assert [e["title"] for e in c.entries("https://a/feed")] == ["a", "b"]


def test_rss_atom_supported(monkeypatch):
    import connectors.rss as rss
    xml = b"<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>t</title><link href='https://x/1'/><id>1</id></entry></feed>"
    monkeypatch.setattr(rss, "_fetch", lambda url, timeout: xml)
    c = connectors.get_type("rss")({"id": "r", "settings": {"feeds": "https://a"}})
    assert c.entries("https://a") == [{"title": "t", "link": "https://x/1", "id": "1", "published": ""}]


def test_rss_rejects_doctype_entities(monkeypatch):
    import connectors.rss as rss
    monkeypatch.setattr(rss, "_fetch", lambda url, timeout: b"<!DOCTYPE x [<!ENTITY a 'b'>]><rss/>")
    c = connectors.get_type("rss")({"id": "r", "settings": {"feeds": "https://a"}})
    ok, msg = c.test()
    assert not ok and "DOCTYPE" in msg


def test_slack_and_webhook_errors_never_leak_the_secret_url():
    import mail_http
    url = "https://hooks.slack.com/services/T/SECRET/TOKEN"
    for type_, settings in (("slack", {}), ("webhook", {"format": "generic"})):
        for exc in (mail_http.MailHTTPError(404, "no", "<redacted>"),
                    mail_http.MailHTTPError(None, "boom", "<redacted>"),
                    RuntimeError("failed posting to " + url)):
            c, _ = make(type_, settings, secret=url, exc=exc)
            ok, msg = c.test()
            assert ok is False and "SECRET" not in msg and url not in msg, (type_, msg)


def test_post_json_redacts_url_and_accepts_plain_ok(monkeypatch):
    import io
    import urllib.error
    import mail_http
    import connectors.slack as slack
    url = "https://hooks.example/SECRETPATH"

    class Resp(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(slack.urllib.request, "urlopen", lambda req, timeout=None: Resp(b"ok"))
    assert slack._post_json("POST", url, json_body={"text": "x"}, timeout=3) is None

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(url, 500, "err", {}, io.BytesIO(b"bad"))
    monkeypatch.setattr(slack.urllib.request, "urlopen", boom)
    with pytest.raises(mail_http.MailHTTPError) as ei:
        slack._post_json("POST", url, json_body={"text": "x"})
    assert ei.value.status == 500 and "SECRETPATH" not in str(ei.value) and "SECRETPATH" not in ei.value.url

    def neterr(req, timeout=None):
        raise urllib.error.URLError("dns " + url)
    monkeypatch.setattr(slack.urllib.request, "urlopen", neterr)
    with pytest.raises(mail_http.MailHTTPError) as ei:
        slack._post_json("POST", url, json_body={"text": "x"})
    assert ei.value.status is None and "SECRETPATH" not in str(ei.value.body)


def test_rss_only_http_urls(monkeypatch):
    import connectors.rss as rss
    with pytest.raises(connectors.base.ConnectorError):
        rss._fetch("file:///etc/passwd", 5)


def test_rss_fetch_reads_at_most_2mb(monkeypatch):
    import io
    import connectors.rss as rss

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(rss.urllib.request, "urlopen", lambda req, timeout=None: Resp(b"a" * (3 * 1024 * 1024)))
    assert len(rss._fetch("https://a/feed", 5)) == 2 * 1024 * 1024


def test_mailbox_test_uses_keychain(monkeypatch):
    import mail_auth
    cls = connectors.get_type("mailbox")
    monkeypatch.setattr(mail_auth, "keychain_get", lambda account_id, service=None: "tok")
    assert cls({"id": "m1", "settings": {}}).test() == (True, "Authorized")
    monkeypatch.setattr(mail_auth, "keychain_get", lambda account_id, service=None: None)
    ok, msg = cls({"id": "m1", "settings": {}}).test()
    assert ok is False and msg.startswith("Not authorized")

    def bad(account_id, service=None):
        raise mail_auth.KeychainError("nope")
    monkeypatch.setattr(mail_auth, "keychain_get", bad)
    assert cls({"id": "m1", "settings": {}}).test()[0] is False


def test_jira_and_gitlab_api_helpers_prefix_urls():
    c, http = make("jira", {"site_url": "https://acme.atlassian.net/", "email": "a@b.co"})
    c.api("GET", "/rest/api/3/myself")
    assert http.calls[0]["url"] == "https://acme.atlassian.net/rest/api/3/myself"
    c, http = make("github", {"api_url": "https://api.github.com/", "username": "u"})
    c.api("GET", "/user")
    assert http.calls[0]["url"] == "https://api.github.com/user"
    assert http.calls[0]["headers"]["Accept"] == "application/vnd.github+json"


def test_rss_rejects_utf16_doctype_entities(monkeypatch):
    import connectors.rss as rss
    body = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "pwned">]><rss><channel><item><title>&a;</title></item></channel></rss>'.encode("utf-16")
    monkeypatch.setattr(rss, "_fetch", lambda url, timeout: body)
    c = connectors.get_type("rss")({"id": "r", "settings": {"feeds": "https://a"}})
    ok, msg = c.test()
    assert not ok and "DOCTYPE" in msg
    with pytest.raises(connectors.base.ConnectorError):
        c.entries("https://a")


def test_send_with_malformed_url_is_redacted():
    import mail_http
    import connectors.slack as slack
    url = "https://hooks.example/SECRET PATH"
    c = connectors.get_type("slack")({"id": "s", "settings": {}}, secret=url)
    with pytest.raises(mail_http.MailHTTPError) as ei:
        c.send("hi")
    e = ei.value
    assert "SECRET" not in str(e) and "SECRET" not in str(e.body) and "SECRET" not in e.url


def test_probes_do_not_retry():
    for type_, settings, resp in (("gitlab", {"url": "https://g"}, {}), ("github", {"api_url": "https://g"}, {}),
                                  ("jira", {"site_url": "https://g", "email": "a@b.c"}, {}), ("linear", {}, {})):
        c, http = make(type_, settings, response=resp)
        c.test()
        assert http.calls[0]["timeout"] == 10
        assert http.kw_seen.get("max_attempts") == 1, type_


def test_linear_errors_list_is_failure():
    c, _ = make("linear", {}, response={"errors": [{"message": "bad key"}], "data": None})
    ok, msg = c.test()
    assert ok is False and "bad key" in msg
