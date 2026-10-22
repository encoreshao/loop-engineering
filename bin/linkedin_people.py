#!/usr/bin/env python3
"""The people the LinkedIn loop has found through the header search: one JSON
list the dashboard shows for review. Per-checkout run state (like the loop's
seen store), keyed by profile URL."""
import json
from pathlib import Path

import seen_store

MIN_TARGET, MAX_TARGET, DEFAULT_TARGET = 20, 50, 30
MAX_STORED = 1000


def default_path():
    return Path(seen_store.DEFAULT_STATE_ROOT) / "linkedin-autopilot-loop" / "people.json"


def target(value):
    """How many NEW people one run should collect: 20-50, default 30."""
    try:
        return max(MIN_TARGET, min(MAX_TARGET, int(str(value).strip())))
    except (TypeError, ValueError):
        return DEFAULT_TARGET


def load(path=None):
    path = Path(path) if path else default_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict) and r.get("url")] if isinstance(data, list) else []


def known_urls(path=None):
    return {r["url"] for r in load(path)}


def merge(rows, query, now, path=None):
    """Add rows (newest first), refresh ones already known; returns how many were new."""
    path = Path(path) if path else default_path()
    stamp = now.isoformat()
    existing = load(path)
    current = {r["url"]: r for r in existing}
    fresh = []
    for row in rows:
        url = row.get("url")
        if not url:
            continue
        keep = {k: row.get(k, "") for k in ("name", "url", "avatar", "company", "position")}
        if url in current:
            current[url].update({k: v for k, v in keep.items() if v}, last_seen=stamp)
        else:
            fresh.append(dict(keep, query=query, first_seen=stamp, last_seen=stamp))
            current[url] = fresh[-1]
    merged = (fresh[::-1] + existing)[:MAX_STORED]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, indent=1))
    tmp.replace(path)
    return len(fresh)
