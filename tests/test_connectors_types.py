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


@pytest.mark.parametrize("type_, settings", [
    ("github", {"api_url": "https://api.github.com", "username": "u"}),
    ("gitlab", {"url": "https://gl.example"}),
])
def test_probe_error_with_secret_in_exception_text_does_not_leak_it(type_, settings):
    secret = "ghp_TOP\nSECRET"
    c, _ = make(type_, settings, secret=secret,
                exc=ValueError(f"Invalid header value b'Bearer {secret}'"))
    ok, msg = c.test()
    assert ok is False
    assert "ghp_TOP" not in msg and "SECRET" not in msg and "Bearer" not in msg
    assert "ValueError" in msg


def test_registry_includes_notion_and_telegram():
    connectors._load_all()
    assert {"notion", "telegram"} <= set(connectors.CONNECTOR_TYPES)


def test_every_type_has_presentation_metadata():
    connectors._load_all()
    for name, cls in connectors.CONNECTOR_TYPES.items():
        assert cls.category in {"google", "code", "chat", "tracking", "knowledge", "feeds", "mail", "other"}, name
        assert cls.description, name
        if not cls.external:
            assert cls.brand, name
            assert cls.docs_url.startswith("https://") or cls.type in ("rss", "webhook"), name


def test_webhook_presets_cover_brands():
    cls = connectors.get_type("webhook")
    keys = {p.key for p in cls.presets}
    assert {"feishu", "dingtalk", "wecom", "microsoftteams", "discord", "googlechat", "generic"} <= keys
    assert all(dict(p.settings)["format"] in cls.fields[0].options for p in cls.presets)
    assert len(set(cls.presets)) == len(cls.presets)  # frozen + hashable


def test_webhook_wecom_and_googlechat_payloads():
    for fmt, expected in [("wecom", {"msgtype": "text", "text": {"content": "hi"}}), ("googlechat", {"text": "hi"})]:
        c, http = make("webhook", {"format": fmt}, secret="https://example.com/h")
        c.send("hi")
        assert http.calls[0]["json"] == expected


def test_notion_test_uses_version_header():
    c, http = make("notion", {}, response={"name": "Loop bot"})
    assert c.test() == (True, "Connected as Loop bot")
    assert http.calls[0]["url"] == "https://api.notion.com/v1/users/me"
    assert http.calls[0]["headers"]["Notion-Version"] == "2022-06-28"
    assert http.calls[0]["token"] == "s"


def test_telegram_send_posts_chat_id():
    c, http = make("telegram", {"chat_id": "-100"}, secret="123:ABC")
    c.send("hi")
    assert http.calls[0]["url"] == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert http.calls[0]["json"] == {"chat_id": "-100", "text": "hi"}


def _token_excs():
    import mail_http
    import urllib.error
    url = "https://api.telegram.org/bot123:ABC/sendMessage"
    return (mail_http.MailHTTPError(401, "bad", url),
            mail_http.MailHTTPError(None, "boom 123:ABC", url),
            urllib.error.URLError(url + " unreachable"),
            ValueError("URL can't contain control characters. '/bot123:ABC /sendMessage'"))


def test_telegram_errors_never_contain_token():
    for exc in _token_excs():
        c, _ = make("telegram", {"chat_id": "-100"}, secret="123:ABC", exc=exc)
        ok, msg = c.test()
        assert not ok and "123:ABC" not in msg and "ABC" not in msg


def test_telegram_send_raises_token_free_exception():
    for exc in _token_excs():
        c, _ = make("telegram", {"chat_id": "-100"}, secret="123:ABC", exc=exc)
        with pytest.raises(Exception) as info:
            c.send("hi")
        e = info.value
        blob = " ".join(str(x) for x in (e, getattr(e, "reason", ""), getattr(e, "url", ""),
                                         getattr(e, "body", ""), e.args))
        assert "ABC" not in blob
        assert e.__cause__ is None and e.__suppress_context__


