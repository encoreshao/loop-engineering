"""Microsoft Graph v1.0 provider. The token this loop obtains cannot
deliver mail at all (see mail_auth.OUTLOOK_SCOPE). Mailbox writes are limited to: creating master
categories, appending a category to a message, and creating a reply draft
via createReply. Nothing here archives, deletes, or changes read state."""
import urllib.parse
from datetime import datetime, timedelta, timezone

import inbox_config
import inbox_triage
from mail_providers.base import BaseProvider, plain_addresses

_PREFER_TEXT = {"Prefer": 'outlook.body-content-type="text"'}
_SELECT = "id,conversationId,from,toRecipients,ccRecipients,subject,receivedDateTime,body,hasAttachments,internetMessageId"
_LIST_PAGE_LIMIT = 20  # safety cap: 20 pages of $top=100, mirroring Gmail's own list-page cap


def _person(entry):
    addr = (entry or {}).get("emailAddress") or {}
    address, name = addr.get("address") or "", addr.get("name") or ""
    if not address:
        return ""
    return f"{name} <{address}>" if name and name != address else address


def _body(raw):
    body = raw.get("body") or {}
    content = body.get("content") or ""
    if (body.get("contentType") or "").lower() == "html":
        return inbox_triage.html_to_text(content)
    return content.strip()


def _iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()


def parse_graph_message(raw):
    return {
        "id": raw["id"],
        "thread_id": raw.get("conversationId", ""),
        "from": _person(raw.get("from")),
        "to": ", ".join(p for p in (_person(r) for r in raw.get("toRecipients", [])) if p),
        "cc": ", ".join(p for p in (_person(r) for r in raw.get("ccRecipients", [])) if p),
        "subject": raw.get("subject") or "",
        "date": _iso(raw["receivedDateTime"]),
        "body_text": inbox_triage.trim(_body(raw), inbox_triage.BODY_LIMIT),
        "prior_thread": [],
        "attachments": [],
        "_has_attachments": bool(raw.get("hasAttachments")),
    }


class OutlookProvider(BaseProvider):
    DEFAULT_BASE_URL = "https://graph.microsoft.com/v1.0"

    def profile_address(self):
        me = self._call("GET", "/me")
        return (me.get("mail") or me.get("userPrincipalName") or "").strip().lower()

    def search_recent(self, addresses, days, limit, now=None):
        """Subject/From/date/preview of the newest `limit` messages with any
        of `addresses` as a participant in the last `days` days - never
        bodies. Graph's $search cannot be combined with a date $filter or
        $orderby, so the window is applied here."""
        addresses = plain_addresses(addresses)
        if not addresses:
            return []
        now = now or datetime.now(timezone.utc)
        search = " OR ".join(f"participants:{a}" for a in addresses)
        params = {"$search": f'"{search}"', "$select": "subject,from,receivedDateTime,bodyPreview",
                  "$top": 50}
        page = self._call("GET", f"/me/messages?{urllib.parse.urlencode(params)}")
        cutoff = now - timedelta(days=int(days))
        rows = []
        for raw in page.get("value", []):
            try:
                received = datetime.fromisoformat(str(raw.get("receivedDateTime")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if received < cutoff:
                continue
            rows.append({"subject": raw.get("subject") or "", "from": _person(raw.get("from")),
                         "date": received.isoformat(), "snippet": raw.get("bodyPreview") or ""})
        rows.sort(key=lambda r: r["date"], reverse=True)
        return rows[:int(limit)]

    def fetch_new(self, since, seen_ids, exclude, limit):
        since_utc = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {
            # Graph rejects $orderby on a property that isn't also the
            # leading clause of $filter, in the same order (400
            # InefficientFilter) - receivedDateTime must come first here.
            "$filter": f"receivedDateTime ge {since_utc} and isRead eq false",
            "$orderby": "receivedDateTime asc",
            "$top": "100",
            "$select": _SELECT,
        }
        url = f"/me/mailFolders/inbox/messages?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"
        messages = []
        for _ in range(_LIST_PAGE_LIMIT):
            page = self._call("GET", url, headers=_PREFER_TEXT)
            for raw in page.get("value", []):
                if raw["id"] in seen_ids:
                    continue
                parsed = parse_graph_message(raw)
                if inbox_config.sender_matches(inbox_config.parse_address(parsed["from"]), exclude):
                    continue
                messages.append(parsed)
                if len(messages) >= limit:
                    break
            if len(messages) >= limit:
                break
            next_link = page.get("@odata.nextLink")
            if not next_link:
                break
            # @odata.nextLink is an absolute URL; BaseProvider._call passes
            # it straight through instead of prefixing the base URL.
            url = next_link
        messages.sort(key=lambda m: m["date"])
        for message in messages:
            if message.pop("_has_attachments", False):
                atts = self._call("GET", f"/me/messages/{message['id']}/attachments?$select=name")
                message["attachments"] = [a.get("name", "") for a in atts.get("value", [])]
            message["prior_thread"] = self._prior_thread(message)
        return messages

    def _prior_thread(self, message):
        params = {
            "$filter": f"conversationId eq '{message['thread_id'].replace(chr(39), chr(39) * 2)}'",
            "$select": "id,from,receivedDateTime,body,isDraft",
            "$top": "10",
        }
        page = self._call("GET", f"/me/messages?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}",
                          headers=_PREFER_TEXT)
        # Never feed the loop's own earlier draft reply in this thread back to the AI.
        earlier = [parse_graph_message({**raw, "subject": ""}) for raw in page.get("value", [])
                   if raw.get("id") != message["id"] and raw.get("receivedDateTime") and not raw.get("isDraft")]
        earlier = sorted((m for m in earlier if m["date"] < message["date"]), key=lambda m: m["date"])
        return [{"from": m["from"], "date": m["date"], "body_text": inbox_triage.trim(m["body_text"], inbox_triage.PRIOR_LIMIT)}
                for m in earlier[-inbox_triage.PRIOR_COUNT:]]

    def ensure_labels(self, labels):
        existing = {c["displayName"] for c in self._call("GET", "/me/outlook/masterCategories").get("value", [])}
        for name in labels:
            if name not in existing:
                self._call("POST", "/me/outlook/masterCategories", json_body={"displayName": name, "color": "preset7"})
                existing.add(name)
        return {name: name for name in labels}

    def apply_label(self, message_id, label_id):
        current = self._call("GET", f"/me/messages/{message_id}?$select=categories").get("categories") or []
        if label_id in current:
            return
        self._call("PATCH", f"/me/messages/{message_id}", json_body={"categories": current + [label_id]})

    def create_reply_draft(self, message, body):
        draft = self._call("POST", f"/me/messages/{message['id']}/createReply", json_body={"comment": body})
        return draft.get("webLink", "")
