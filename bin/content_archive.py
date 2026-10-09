#!/usr/bin/env python3
"""Small newest-first JSON archive for content the loops produce (draft
replies, RSS highlights, meeting briefs) so the dashboard can show it any
time after the Slack message has scrolled away. One file holds a list of
records; only the newest `cap` are kept. Paths are passed in by callers
(resolved at call time) so tests can point everything at tmp_path."""
import json
import os
import uuid
from pathlib import Path

DEFAULT_CAP = 300


def load(path):
    """The archived records, newest first; [] when missing or unreadable."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def append(path, records, cap=DEFAULT_CAP):
    """Put `records` (in the given order) ahead of what is stored, keep the
    newest `cap`, and write atomically. Does nothing for an empty batch."""
    records = [r for r in records if isinstance(r, dict)]
    if not records:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps((records + load(path))[:cap], indent=2) + "\n")
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