def test_telegram_scrubs_token_from_api_description():
    c, _ = make("telegram", {"chat_id": "-100"}, secret="123:ABC",
                response={"ok": False, "description": "Unauthorized bot123:ABC"})
    ok, msg = c.test()
    assert not ok and "ABC" not in msg
    c, _ = make("telegram", {"chat_id": "-100"}, secret="123:ABC", response={"ok": True})
    assert c.test()[0]


def test_telegram_surfaces_api_description_without_token():
    import mail_http
    url = "https://api.telegram.org/bot123:ABC/sendMessage"
    for body, want in [('{"ok":false,"description":"Bad Request: chat not found"}', "chat not found"),
                       ('{"ok":false,"description":"bad bot123:ABC token"}', "bad bot")]:
        c, _ = make("telegram", {"chat_id": "-100"}, secret="123:ABC",
                    exc=mail_http.MailHTTPError(400, body, url))
        ok, msg = c.test()
        assert not ok and want in msg and "ABC" not in msg
    c, _ = make("telegram", {"chat_id": "-100"}, secret="123:ABC",
                exc=mail_http.MailHTTPError(502, "<html>ABC</html>", url))
    assert c.test() == (False, "HTTP 502")


def test_telegram_send_uses_default_http_settings_and_truncates():
    c, http = make("telegram", {"chat_id": "-100"}, secret="123:ABC")
    c.send("x" * 5000)
    assert len(http.calls[0]["json"]["text"]) == 4096 and http.kw_seen == {}
    assert http.calls[0]["timeout"] == 30
    c.test()
    assert http.calls[1]["timeout"] == 10 and http.kw_seen == {"max_attempts": 1}


def test_webhook_non_generic_presets_have_https_docs_url():
    cls = connectors.get_type("webhook")
    for p in cls.presets:
        if p.key != "generic":
            assert p.docs_url.startswith("https://"), p.key
    assert "Adaptive Cards" in {p.key: p for p in cls.presets}["microsoftteams"].description


# --- P2c: Google Calendar ---------------------------------------------------

_GCLIENT = {"client_id": "c", "client_secret": "s"}


def _gcal(settings=None, secret="RT", http=None, client=_GCLIENT, refresh_fn=None):
    return connectors.get_type("google_calendar")(
        {"id": "g", "settings": settings if settings is not None else {"calendar_id": "primary"}},
        secret=secret, http=http, client_fn=lambda: client,
        refresh_fn=refresh_fn or (lambda rt, cl: {"access_token": "AT"}))


def test_calendar_type_metadata():
    from connectors.base import CALENDAR
    cls = connectors.get_type("google_calendar")
    assert cls.label == "Google Calendar" and cls.brand == "googlecalendar" and cls.category == "google"
    assert cls.capabilities == frozenset({CALENDAR}) and cls.auth == "oauth_google" and cls.secret_label is None
    assert [f.key for f in cls.fields] == ["calendar_id"] and cls.fields[0].default == "primary"
    assert cls.docs_url == "https://support.google.com/calendar/answer/37082"
    assert connectors.get_type("github").auth == "secret"


def test_calendar_test_success():
    http = FakeHTTP(response={"summary": "Work"})
    c = connectors.get_type("google_calendar")({"id": "g", "settings": {"calendar_id": "primary"}}, secret="RT",
        http=http, client_fn=lambda: {"client_id": "c", "client_secret": "s"},
        refresh_fn=lambda rt, client: {"access_token": "AT"})
    assert c.test() == (True, "Connected: Work")
    assert http.calls[0]["url"] == "https://www.googleapis.com/calendar/v3/calendars/primary"
    assert http.calls[0]["token"] == "AT"
    assert http.calls[0]["timeout"] == 10 and http.kw_seen.get("max_attempts") == 1


def test_calendar_test_accepts_plain_access_token_from_google_refresh():
    http = FakeHTTP(response={"summary": "Work"})
    c = _gcal(http=http, refresh_fn=lambda rt, cl: "AT2")
    assert c.test() == (True, "Connected: Work") and http.calls[0]["token"] == "AT2"


