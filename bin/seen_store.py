"""Generic per-loop "seen" store: de-dups work items across loop runs.

File: <root>/<loop_name>/seen.json = {"<key>": "<iso-8601 utc>"}. Entries
older than window_days are dropped on save().
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loops_config import _VALID_LOOP_NAME_RE

# Per-checkout run state (like outputs/events), not per-machine config.
DEFAULT_STATE_ROOT = Path(__file__).resolve().parent.parent / "outputs" / "loops"


def _parse(value):
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class SeenStore:
    def __init__(self, loop_name, state_dir=None, window_days=30, now_fn=None):
        if not _VALID_LOOP_NAME_RE.match(loop_name) or loop_name in (".", ".."):
            raise ValueError(
                f"Invalid loop name {loop_name!r} - loop names may only contain "
                f"letters, digits, '.', '_', and '-'."
            )
        if state_dir is None:
            state_dir = DEFAULT_STATE_ROOT
        if now_fn is None:
            now_fn = lambda: datetime.now(timezone.utc)
        self._now_fn = now_fn
        self._window = timedelta(days=window_days)
        self._path = Path(state_dir) / loop_name / "seen.json"
        self._entries = self._load()

    def _load(self):
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str)}

    def has(self, key):
        return key in self._entries

    def add(self, key):
        self._entries[key] = self._now_fn().astimezone(timezone.utc).isoformat()

    def save(self):
        cutoff = self._now_fn() - self._window
        kept = {}
        for key, value in self._entries.items():
            dt = _parse(value)
            if dt is not None and dt >= cutoff:
                kept[key] = dt.isoformat()
        self._entries = kept
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f".{self._path.name}.tmp")
        with open(tmp, "w") as f:
            json.dump(kept, f, indent=2)
        tmp.replace(self._path)
