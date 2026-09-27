"""Gmail REST v1 provider. Mailbox writes are limited to: creating labels,
adding a label to a message, and creating a draft. Nothing here delivers
mail, archives, deletes, or changes read state."""
import base64
import email.message
import re
import urllib.parse
from datetime import datetime, timezone

import inbox_config
import inbox_triage
from mail_providers.base import BaseProvider

_LIST_PAGE_LIMIT = 20  # safety cap: 20 pages of maxResults=100


def _header(headers, name):
    for h in headers or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode(data, charset):
    raw = base64.urlsafe_b64decode((data or "") + "=" * (-len(data or "") % 4))
    try:
        return raw.decode(charset or "utf-8", "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def _charset(part):
    match = re.search(r"charset=\"?([\w.-]+)", _header(part.get("headers"), "Content-Type"), re.I)
    return match.group(1) if match else "utf-8"


def _walk(part):
    yield part
    for child in part.get("parts", []) or []:
        yield from _walk(child)


def _body_text(payload):
    plain, markup = None, None
    for part in _walk(payload):
        if part.get("filename"):
            continue
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        if part.get("mimeType") == "text/plain" and plain is None:
            plain = _decode(data, _charset(part))
        elif part.get("mimeType") == "text/html" and markup is None:
            markup = _decode(data, _charset(part))
    if plain is not None and plain.strip():
        return plain.strip()
    return inbox_triage.html_to_text(markup or "")


def parse_gmail_message(raw):
    payload = raw.get("payload") or {}
    headers = payload.get("headers") or []
    date = datetime.fromtimestamp(int(raw.get("internalDate", "0")) / 1000, timezone.utc).isoformat()
    return {
        "id": raw["id"],
        "thread_id": raw.get("threadId", ""),
        "from": _header(headers, "From"),
        "to": _header(headers, "To"),
        "cc": _header(headers, "Cc"),
        "subject": _header(headers, "Subject"),
        "date": date,
        "body_text": inbox_triage.trim(_body_text(payload), inbox_triage.BODY_LIMIT),
        "prior_thread": [],
        "attachments": [p["filename"] for p in _walk(payload) if p.get("filename")],
        "_message_id_header": _header(headers, "Message-ID"),
        "_references": _header(headers, "References"),
        "_reply_to": _header(headers, "Reply-To"),
    }


def reply_subject(subject):
    subject = (subject or "").strip()
    return subject if re.match(r"(?i)^re:", subject) else f"Re: {subject}".strip()


class GmailProvider(BaseProvider):
    DEFAULT_BASE_URL = "https://gmail.googleapis.com/gmail/v1"

    def profile_address(self):
        return self._call("GET", "/users/me/profile").get("emailAddress", "").strip().lower()

    def fetch_new(self, since, seen_ids, exclude, limit):
        query = f"in:inbox is:unread after:{int(since.timestamp())}"
        ids, page_token = [], None
        for _ in range(_LIST_PAGE_LIMIT):
            params = {"q": query, "maxResults": 100}
            if page_token:
                params["pageToken"] = page_token
            page = self._call("GET", f"/users/me/messages?{urllib.parse.urlencode(params)}")
            ids += [m["id"] for m in page.get("messages", []) if m["id"] not in seen_ids]
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        # messages.list is newest-first; walk oldest-first so a backlog
        # larger than one page still yields the true oldest messages, not
        # just the newest page's.
        ids.reverse()
        messages = []
        for msg_id in ids:
            if len(messages) >= limit:
                break
            parsed = parse_gmail_message(self._call("GET", f"/users/me/messages/{msg_id}?format=full"))
            if inbox_config.sender_matches(inbox_config.parse_address(parsed["from"]), exclude):
                continue
            messages.append(parsed)
        messages.sort(key=lambda m: m["date"])
        for message in messages:
            message["prior_thread"] = self._prior_thread(message)
        return messages

    def _prior_thread(self, message):
        thread = self._call("GET", f"/users/me/threads/{message['thread_id']}?format=full")
        # Never feed the loop's own earlier drafts in this thread back to the AI.
        earlier = [parse_gmail_message(m) for m in thread.get("messages", [])
                   if "DRAFT" not in (m.get("labelIds") or [])]
        earlier = [m for m in earlier if m["date"] < message["date"] and m["id"] != message["id"]]
        earlier.sort(key=lambda m: m["date"])
        return [{"from": m["from"], "date": m["date"], "body_text": inbox_triage.trim(m["body_text"], inbox_triage.PRIOR_LIMIT)}
                for m in earlier[-inbox_triage.PRIOR_COUNT:]]

    def ensure_labels(self, labels):
        existing = {l["name"]: l["id"] for l in self._call("GET", "/users/me/labels").get("labels", [])}
        mapping = {}
        for name in labels:
            if name not in existing:
                created = self._call("POST", "/users/me/labels", json_body={
                    "name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"})
                existing[name] = created["id"]
            mapping[name] = existing[name]
        return mapping

    def apply_label(self, message_id, label_id):
        self._call("POST", f"/users/me/messages/{message_id}/modify", json_body={"addLabelIds": [label_id]})

    def create_reply_draft(self, message, body):
        mime = email.message.EmailMessage()
        mime["To"] = message.get("_reply_to") or message["from"]
        mime["Subject"] = reply_subject(message.get("subject", ""))
        if message.get("_message_id_header"):
            mime["In-Reply-To"] = message["_message_id_header"]
            mime["References"] = " ".join(x for x in (message.get("_references"), message["_message_id_header"]) if x)
        mime.set_content(body)
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        draft = self._call("POST", "/users/me/drafts", json_body={"message": {"raw": raw, "threadId": message["thread_id"]}})
        draft_message_id = (draft.get("message") or {}).get("id", "")
        return f"https://mail.google.com/mail/?authuser={urllib.parse.quote(self._account)}#drafts?compose={draft_message_id}"
