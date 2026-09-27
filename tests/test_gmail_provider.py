import base64
import email
import email.policy
import inspect
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import mail_http  # noqa: E402
import mail_providers  # noqa: E402
from mail_providers import base, gmail  # noqa: E402
from mail_stub import StubServer  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
U = "/gmail/v1/users/me"


def _b64(text, encoding="utf-8"):
    return base64.urlsafe_b64encode(text.encode(encoding)).decode().rstrip("=")


def _raw(msg_id, sender="Alice <alice@x.com>", subject="Hello", body="Hi there", ms=1790000000000,
         thread="t1", mime="text/plain", extra_parts=None, headers=None):
    hdrs = [{"name": "From", "value": sender}, {"name": "To", "value": "me@example.com"},
            {"name": "Subject", "value": subject}, {"name": "Message-ID", "value": f"<{msg_id}@x.com>"}]
    hdrs += headers or []
    parts = [{"mimeType": mime, "body": {"data": _b64(body)}, "headers": []}] + (extra_parts or [])
    return {"id": msg_id, "threadId": thread, "internalDate": str(ms),
            "payload": {"mimeType": "multipart/mixed", "headers": hdrs, "parts": parts}}


def test_parse_gmail_message_plain():
    parsed = gmail.parse_gmail_message(_raw("m1"))
    assert parsed["id"] == "m1" and parsed["thread_id"] == "t1"
    assert parsed["from"] == "Alice <alice@x.com>" and parsed["subject"] == "Hello"
    assert parsed["body_text"] == "Hi there"
    assert parsed["date"] == datetime.fromtimestamp(1790000000, timezone.utc).isoformat()
    assert parsed["_message_id_header"] == "<m1@x.com>"


def test_parse_gmail_message_html_only_and_bad_charset():
    """Review Focus 1."""
    raw = _raw("m2", body="<p>Hello <b>world</b></p>", mime="text/html")
    assert gmail.parse_gmail_message(raw)["body_text"] == "Hello world"
    broken = _raw("m3")
    broken["payload"]["parts"][0]["body"]["data"] = base64.urlsafe_b64encode(b"caf\xe9 \xff").decode()
    broken["payload"]["parts"][0]["headers"] = [{"name": "Content-Type", "value": "text/plain; charset=gb2312"}]
    assert "caf" in gmail.parse_gmail_message(broken)["body_text"]


def test_parse_gmail_message_lists_attachments_and_trims_body():
    att = {"mimeType": "application/pdf", "filename": "invoice.pdf", "body": {"attachmentId": "a1", "size": 10}}
    parsed = gmail.parse_gmail_message(_raw("m4", body="x" * 9000, extra_parts=[att]))
    assert parsed["attachments"] == ["invoice.pdf"]
    assert len(parsed["body_text"]) == 4000


def test_reply_subject_not_doubled():
    """Review Focus 2."""
    assert gmail.reply_subject("Hello") == "Re: Hello"
    assert gmail.reply_subject("RE: Hello") == "RE: Hello"
    assert gmail.reply_subject("re:Hello") == "re:Hello"
    assert gmail.reply_subject("") == "Re:"


def _provider(stub, refresh=None):
    return gmail.GmailProvider("tok", refresh=refresh, base_url=stub.base_url + "/gmail/v1", account="me@example.com")


def test_profile_address():
    with StubServer() as stub:
        stub.add("GET", f"{U}/profile", body={"emailAddress": "Me@Example.com"})
        assert _provider(stub).profile_address() == "me@example.com"


def test_fetch_new_filters_seen_excluded_and_sorts_oldest_first():
    since = datetime(2026, 9, 25, tzinfo=timezone.utc)
    with StubServer() as stub:
        stub.add("GET", f"{U}/messages", body={"messages": [{"id": "new2"}, {"id": "seen"}, {"id": "new1"}, {"id": "spam"}]})
        stub.add("GET", f"{U}/messages/new1", body=_raw("new1", ms=1790000000000, thread="t1"))
        stub.add("GET", f"{U}/messages/new2", body=_raw("new2", ms=1790000500000, thread="t2"))
        stub.add("GET", f"{U}/messages/spam", body=_raw("spam", sender="bot@spam.com", thread="t3"))
        for t, ids in (("t1", ["new1"]), ("t2", ["new2"])):
            stub.add("GET", f"{U}/threads/{t}", body={"messages": [_raw(i, thread=t) for i in ids]})
        messages = _provider(stub).fetch_new(since, {"seen"}, ["@spam.com"], limit=10)
        list_query = next(r["query"] for r in stub.requests if r["path"] == f"{U}/messages")
    assert [m["id"] for m in messages] == ["new1", "new2"]
    # One second early: Gmail's after: is exclusive, so a message in the same
    # second as the high-water mark would otherwise be skipped (seen IDs dedupe).
    assert list_query["q"] == [f"in:inbox is:unread after:{int(since.timestamp()) - 1}"]
    assert not any(r["path"] == f"{U}/messages/seen" for r in stub.requests)


