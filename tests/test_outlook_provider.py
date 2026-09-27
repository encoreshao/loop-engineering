import inspect
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from mail_providers import base, outlook  # noqa: E402
from mail_stub import StubServer  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
V = "/v1.0/me"


def _raw(msg_id, sender="alice@x.com", name="Alice", when="2026-09-27T07:00:00Z", conv="c1", body="Hi", content_type="text", has_att=False, is_draft=False):
    raw = {"id": msg_id, "conversationId": conv, "subject": f"S {msg_id}", "receivedDateTime": when,
           "toRecipients": [{"emailAddress": {"name": "Me", "address": "me@example.com"}}], "ccRecipients": [],
           "body": {"contentType": content_type, "content": body}, "hasAttachments": has_att,
           "internetMessageId": f"<{msg_id}@x.com>", "isDraft": is_draft}
    if sender:
        raw["from"] = {"emailAddress": {"name": name, "address": sender}}
    return raw


def _provider(stub):
    return outlook.OutlookProvider("tok", base_url=stub.base_url + "/v1.0", account="me@example.com")


def test_parse_graph_message_basic_and_missing_from():
    parsed = outlook.parse_graph_message(_raw("m1"))
    assert parsed["from"] == "Alice <alice@x.com>"
    assert parsed["to"] == "Me <me@example.com>"
    assert parsed["date"] == "2026-09-27T07:00:00+00:00"
    assert parsed["body_text"] == "Hi"
    assert outlook.parse_graph_message(_raw("m2", sender=None))["from"] == ""


def test_parse_graph_message_html_body_is_converted():
    parsed = outlook.parse_graph_message(_raw("m1", body="<p>Hi <i>you</i></p>", content_type="html"))
    assert parsed["body_text"] == "Hi you"


def test_profile_address_prefers_mail_then_upn():
    with StubServer() as stub:
        stub.add("GET", V, body={"mail": None, "userPrincipalName": "Me@Example.com"})
        assert _provider(stub).profile_address() == "me@example.com"


def test_fetch_new_filter_prefer_header_and_prior_thread():
    since = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
    with StubServer() as stub:
        stub.add("GET", f"{V}/mailFolders/inbox/messages", body={"value": [
            _raw("m1", when="2026-09-27T07:00:00Z"), _raw("seen"), _raw("bot", sender="x@spam.com"),
            _raw("m2", when="2026-09-27T08:00:00Z", conv="c2", has_att=True)]})
        stub.add("GET", f"{V}/messages/m2/attachments", body={"value": [{"name": "plan.pdf"}]})
        stub.add("GET", f"{V}/messages", body={"value": [
            _raw("old", when="2026-09-26T07:00:00Z", body="earlier"), _raw("m1", when="2026-09-27T07:00:00Z")]})
        messages = _provider(stub).fetch_new(since, {"seen"}, ["@spam.com"], limit=10)
        list_req = next(r for r in stub.requests if r["path"] == f"{V}/mailFolders/inbox/messages")
    assert [m["id"] for m in messages] == ["m1", "m2"]
    assert messages[1]["attachments"] == ["plan.pdf"]
    assert messages[0]["prior_thread"][0]["body_text"] == "earlier"
    assert "isRead eq false" in list_req["query"]["$filter"][0]
    assert "receivedDateTime ge 2026-09-25T00:00:00Z" in list_req["query"]["$filter"][0]
    # Graph requires the $orderby property to also be the leading clause of
    # $filter, in the same order, or it rejects the request with a 400
    # InefficientFilter - receivedDateTime must come first.
    assert list_req["query"]["$filter"][0].startswith("receivedDateTime ge ")
    assert 'outlook.body-content-type="text"' in list_req["headers"].get("Prefer", "")


def test_fetch_new_follows_odata_next_link_past_an_all_excluded_page():
    """Controller review: if the first page is entirely seen/excluded
    senders, fetch_new must follow @odata.nextLink rather than returning
    nothing and silently stalling forever."""
    since = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
    with StubServer() as stub:
        stub.add("GET", f"{V}/mailFolders/inbox/messages", body={
            "value": [_raw("bot1", sender="x@spam.com"), _raw("bot2", sender="y@spam.com")],
            "@odata.nextLink": stub.base_url + "/v1.0/next-page",
        })
        stub.add("GET", "/v1.0/next-page", body={"value": [_raw("m1", when="2026-09-27T07:00:00Z")]})
        stub.add("GET", f"{V}/messages", body={"value": []})
        messages = _provider(stub).fetch_new(since, set(), ["@spam.com"], limit=1)
        next_page_req = next(r for r in stub.requests if r["path"] == "/v1.0/next-page")
    assert [m["id"] for m in messages] == ["m1"]
    assert 'outlook.body-content-type="text"' in next_page_req["headers"].get("Prefer", "")


