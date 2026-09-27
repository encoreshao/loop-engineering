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
