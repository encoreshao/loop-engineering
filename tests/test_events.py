import json
import os
import subprocess
import sys
from datetime import datetime, timezone
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


REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(args, env):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "events.py"), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_cli_emit_writes_event(tmp_path):
    env = {**os.environ}
    result = _run_cli(
        ["emit", "--type", "run.started", "--run-id", "run_1",
         "--events-dir", str(tmp_path)],
        env,
    )

    assert result.returncode == 0, result.stderr
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    event = json.loads(files[0].read_text().splitlines()[0])
    assert event["event_type"] == "run.started"
    assert event["run_id"] == "run_1"


def test_cli_emit_with_all_optional_fields(tmp_path):
    env = {**os.environ}
    result = _run_cli(
        ["emit", "--type", "issue.completed", "--run-id", "run_1",
         "--issue-run-id", "run_1_kurrant_123", "--project", "kurrant",
         "--issue-iid", "123", "--data", '{"action": "fix"}',
         "--events-dir", str(tmp_path)],
        env,
    )

    assert result.returncode == 0, result.stderr
    event = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()[0])
    assert event["issue_run_id"] == "run_1_kurrant_123"
    assert event["project"] == "kurrant"
    assert event["issue_iid"] == 123
    assert event["data"] == {"action": "fix"}


def test_cli_emit_missing_type_fails_clearly(tmp_path):
    result = _run_cli(
        ["emit", "--run-id", "run_1", "--events-dir", str(tmp_path)],
        {**os.environ},
    )

    assert result.returncode == 1
    assert "type" in result.stderr.lower()
    assert not list(tmp_path.glob("*.jsonl"))


def test_cli_emit_malformed_data_fails_clearly(tmp_path):
    result = _run_cli(
        ["emit", "--type", "run.started", "--run-id", "run_1",
         "--data", "{not valid json", "--events-dir", str(tmp_path)],
        {**os.environ},
    )

    assert result.returncode == 1
    assert "data" in result.stderr.lower()
    assert not list(tmp_path.glob("*.jsonl"))


def test_cli_list_filters_by_run_id_and_date(tmp_path):
    events.emit("run.started", run_id="run_1", events_dir=tmp_path)
    events.emit("run.started", run_id="run_2", events_dir=tmp_path)
    events.emit("run.completed", run_id="run_1", events_dir=tmp_path)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = _run_cli(
        ["list", "--date", today, "--run-id", "run_1",
         "--events-dir", str(tmp_path)],
        {**os.environ},
    )

    assert result.returncode == 0, result.stderr
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    assert len(lines) == 2
    assert all(json.loads(l)["run_id"] == "run_1" for l in lines)
    types = [json.loads(l)["event_type"] for l in lines]
    assert types == ["run.started", "run.completed"]


def test_cli_list_no_matches_prints_nothing(tmp_path):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = _run_cli(
        ["list", "--date", today, "--events-dir", str(tmp_path)],
        {**os.environ},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_cli_list_skips_malformed_line_without_crashing(tmp_path):
    events.emit("run.started", run_id="run_1", events_dir=tmp_path)

    path = list(tmp_path.glob("*.jsonl"))[0]
    with open(path, "a") as f:
        f.write("{not valid json\n")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = _run_cli(
        ["list", "--date", today, "--events-dir", str(tmp_path)],
        {**os.environ},
    )

    assert result.returncode == 0, result.stderr
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["event_type"] == "run.started"


def test_cli_list_by_run_id_scans_all_dates_without_date_flag(tmp_path):
    events.emit("run.started", run_id="run_shared", events_dir=tmp_path)

    other_path = tmp_path / "2020-01-01.jsonl"
    other_event = {
        "schema_version": 1,
        "event_id": "evt_other",
        "timestamp": "2020-01-01T00:00:00.000Z",
        "event_type": "run.completed",
        "run_id": "run_shared",
        "issue_run_id": None,
        "project": None,
        "issue_iid": None,
        "data": {},
    }
    other_path.write_text(json.dumps(other_event) + "\n")

    result = _run_cli(
        ["list", "--run-id", "run_shared", "--events-dir", str(tmp_path)],
        {**os.environ},
    )

    assert result.returncode == 0, result.stderr
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    assert len(lines) == 2
    types = [json.loads(l)["event_type"] for l in lines]
    # 2020-01-01.jsonl sorts before today's file, so it comes first.
    assert types == ["run.completed", "run.started"]


def test_cli_list_without_date_or_run_id_fails_clearly(tmp_path):
    result = _run_cli(
        ["list", "--events-dir", str(tmp_path)],
        {**os.environ},
    )

    assert result.returncode == 1
    assert "--date" in result.stderr or "--run-id" in result.stderr


def test_iter_events_yields_all_events_unfiltered(tmp_path):
    events.emit("run.started", run_id="run_1", events_dir=tmp_path)
    events.emit("run.completed", run_id="run_1", events_dir=tmp_path)

    result = list(events.iter_events(events_dir=tmp_path))

    assert [e["event_type"] for e in result] == ["run.started", "run.completed"]


def test_iter_events_filters_by_date_range(tmp_path):
    events.emit("run.started", run_id="run_old", events_dir=tmp_path)

    # Write a second, earlier date's file directly (emit() always writes
    # "today", so simulate an older day by writing the file ourselves).
    old_event = events.emit("run.started", run_id="run_older", events_dir=tmp_path)
    old_path = list(tmp_path.glob("*.jsonl"))[0]
    older_date = "2000-01-01"
    older_path = tmp_path / f"{older_date}.jsonl"
    older_path.write_text(json.dumps({**old_event, "run_id": "run_older_file"}) + "\n")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Unbounded: sees both files.
    all_events = list(events.iter_events(events_dir=tmp_path))
    assert len(all_events) == 3  # run_1's 2 events from the other test-writes above don't leak; this file has run_old + run_older, plus the synthetic older file's 1 event
    run_ids_seen = {e["run_id"] for e in all_events}
    assert "run_older_file" in run_ids_seen

    # since_date excludes the older file.
    since_only = list(events.iter_events(events_dir=tmp_path, since_date=today))
    assert all(e["run_id"] != "run_older_file" for e in since_only)

    # until_date set to the older date excludes today's file.
    until_only = list(events.iter_events(events_dir=tmp_path, until_date=older_date))
    assert all(e["run_id"] == "run_older_file" for e in until_only)


def test_iter_events_skips_malformed_line(tmp_path):
    events.emit("run.started", run_id="run_1", events_dir=tmp_path)
    path = list(tmp_path.glob("*.jsonl"))[0]
    with open(path, "a") as f:
        f.write("{not valid json\n")

    result = list(events.iter_events(events_dir=tmp_path))

    assert len(result) == 1
    assert result[0]["run_id"] == "run_1"


def test_iter_events_empty_dir_yields_nothing(tmp_path):
    assert list(events.iter_events(events_dir=tmp_path / "does-not-exist")) == []