def test_fetch_new_returns_oldest_limit_when_more_qualify():
    since = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
    with StubServer() as stub:
        stub.add("GET", f"{V}/mailFolders/inbox/messages", body={"value": [
            _raw("m1", when="2026-09-27T06:00:00Z"),
            _raw("m2", when="2026-09-27T07:00:00Z"),
            _raw("m3", when="2026-09-27T08:00:00Z"),
        ]})
        stub.add("GET", f"{V}/messages", body={"value": []})
        messages = _provider(stub).fetch_new(since, set(), [], limit=2)
    assert [m["id"] for m in messages] == ["m1", "m2"]


def test_prior_thread_skips_draft_messages():
    """Controller review: the loop's own earlier reply draft in a thread must
    not be fed back to the AI as prior-thread context."""
    since = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
    with StubServer() as stub:
        stub.add("GET", f"{V}/mailFolders/inbox/messages", body={"value": [
            _raw("c", when="2026-09-27T09:00:00Z", conv="t")]})
        stub.add("GET", f"{V}/messages", body={"value": [
            _raw("a", when="2026-09-27T07:00:00Z", conv="t", body="first"),
            _raw("draftmsg", when="2026-09-27T07:30:00Z", conv="t", body="draft reply", is_draft=True),
            _raw("b", when="2026-09-27T08:00:00Z", conv="t", body="second"),
            _raw("c", when="2026-09-27T09:00:00Z", conv="t")]})
        messages = _provider(stub).fetch_new(since, set(), [], limit=1)
    assert [p["body_text"] for p in messages[0]["prior_thread"]] == ["first", "second"]


def test_ensure_labels_creates_missing_master_categories():
    with StubServer() as stub:
        stub.add("GET", f"{V}/outlook/masterCategories", body={"value": [{"displayName": "Loop/Urgent"}]})
        stub.add("POST", f"{V}/outlook/masterCategories", body={"displayName": "Loop/FYI"})
        mapping = _provider(stub).ensure_labels(["Loop/Urgent", "Loop/FYI"])
        created = [json.loads(b)["displayName"] for b in stub.bodies("POST", f"{V}/outlook/masterCategories")]
    assert mapping == {"Loop/Urgent": "Loop/Urgent", "Loop/FYI": "Loop/FYI"}
    assert created == ["Loop/FYI"]


def test_apply_label_appends_and_keeps_existing_categories():
    with StubServer() as stub:
        stub.add("GET", f"{V}/messages/m1", body={"categories": ["Red category"]})
        stub.add("PATCH", f"{V}/messages/m1", body={})
        _provider(stub).apply_label("m1", "Loop/FYI")
        patched = json.loads(stub.bodies("PATCH", f"{V}/messages/m1")[0])
    assert patched == {"categories": ["Red category", "Loop/FYI"]}


def test_apply_label_already_present_is_noop():
    with StubServer() as stub:
        stub.add("GET", f"{V}/messages/m1", body={"categories": ["Loop/FYI"]})
        _provider(stub).apply_label("m1", "Loop/FYI")
        assert stub.bodies("PATCH", f"{V}/messages/m1") == []


def test_create_reply_draft_uses_create_reply_comment():
    with StubServer() as stub:
        stub.add("POST", f"{V}/messages/m1/createReply", body={"id": "d1", "webLink": "https://outlook.office.com/owa/?ItemID=d1"})
        link = _provider(stub).create_reply_draft(outlook.parse_graph_message(_raw("m1")), "Will do.")
        sent = json.loads(stub.bodies("POST", f"{V}/messages/m1/createReply")[0])
    assert sent == {"comment": "Will do."}
    assert link == "https://outlook.office.com/owa/?ItemID=d1"


def test_outlook_source_has_no_send_and_only_interface_methods():
    source = (REPO_ROOT / "bin" / "mail_providers" / "outlook.py").read_text()
    assert not re.search(r"(?i)\bsend(mail)?\b|/send", source)
    public = {n for n, _ in inspect.getmembers(outlook.OutlookProvider, inspect.isfunction) if not n.startswith("_")}
    assert public == base.PUBLIC_METHODS
