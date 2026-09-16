import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import issue_tracking_config as itc


def test_is_issue_enabled_defaults_true_when_no_file_exists(tmp_path):
    path = tmp_path / "issue_tracking.json"

    assert itc.is_issue_enabled("harbor", 42, path=path) is True


def test_set_issue_enabled_false_then_is_issue_enabled_returns_false(tmp_path):
    path = tmp_path / "issue_tracking.json"

    itc.set_issue_enabled("harbor", 42, False, path=path)

    assert itc.is_issue_enabled("harbor", 42, path=path) is False


def test_set_issue_enabled_only_affects_the_given_issue(tmp_path):
    path = tmp_path / "issue_tracking.json"

    itc.set_issue_enabled("harbor", 42, False, path=path)

    assert itc.is_issue_enabled("harbor", 43, path=path) is True
    assert itc.is_issue_enabled("orchard", 42, path=path) is True


def test_set_issue_enabled_true_removes_a_previously_disabled_entry(tmp_path):
    """Re-enabling clears the entry rather than storing {"enabled": true} -
    the file only ever grows with issues someone actually opted out of."""
    path = tmp_path / "issue_tracking.json"
    itc.set_issue_enabled("harbor", 42, False, path=path)

    itc.set_issue_enabled("harbor", 42, True, path=path)

    assert itc.is_issue_enabled("harbor", 42, path=path) is True
    assert json.loads(path.read_text()) == {}


def test_set_issue_enabled_persists_to_disk_for_a_fresh_read(tmp_path):
    path = tmp_path / "issue_tracking.json"
    itc.set_issue_enabled("harbor", 42, False, path=path)

    # A completely separate read (no shared in-memory state) must still see it.
    assert json.loads(path.read_text()) == {"harbor#42": {"enabled": False}}


def test_set_issue_enabled_returns_ok_and_message(tmp_path):
    ok, message = itc.set_issue_enabled("harbor", 42, False, path=tmp_path / "issue_tracking.json")
    assert ok is True
    assert "Disabled" in message
    assert "42" in message