def test_fetch_new_includes_prior_thread_and_respects_limit():
    since = datetime(2026, 9, 25, tzinfo=timezone.utc)
    with StubServer() as stub:
        # messages.list is newest-first; "d" (990000000) is newer than "c" (900000000).
        stub.add("GET", f"{U}/messages", body={"messages": [{"id": "d"}, {"id": "c"}]})
        stub.add("GET", f"{U}/messages/c", body=_raw("c", ms=1790000900000, thread="t"))
        stub.add("GET", f"{U}/messages/d", body=_raw("d", ms=1790000990000, thread="t"))
        thread = [_raw("a", body="first", ms=1790000100000, thread="t"), _raw("b", body="second", ms=1790000200000, thread="t"),
                  _raw("x", body="third", ms=1790000300000, thread="t"), _raw("c", ms=1790000900000, thread="t")]
        stub.add("GET", f"{U}/threads/t", body={"messages": thread})
        messages = _provider(stub).fetch_new(since, set(), [], limit=1)
    assert [m["id"] for m in messages] == ["c"]
    assert [p["body_text"] for p in messages[0]["prior_thread"]] == ["second", "third"]
    assert not any(r["path"] == f"{U}/messages/d" for r in stub.requests)


def test_fetch_new_paginates_all_pages_and_keeps_globally_oldest():
    """Controller review: messages.list returns newest-first. With a backlog
    bigger than the page-1 batch, fetch_new must page through every list
    result (not stop after page 1), then keep the globally oldest `limit`
    messages - never just the newest page's oldest - and must not
    full-fetch messages beyond the ones it keeps."""
    since = datetime(2026, 9, 25, tzinfo=timezone.utc)
    with StubServer() as stub:
        stub.add("GET", f"{U}/messages",
                  body={"messages": [{"id": "f"}, {"id": "e"}, {"id": "d"}, {"id": "g"}], "nextPageToken": "p2"})
        stub.add("GET", f"{U}/messages", body={"messages": [{"id": "b"}, {"id": "a"}]})
        for msg_id, ms in (("f", 1790000690000), ("e", 1790000680000), ("d", 1790000670000),
                           ("g", 1790000660000), ("b", 1790000200000), ("a", 1790000100000)):
            thread = f"t{msg_id}"
            stub.add("GET", f"{U}/messages/{msg_id}", body=_raw(msg_id, ms=ms, thread=thread))
            stub.add("GET", f"{U}/threads/{thread}", body={"messages": [_raw(msg_id, ms=ms, thread=thread)]})
        messages = _provider(stub).fetch_new(since, set(), [], limit=2)
        list_requests = [r for r in stub.requests if r["path"] == f"{U}/messages"]
        fetched_ids = {r["path"].rsplit("/", 1)[-1] for r in stub.requests if r["path"].startswith(f"{U}/messages/")}
    assert [m["id"] for m in messages] == ["a", "b"]
    assert len(list_requests) == 2
    assert "pageToken" not in list_requests[0]["query"]
    assert list_requests[1]["query"]["pageToken"] == ["p2"]
    assert fetched_ids == {"a", "b"}


def test_prior_thread_skips_draft_messages():
    """Controller review: the loop's own earlier draft in a thread must not
    be fed back to the AI as prior-thread context."""
    since = datetime(2026, 9, 25, tzinfo=timezone.utc)
    with StubServer() as stub:
        stub.add("GET", f"{U}/messages", body={"messages": [{"id": "c"}]})
        stub.add("GET", f"{U}/messages/c", body=_raw("c", ms=1790000900000, thread="t"))
        draft = _raw("draftmsg", body="draft reply", ms=1790000400000, thread="t")
        draft["labelIds"] = ["DRAFT"]
        thread = [_raw("a", body="first", ms=1790000100000, thread="t"), draft,
                  _raw("b", body="second", ms=1790000200000, thread="t"),
                  _raw("c", ms=1790000900000, thread="t")]
        stub.add("GET", f"{U}/threads/t", body={"messages": thread})
        messages = _provider(stub).fetch_new(since, set(), [], limit=1)
    assert [p["body_text"] for p in messages[0]["prior_thread"]] == ["first", "second"]


