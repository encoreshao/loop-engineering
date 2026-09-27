import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import inbox_seen  # noqa: E402

NOW = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)


def test_first_run_since_is_48_hours_back(tmp_path):
    state = inbox_seen.load("w", state_dir=tmp_path)
    assert state == {"high_water": None, "seen": {}}
    assert inbox_seen.since(state, NOW) == NOW - timedelta(hours=48)


def test_record_advances_high_water_to_newest_message(tmp_path):
    msgs = [{"id": "a", "date": "2026-09-27T07:00:00+00:00"}, {"id": "b", "date": "2026-09-27T08:30:00+00:00"}]
    inbox_seen.record("w", msgs, NOW, state_dir=tmp_path)
    state = inbox_seen.load("w", state_dir=tmp_path)
    assert state["high_water"] == "2026-09-27T08:30:00+00:00"
    assert set(state["seen"]) == {"a", "b"}
    assert inbox_seen.since(state, NOW) == datetime(2026, 9, 27, 8, 30, tzinfo=timezone.utc)


def test_record_never_moves_high_water_backwards(tmp_path):
    inbox_seen.record("w", [{"id": "b", "date": "2026-09-27T08:30:00+00:00"}], NOW, state_dir=tmp_path)
    inbox_seen.record("w", [{"id": "a", "date": "2026-09-27T07:00:00+00:00"}], NOW, state_dir=tmp_path)
    assert inbox_seen.load("w", state_dir=tmp_path)["high_water"] == "2026-09-27T08:30:00+00:00"


def test_record_empty_keeps_state(tmp_path):
    inbox_seen.record("w", [], NOW, state_dir=tmp_path)
    assert inbox_seen.load("w", state_dir=tmp_path) == {"high_water": None, "seen": {}}


def test_record_prunes_seen_older_than_14_days(tmp_path):
    path = tmp_path / "w.json"
    path.write_text(json.dumps({"high_water": None, "seen": {"old": (NOW - timedelta(days=15)).isoformat(), "new": NOW.isoformat()}}))
    inbox_seen.record("w", [], NOW, state_dir=tmp_path)
    assert set(inbox_seen.load("w", state_dir=tmp_path)["seen"]) == {"new"}


def test_corrupt_state_is_treated_as_first_run(tmp_path):
    (tmp_path / "w.json").write_text("{nope")
    assert inbox_seen.load("w", state_dir=tmp_path) == {"high_water": None, "seen": {}}


def test_default_dir_resolved_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox_seen, "DEFAULT_STATE_DIR", tmp_path)
    inbox_seen.record("w", [{"id": "a", "date": NOW.isoformat()}], NOW)
    assert (tmp_path / "w.json").exists()
