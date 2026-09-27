#!/usr/bin/env python3
"""Pure logic for the Inbox Triage loop: turn normalized messages into the
AI prompt, and turn the AI's reply back into validated decisions. No I/O -
see bin/inbox_triage_runner.py for orchestration and
docs/superpowers/specs/2026-09-27-inbox-triage-design.md for the contract."""
import html
import json
import re
from html.parser import HTMLParser

import inbox_config

BODY_LIMIT = 4000
PRIOR_LIMIT = 1500
PRIOR_COUNT = 2
REASON_LIMIT = 200
DRAFT_LIMIT = 4000

_BLOCK_TAGS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "table"}
_SKIP_TAGS = {"script", "style", "head", "title"}
_PROMPT_FIELDS = ("id", "from", "to", "cc", "subject", "date", "body_text", "prior_thread", "attachments")


class TriageResponseError(ValueError):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup):
    parser = _TextExtractor()
    parser.feed(markup or "")
    parser.close()
    text = html.unescape("".join(parser.parts))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def trim(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def filter_excluded(messages, exclude):
    return [m for m in messages if not inbox_config.sender_matches(inbox_config.parse_address(m.get("from", "")), exclude)]


def build_prompt(instructions, inbox, categories, messages):
    payload = {
        "account": inbox["account"],
        "urgent_brief": inbox.get("urgent_brief", ""),
        "categories": [{"key": c["key"], "description": c["description"], "draft": bool(c.get("draft"))} for c in categories],
        "messages": [{field: m.get(field) for field in _PROMPT_FIELDS} for m in messages],
    }
    return f"{instructions.rstrip()}\n\n## Input\n\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n"


def extract_json_array(text):
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end < start:
        raise TriageResponseError("no JSON array in the AI response")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise TriageResponseError(f"AI response is not valid JSON: {exc.msg}") from None
    if not isinstance(data, list):
        raise TriageResponseError("AI response is not a JSON array")
    return data


def parse_response(text, messages, categories):
    entries = extract_json_array(text)
    by_key = {c["key"]: c for c in categories}
    expected = [m["id"] for m in messages]
    decisions = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise TriageResponseError("every entry must be an object")
        msg_id = entry.get("id")
        if msg_id not in expected:
            raise TriageResponseError(f"unknown id {msg_id!r}")
        if msg_id in decisions:
            raise TriageResponseError(f"duplicate id {msg_id!r}")
        category = entry.get("category")
        if category not in by_key:
            raise TriageResponseError(f"unknown category {category!r} for {msg_id}")
        reason = entry.get("reason") or ""
        if not isinstance(reason, str) or len(reason) > REASON_LIMIT:
            raise TriageResponseError(f"reason for {msg_id} must be a string of at most {REASON_LIMIT} chars")
        draft = entry.get("draft_body")
        if isinstance(draft, str) and not draft.strip():
            draft = None
        if draft is not None:
            if not isinstance(draft, str) or len(draft) > DRAFT_LIMIT:
                raise TriageResponseError(f"draft_body for {msg_id} must be a string of at most {DRAFT_LIMIT} chars")
            if not by_key[category].get("draft"):
                raise TriageResponseError(f"draft_body is not allowed for category {category!r} ({msg_id})")
        decisions[msg_id] = {"id": msg_id, "category": category, "reason": reason, "draft_body": draft}
    missing = [i for i in expected if i not in decisions]
    if missing:
        raise TriageResponseError(f"missing decisions for {', '.join(missing)}")
    return [decisions[i] for i in expected]


def apply_rules(decisions, messages, inbox):
    """Deterministic overrides the prompt is not trusted with: VIP senders
    are always urgent, and the user's own mail never gets a reply draft."""
    by_id = {m["id"]: m for m in messages}
    own = inbox["account"].strip().lower()
    out = []
    for decision in decisions:
        decision = dict(decision)
        sender = inbox_config.parse_address(by_id[decision["id"]].get("from", ""))
        if sender == own:
            decision["draft_body"] = None
            decision["needs_manual_reply"] = False
            out.append(decision)
            continue
        if inbox_config.sender_matches(sender, inbox.get("vip_senders", [])) and decision["category"] != "urgent":
            decision["category"] = "urgent"
            decision["reason"] = trim(f"VIP sender. {decision['reason']}", REASON_LIMIT)
        decision["needs_manual_reply"] = decision["category"] == "urgent" and not decision["draft_body"]
        out.append(decision)
    return out


def count_by_category(decisions):
    counts = {}
    for decision in decisions:
        counts[decision["category"]] = counts.get(decision["category"], 0) + 1
    return counts
