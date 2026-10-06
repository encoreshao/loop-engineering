#!/usr/bin/env python3
"""Hub pages: one sidebar entry, several tabbed views, each view reusing an
existing page body. Pure helpers only - no dashboard_server import - so the
tab/selection logic is unit-testable on its own. Views are selected with the
`view` query param, never `tab`, because `tab` is already used *inside*
bodies (General Settings, Inbox Setup)."""
import html
from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass(frozen=True)
class HubView:
    key: str
    label: str  # English literal; translated at render time
    body_fn: Callable[..., str]
    refresh: bool = False
    lazy_refresh: bool = False
    badge_fn: Callable[[], str] | None = None


@dataclass(frozen=True)
class Hub:
    key: str
    path: str
    label: str
    icon: str
    views: tuple


def resolve_view(hub, requested):
    for view in hub.views:
        if view.key == requested:
            return view
    return hub.views[0]


def hub_tab_strip_html(hub_path, views, active_key, translate, extra_query=""):
    if len(views) < 2:
        return ""
    links = []
    for view in views:
        current = " aria-current='page'" if view.key == active_key else ""
        cls = "hub-tab active" if view.key == active_key else "hub-tab"
        href = f"{hub_path}?view={view.key}{extra_query}"
        links.append(
            f"<a class='{cls}' href='{html.escape(href)}'{current}>"
            f"{html.escape(translate(view.label))}</a>"
        )
    label = html.escape(translate("Page sections"))
    return f"<nav class='hub-tabs' aria-label='{label}'>{''.join(links)}</nav>"
