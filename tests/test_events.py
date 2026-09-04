import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import events


def test_emit_writes_one_line_with_required_fields(tmp_path):
    event = events.emit("run.started", run_id="run_1", events_dir=tmp_path)

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text().splitlines()
    assert len(lines) == 1

    on_disk = json.loads(lines[0])
    assert on_disk == event
    assert on_disk["event_type"] == "run.started"
    assert on_disk["run_id"] == "run_1"
    assert on_disk["schema_version"] == 1
    assert on_disk["event_id"].startswith("evt_")
    assert on_disk["issue_run_id"] is None
    assert on_disk["project"] is None
    assert on_disk["issue_iid"] is None
    assert on_disk["data"] == {}


def test_emit_timestamp_is_utc_iso8601(tmp_path):
    event = events.emit("run.started", run_id="run_1", events_dir=tmp_path)

    # Must parse as ISO-8601 and carry explicit UTC offset/marker.
    from datetime import datetime

    parsed = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_emit_optional_fields_round_trip(tmp_path):
    event = events.emit(
        "issue.completed",
        run_id="run_1",
        issue_run_id="run_1_kurrant_123",
        project="kurrant",
        issue_iid=123,
        data={"action": "fix", "mr_url": "https://example.test/mr/1"},
        events_dir=tmp_path,
    )

    assert event["issue_run_id"] == "run_1_kurrant_123"
    assert event["project"] == "kurrant"
    assert event["issue_iid"] == 123
    assert event["data"] == {"action": "fix", "mr_url": "https://example.test/mr/1"}


def test_emit_two_calls_never_collide_event_ids(tmp_path):
    first = events.emit("run.started", run_id="run_1", events_dir=tmp_path)
    second = events.emit("run.completed", run_id="run_1", events_dir=tmp_path)

    assert first["event_id"] != second["event_id"]


def test_emit_appends_rather_than_overwrites(tmp_path):
    events.emit("run.started", run_id="run_1", events_dir=tmp_path)
    events.emit("run.completed", run_id="run_1", events_dir=tmp_path)

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event_type"] == "run.started"
    assert json.loads(lines[1])["event_type"] == "run.completed"


def test_emit_raises_on_missing_event_type(tmp_path):
    with pytest.raises(ValueError):
        events.emit("", run_id="run_1", events_dir=tmp_path)


def test_emit_raises_on_missing_run_id(tmp_path):
    with pytest.raises(ValueError):
        events.emit("run.started", run_id="", events_dir=tmp_path)


def test_emit_default_events_dir_is_none_default_resolved_in_body(monkeypatch, tmp_path):
    # Regression guard for this repo's dependency-injection rule: a
    # monkeypatched DEFAULT_EVENTS_DIR must actually take effect, which
    # only happens if emit() resolves `events_dir=None` inside its own
    # body rather than binding DEFAULT_EVENTS_DIR at def time.
    monkeypatch.setattr(events, "DEFAULT_EVENTS_DIR", tmp_path)

    events.emit("run.started", run_id="run_1")

    assert list(tmp_path.glob("*.jsonl"))
