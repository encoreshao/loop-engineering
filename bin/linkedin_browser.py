#!/usr/bin/env python3
"""LinkedIn page reading: pure HTML parsers, challenge detection and (below)
the Session that fetches pages through linkedin_humanize.HumanPage. This is the
only module that knows LinkedIn's page structure. It never navigates, scrolls,
clicks or types by itself - tests/test_linkedin_browser.py enforces that."""
import hashlib
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

LINKEDIN = "https://www.linkedin.com"
MAX_TEXT = 400
_VOID = {"br", "img", "input", "meta", "link", "hr", "wbr", "source", "area", "col", "embed", "track"}
_LOGIN_PATHS = ("/login", "/uas/", "/authwall", "/signup")
_CHALLENGE_PATHS = ("/checkpoint/", "/challenge")
_CHALLENGE_WORDS = ("captcha", "security verification", "quick security check",
                    "unusual activity", "restricted your account")


class BrowserUnavailable(Exception):
    pass


class SessionExpired(Exception):
    pass


class ChallengeError(Exception):
    pass


class _Nodes(HTMLParser):
    """Collects, for every element `match(tag, attrs)` accepts, its inner text
    and the hrefs inside it. Nested matches are collected independently; each
    carries a `seq` (opening order) so callers can sort into document order."""

    def __init__(self, match):
        super().__init__(convert_charrefs=True)
        self._match, self.found = match, []
        self._open, self._depth, self._seq = [], 0, 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            for node in self._open:
                node["hrefs"].append(attrs["href"])
        if tag in _VOID:
            return
        self._depth += 1
        if self._match(tag, attrs):
            self._seq += 1
            self._open.append({"seq": self._seq, "depth": self._depth, "attrs": attrs, "text": [], "hrefs": []})

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        while self._open and self._open[-1]["depth"] >= self._depth:
            node = self._open.pop()
            self.found.append({"seq": node["seq"], "attrs": node["attrs"], "hrefs": node["hrefs"],
                               "text": " ".join("".join(node["text"]).split())})
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data):
        for node in self._open:
            node["text"].append(data)

    def close(self):
        super().close()
        while self._open:                     # unclosed elements still count
            node = self._open.pop()
            self.found.append({"seq": node["seq"], "attrs": node["attrs"], "hrefs": node["hrefs"],
                               "text": " ".join("".join(node["text"]).split())})


def _extract(html, match):
    parser = _Nodes(match)
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:  # noqa: BLE001 - broken markup must never crash a run
        pass
    # Document order (outermost first), so a nested duplicate never beats its parent.
    return sorted(parser.found, key=lambda node: node["seq"])


def _clean_url(href):
    """Absolute https://www.linkedin.com URL without query/fragment, else ''."""
    href = (href or "").split("?")[0].split("#")[0]
    if href.startswith("/"):
        href = LINKEDIN + href
    return href if href.startswith(LINKEDIN + "/") else ""


def _first(hrefs, needle=""):
    for href in hrefs:
        url = _clean_url(href)
        if url and needle in url:
            return url
    return ""


def _row_id(kind, text, url):
    return f"{kind}:" + hashlib.sha1(f"{url}|{text}".encode()).hexdigest()[:12]


def _has_class(attrs, name):
    return name in (attrs.get("class") or "").split()


def parse_notifications(html):
    rows = []
    for node in _extract(html, lambda t, a: t == "article" and _has_class(a, "nt-card")):
        text = node["text"][:MAX_TEXT]
        if text:
            url = _first(node["hrefs"])
            rows.append({"id": _row_id("notification", text, url), "text": text, "url": url})
    return rows


def parse_feed(html):
    rows, seen = [], set()
    for node in _extract(html, lambda t, a: (a.get("data-urn") or "").startswith("urn:li:activity:")):
        urn = node["attrs"]["data-urn"]
        text = node["text"][:MAX_TEXT]
        if urn in seen or not text:
            continue
        seen.add(urn)
        rows.append({"id": urn, "text": text, "url": f"{LINKEDIN}/feed/update/{urn}/",
                     "profile": _first(node["hrefs"], "/in/")})
    return rows


def parse_messages(html):
    rows = []
    for node in _extract(html, lambda t, a: t == "li" and _has_class(a, "msg-conversation-listitem")):
        text = node["text"][:MAX_TEXT]
        if text:
            url = _first(node["hrefs"], "/messaging/")
            rows.append({"id": _row_id("message", text, url), "text": text, "url": url})
    return rows


def detect_challenge(url, html=""):
    """None for a normal page, 'login' for a login wall, 'challenge' for a
    checkpoint / captcha / security check. Text is searched outside
    <script>/<style> only, so page code cannot trigger it."""
    path = urlparse(url or "").path.lower()
    if any(p in path for p in _CHALLENGE_PATHS):
        return "challenge"
    if any(path.startswith(p) for p in _LOGIN_PATHS):
        return "login"
    visible = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html or "").lower()
    if any(word in visible for word in _CHALLENGE_WORDS):
        return "challenge"
    return None