def test_calendar_test_not_connected():
    c = connectors.get_type("google_calendar")({"id": "g", "settings": {"calendar_id": "primary"}}, secret=None,
        client_fn=lambda: {"client_id": "c", "client_secret": "s"})
    assert c.test()[0] is False and "Connect with Google" in c.test()[1]


def test_calendar_test_without_google_client():
    ok, msg = _gcal(client={}).test()
    assert not ok and "Google OAuth client" in msg


def test_calendar_test_errors_never_contain_refresh_token():
    import mail_http
    def bad_refresh(rt, client): raise mail_http.MailHTTPError(400, '{"error":"invalid_grant","rt":"RT-SECRET"}', "https://oauth2.googleapis.com/token")
    c = connectors.get_type("google_calendar")({"id": "g", "settings": {"calendar_id": "primary"}}, secret="RT-SECRET",
        client_fn=lambda: {"client_id": "c", "client_secret": "s"}, refresh_fn=bad_refresh)
    ok, msg = c.test()
    assert not ok and "RT-SECRET" not in msg


def test_calendar_test_errors_never_contain_access_token():
    import mail_http
    http = FakeHTTP(exc=mail_http.MailHTTPError(403, "denied for AT-SECRET", "https://www.googleapis.com/x"))
    ok, msg = _gcal(http=http, refresh_fn=lambda rt, cl: {"access_token": "AT-SECRET"}).test()
    assert not ok and "AT-SECRET" not in msg and "403" in msg


def test_calendar_error_scrubs_token_straddling_the_200_char_cut():
    import mail_http
    rt = "1//0gABCDEFGHIJKLMNOPQRSTUVWXYZ-refresh-token-value"
    body = "x" * 190 + rt
    def bad_refresh(r, c): raise mail_http.MailHTTPError(400, body, "https://oauth2.googleapis.com/token")
    ok, msg = _gcal(secret=rt, refresh_fn=bad_refresh).test()
    assert not ok
    for i in range(len(rt) - 7):
        assert rt[i:i + 8] not in msg
    http = FakeHTTP(exc=mail_http.MailHTTPError(403, "y" * 195 + "AT-LONG-ACCESS-TOKEN-123", "https://x"))
    ok, msg = _gcal(http=http, refresh_fn=lambda r, c: {"access_token": "AT-LONG-ACCESS-TOKEN-123"}).test()
    assert not ok and "AT-LONG-" not in msg and "TOKEN-123" not in msg


def test_calendar_reauth_required_message_is_token_free():
    import mail_auth
    def reauth(rt, cl): raise mail_auth.ReauthRequired("Google refresh failed (invalid_grant) RT-SECRET")
    ok, msg = _gcal(secret="RT-SECRET", refresh_fn=reauth).test()
    assert not ok and "RT-SECRET" not in msg and "Reconnect" in msg


def test_calendar_id_is_url_quoted():
    http = FakeHTTP(response={"summary": "x"})
    c = connectors.get_type("google_calendar")({"id": "g", "settings": {"calendar_id": "a@group.calendar.google.com/../x"}}, secret="RT",
        http=http, client_fn=lambda: {"client_id": "c", "client_secret": "s"}, refresh_fn=lambda rt, cl: {"access_token": "AT"})
    c.test()
    assert http.calls[0]["url"].endswith("/calendars/a%40group.calendar.google.com%2F..%2Fx")


