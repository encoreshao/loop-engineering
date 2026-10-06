#!/usr/bin/env python3
"""RSS / Atom feed connector (read-only, no secret)."""
import urllib.request
import xml.etree.ElementTree as ET

import i18n
from connectors import register
from connectors.base import Connector, ConnectorError, Field, FEED, TEST_TIMEOUT_SECONDS

MAX_BYTES = 2 * 1024 * 1024
_ATOM = "{http://www.w3.org/2005/Atom}"


def _fetch(url, timeout):
    if not str(url).lower().startswith(("http://", "https://")):
        raise ConnectorError(i18n.t("Only http:// and https:// feed URLs are supported"))
    req = urllib.request.Request(url, headers={"User-Agent": "LoopX/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(MAX_BYTES)


def _text(node, tag):
    child = node.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _parse(body):
    head = body.lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise ConnectorError(i18n.t("Feed contains a DOCTYPE or ENTITY declaration, which is not allowed"))
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ConnectorError(i18n.t("Feed is not valid XML: {detail}", detail=exc)) from None
    out = []
    for item in root.iter("item"):
        out.append({"title": _text(item, "title"), "link": _text(item, "link"),
                    "id": _text(item, "guid"), "published": _text(item, "pubDate")})
    for entry in root.iter(_ATOM + "entry"):
        link = ""
        for el in entry.findall(_ATOM + "link"):
            if el.get("rel", "alternate") == "alternate" and el.get("href"):
                link = el.get("href")
                break
        out.append({"title": _text(entry, _ATOM + "title"), "link": link,
                    "id": _text(entry, _ATOM + "id"),
                    "published": _text(entry, _ATOM + "published") or _text(entry, _ATOM + "updated")})
    return out


@register
class RSSConnector(Connector):
    type = "rss"
    label = "RSS / Atom feeds"
    icon = "rss_feed"
    capabilities = frozenset({FEED})
    fields = (Field("feeds", "Feed URLs", kind="textarea", help="One feed URL per line"),)
    secret_label = None

    def feed_urls(self):
        return [u.strip() for u in (self.settings.get("feeds") or "").splitlines() if u.strip()]

    def entries(self, url, timeout=TEST_TIMEOUT_SECONDS):
        return _parse(_fetch(url, timeout))

    def test(self):
        urls = self.feed_urls()
        if not urls:
            return False, i18n.t("{field} is required", field=i18n.t("Feed URLs"))
        total = 0
        for url in urls:
            try:
                total += len(self.entries(url))
            except ConnectorError as exc:
                return False, i18n.t("{feed}: {detail}", feed=url, detail=exc)
            except Exception as exc:  # noqa: BLE001 - test() must never raise
                return False, i18n.t("{feed}: {detail}", feed=url, detail=f"{type(exc).__name__}: {exc}")
        return True, i18n.t("{feeds} feeds, {entries} entries", feeds=len(urls), entries=total)
