import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import inbox_status  # noqa: E402


def test_read_missing_is_empty(tmp_path):
    assert inbox_status.read(tmp_path / "status.json") == {"inboxes": {}}


def test_write_merges_per_inbox(tmp_path):
    path = tmp_path / "status.json"
    inbox_status.write("w", "running", status_path=path)
    inbox_status.write("h", "idle", status_path=path, counts={"fyi": 1})
    inbox_status.write("w", "failed", status_path=path, error="boom")
    data = inbox_status.read(path)["inboxes"]
    assert data["w"]["state"] == "failed" and data["w"]["error"] == "boom"
    assert data["h"]["counts"] == {"fyi": 1}
    assert "updated_at" in data["h"]


def test_any_running(tmp_path):
    path = tmp_path / "status.json"
    assert not inbox_status.any_running(path)
    inbox_status.write("w", "running", status_path=path)
    assert inbox_status.any_running(path)


def test_corrupt_status_reads_empty(tmp_path):
    path = tmp_path / "status.json"
    path.write_text("[1,2")
    assert inbox_status.read(path) == {"inboxes": {}}


def test_remove_drops_only_that_inbox(tmp_path):
    path = tmp_path / "status.json"
    inbox_status.write("w", "idle", status_path=path)
    inbox_status.write("h", "failed", status_path=path)
    assert inbox_status.remove("w", status_path=path) is True
    assert list(inbox_status.read(path)["inboxes"]) == ["h"]
    assert inbox_status.remove("w", status_path=path) is False


def test_remove_on_missing_file_is_a_no_op(tmp_path):
    path = tmp_path / "status.json"
    assert inbox_status.remove("w", status_path=path) is False
    assert not path.exists()


def test_remove_default_path_resolved_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox_status, "DEFAULT_STATUS_PATH", tmp_path / "status.json")
    inbox_status.write("w", "idle")
    inbox_status.remove("w")
    assert inbox_status.read()["inboxes"] == {}
