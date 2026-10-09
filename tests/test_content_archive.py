import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import content_archive  # noqa: E402


def test_load_missing_or_corrupt_is_empty(tmp_path):
    assert content_archive.load(tmp_path / "none.json") == []
    (tmp_path / "bad.json").write_text("{nope")
    assert content_archive.load(tmp_path / "bad.json") == []
    (tmp_path / "obj.json").write_text('{"a": 1}')
    assert content_archive.load(tmp_path / "obj.json") == []


def test_append_keeps_newest_first_and_caps(tmp_path):
    path = tmp_path / "sub" / "c.json"
    content_archive.append(path, [{"n": 1}, {"n": 2}], cap=3)
    content_archive.append(path, [{"n": 3}, {"n": 4}], cap=3)
    assert [r["n"] for r in content_archive.load(path)] == [3, 4, 1]  # newest batch first, oldest dropped


def test_append_empty_is_a_noop(tmp_path):
    path = tmp_path / "c.json"
    content_archive.append(path, [])
    assert not path.exists()


def test_append_leaves_no_temp_files(tmp_path):
    content_archive.append(tmp_path / "c.json", [{"a": 1}])
    assert [p.name for p in tmp_path.iterdir()] == ["c.json"]
    assert isinstance(json.loads((tmp_path / "c.json").read_text()), list)