def test_calendar_list_events_shapes_items():
    http = FakeHTTP(response={"items": [
        {"summary": "Standup", "start": {"dateTime": "2026-10-06T09:00:00Z"}, "end": {"dateTime": "2026-10-06T09:15:00Z"},
         "htmlLink": "https://calendar.google.com/e1", "attendees": [{"email": "a"}, {"email": "b"}]},
        {"start": {"date": "2026-10-07"}, "end": {"date": "2026-10-08"}},
    ]})
    events = _gcal(http=http).list_events("2026-10-06T00:00:00Z", "2026-10-08T00:00:00Z", max_results=5)
    base_keys = ("summary", "start", "end", "html_link", "attendees_count")
    assert [{k: e[k] for k in base_keys} for e in events] == [
        {"summary": "Standup", "start": "2026-10-06T09:00:00Z", "end": "2026-10-06T09:15:00Z",
         "html_link": "https://calendar.google.com/e1", "attendees_count": 2},
        {"summary": "", "start": "2026-10-07", "end": "2026-10-08", "html_link": "", "attendees_count": 0},
    ]
    url = http.calls[0]["url"]
    assert url.startswith("https://www.googleapis.com/calendar/v3/calendars/primary/events?")
    import urllib.parse
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert q["timeMin"] == ["2026-10-06T00:00:00Z"] and q["timeMax"] == ["2026-10-08T00:00:00Z"]
    assert q["maxResults"] == ["5"] and q["singleEvents"] == ["true"] and q["orderBy"] == ["startTime"]
    assert http.calls[0]["token"] == "AT"


def test_googlechat_preset_overrides_category_to_google():
    from connectors import webhook
    chat = next(p for p in webhook.WebhookConnector.presets if p.key == "googlechat")
    assert chat.category == "google"
    assert all(p.category == "" for p in webhook.WebhookConnector.presets if p.key != "googlechat")


def test_webhook_preset_descriptions_are_distinct_and_non_empty():
    cls = connectors.get_type("webhook")
    descriptions = [p.description for p in cls.presets]
    assert all(d.strip() for d in descriptions)
    assert len(set(descriptions)) == len(descriptions)
    assert cls.description not in descriptions


class _FakeResp:
    def __init__(self, data, status=200):
        self.data = data; self.pos = 0; self.status = status; self.max_chunk = 0
    def read(self, n=-1):
        n = len(self.data) - self.pos if n is None or n < 0 else n
        self.max_chunk = max(self.max_chunk, n)
        out = self.data[self.pos:self.pos + n]; self.pos += len(out); return out
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_gitlab_api_text_returns_text_with_token_and_range_headers(monkeypatch):
    import urllib.request
    seen = {}
    def fake(req, timeout=None):
        seen["url"] = req.full_url; seen["headers"] = dict(req.header_items()); seen["timeout"] = timeout
        return _FakeResp("boom \u2713\n".encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    c, _ = make("gitlab", {"url": "https://gl.example/"}, secret="tok")
    assert c.api_text("/projects/9/jobs/5/trace", timeout=7) == "boom \u2713\n"
    assert seen["url"] == "https://gl.example/api/v4/projects/9/jobs/5/trace"
    assert seen["headers"]["Private-token"] == "tok" and seen["timeout"] == 7
    assert seen["headers"]["Range"] == "bytes=-2097152"


def test_gitlab_api_text_keeps_tail_when_server_ignores_range(monkeypatch):
    import urllib.request
    body = b"a" * (1024 * 1024) + b"b" * (2 * 1024 * 1024 - 5) + b"THE-END"
    resp = _FakeResp(body)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: resp)
    c, _ = make("gitlab", {"url": "https://gl.example"})
    text = c.api_text("/t")
    assert len(text) <= 2 * 1024 * 1024 and text.endswith("THE-END") and text.startswith("b")
    assert resp.max_chunk <= 64 * 1024


def test_gitlab_api_text_accepts_206_partial(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(b"tail-part", status=206))
    c, _ = make("gitlab", {"url": "https://gl.example"})
    assert c.api_text("/t") == "tail-part"


def test_gitlab_api_text_416_is_empty(monkeypatch):
    import io, urllib.error, urllib.request
    def r416(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 416, "x", {}, io.BytesIO(b""))
    monkeypatch.setattr(urllib.request, "urlopen", r416)
    c, _ = make("gitlab", {"url": "https://gl.example"})
    assert c.api_text("/t") == ""