def test_ensure_labels_creates_only_missing():
    with StubServer() as stub:
        stub.add("GET", f"{U}/labels", body={"labels": [{"id": "L1", "name": "Loop/Urgent"}, {"id": "INBOX", "name": "INBOX"}]})
        stub.add("POST", f"{U}/labels", body={"id": "L2", "name": "Loop/FYI"})
        mapping = _provider(stub).ensure_labels(["Loop/Urgent", "Loop/FYI"])
        created = [json.loads(b) for b in stub.bodies("POST", f"{U}/labels")]
    assert mapping == {"Loop/Urgent": "L1", "Loop/FYI": "L2"}
    assert [c["name"] for c in created] == ["Loop/FYI"]


def test_apply_label_only_adds():
    with StubServer() as stub:
        stub.add("POST", f"{U}/messages/m1/modify", body={})
        _provider(stub).apply_label("m1", "L1")
        body = json.loads(stub.bodies("POST", f"{U}/messages/m1/modify")[0])
    assert body == {"addLabelIds": ["L1"]}


def test_create_reply_draft_is_threaded():
    message = gmail.parse_gmail_message(_raw("m1", subject="Prod down", headers=[{"name": "References", "value": "<r0@x.com>"}]))
    with StubServer() as stub:
        stub.add("POST", f"{U}/drafts", body={"id": "d1", "message": {"id": "dm1", "threadId": "t1"}})
        link = _provider(stub).create_reply_draft(message, "On it - fixing now.")
        sent = json.loads(stub.bodies("POST", f"{U}/drafts")[0])
    assert sent["message"]["threadId"] == "t1"
    mime = email.message_from_bytes(base64.urlsafe_b64decode(sent["message"]["raw"] + "=="), policy=email.policy.default)
    assert mime["To"] == "Alice <alice@x.com>"
    assert mime["Subject"] == "Re: Prod down"
    assert mime["In-Reply-To"] == "<m1@x.com>"
    assert mime["References"] == "<r0@x.com> <m1@x.com>"
    assert "On it - fixing now." in mime.get_content()
    assert link.startswith("https://mail.google.com/") and "dm1" in link


def test_reply_goes_to_reply_to_header_when_present():
    message = gmail.parse_gmail_message(_raw("m1", headers=[{"name": "Reply-To", "value": "team@x.com"}]))
    with StubServer() as stub:
        stub.add("POST", f"{U}/drafts", body={"id": "d1", "message": {"id": "dm1"}})
        _provider(stub).create_reply_draft(message, "ok")
        sent = json.loads(stub.bodies("POST", f"{U}/drafts")[0])
    assert email.message_from_bytes(base64.urlsafe_b64decode(sent["message"]["raw"] + "=="), policy=email.policy.default)["To"] == "team@x.com"


def test_401_refreshes_once_then_succeeds():
    refreshed = []
    with StubServer() as stub:
        stub.add("GET", f"{U}/profile", status=401, body={}).add("GET", f"{U}/profile", body={"emailAddress": "me@example.com"})
        provider = _provider(stub, refresh=lambda: refreshed.append(1) or "tok2")
        assert provider.profile_address() == "me@example.com"
        assert stub.requests[-1]["headers"]["Authorization"] == "Bearer tok2"
    assert refreshed == [1]


def test_second_401_raises_auth_expired():
    with StubServer() as stub:
        stub.add("GET", f"{U}/profile", status=401, body={})
        with pytest.raises(mail_http.AuthExpired):
            _provider(stub, refresh=lambda: "tok2").profile_address()


def test_gmail_source_has_no_send_and_only_interface_methods():
    for name in ("gmail.py", "base.py"):
        source = (REPO_ROOT / "bin" / "mail_providers" / name).read_text()
        assert not re.search(r"(?i)\bsend\b|/send", source), name
    public = {n for n, _ in inspect.getmembers(gmail.GmailProvider, inspect.isfunction) if not n.startswith("_")}
    assert public == base.PUBLIC_METHODS


def test_get_provider_factory():
    assert isinstance(mail_providers.get_provider({"provider": "gmail", "account": "a@b.co"}, "tok"), gmail.GmailProvider)
    with pytest.raises(ValueError):
        mail_providers.get_provider({"provider": "yahoo", "account": "a@b.co"}, "tok")