def test_gitlab_api_text_errors_redact_url_and_token(monkeypatch):
    import io, urllib.error, urllib.request
    import mail_http
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b"secret-body tok"))
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    c, _ = make("gitlab", {"url": "https://gl.example"}, secret="tok")
    with pytest.raises(mail_http.MailHTTPError) as ei:
        c.api_text("/projects/9/jobs/5/trace")
    text = str(ei.value) + repr(ei.value.url) + str(ei.value.body)
    assert ei.value.status == 404 and "tok" not in text and "gl.example" not in text
    def net(req, timeout=None): raise urllib.error.URLError("dns tok")
    monkeypatch.setattr(urllib.request, "urlopen", net)
    with pytest.raises(mail_http.MailHTTPError) as ei:
        c.api_text("/x")
    assert ei.value.status is None and "tok" not in str(ei.value) + str(ei.value.body)


def _sent_text(http):
    body = http.calls[0]["json"]
    return body.get("text", body.get("content"))


def test_test_message_is_plain_and_names_the_connector():
    for type_, settings, secret in (("slack", {}, "https://hooks.slack.com/services/T/B/X"),
                                    ("webhook", {"format": "generic"}, "https://example.com/h"),
                                    ("telegram", {"chat_id": "1"}, "123:abc")):
        http = FakeHTTP({"ok": True})
        cls = connectors.get_type(type_)
        c = cls({"id": "team-alerts", "type": type_, "label": "Team alerts", "settings": settings},
                secret=secret, http=http)
        assert c.test()[0], type_
        text = _sent_text(http)
        assert text == ('Loop X test message from connector "Team alerts" (team-alerts). '
                        "Notifications sent through this connector will appear here."), type_


def test_test_message_shows_id_once_when_label_matches():
    c, http = make("slack", {}, secret="https://hooks.slack.com/services/T/B/X")
    c.test()
    assert http.calls[0]["json"]["text"].startswith('Loop X test message from connector "x". ')


def test_calendar_list_events_returns_prep_details():
    http = FakeHTTP(response={"items": [
        {"id": "ev1", "recurringEventId": "series1", "summary": "Sync",
         "start": {"dateTime": "2026-10-06T09:00:00Z"}, "end": {"dateTime": "2026-10-06T09:30:00Z"},
         "description": "Agenda https://gitlab.example.com/g/p/-/issues/3", "location": "Room 1",
         "conferenceData": {"entryPoints": [{"entryPointType": "phone", "uri": "tel:+1"},
                                            {"entryPointType": "video", "uri": "https://zoom.us/j/1"}]},
         "organizer": {"email": "boss@example.com"},
         "attendees": [{"email": "me@example.com", "self": True, "responseStatus": "accepted"},
                       {"email": "ann@example.com", "displayName": "Ann", "responseStatus": "tentative"}]},
        {"id": "ev2", "summary": "Meet", "hangoutLink": "https://meet.google.com/abc",
         "start": {"dateTime": "2026-10-06T10:00:00Z"}, "end": {"dateTime": "2026-10-06T10:30:00Z"},
         "conferenceData": {"entryPoints": [{"entryPointType": "video", "uri": "https://zoom.us/j/2"}]}},
    ]})
    first, second = _gcal(http=http).list_events("a", "b")
    assert first["id"] == "ev1" and first["recurring_event_id"] == "series1"
    assert first["description"].startswith("Agenda") and first["location"] == "Room 1"
    assert first["join_url"] == "https://zoom.us/j/1" and first["organizer"] == "boss@example.com"
    assert first["attendees"] == [
        {"name": "", "email": "me@example.com", "response": "accepted", "self": True},
        {"name": "Ann", "email": "ann@example.com", "response": "tentative", "self": False}]
    assert first["self_response"] == "accepted"
    assert second["join_url"] == "https://meet.google.com/abc"
    assert second["recurring_event_id"] == "" and second["attendees"] == [] and second["self_response"] is None
