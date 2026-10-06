import json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import seen_store

NOW = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)


def store(tmp_path, now=NOW, **kw):
    return seen_store.SeenStore("demo-loop", state_dir=tmp_path, now_fn=lambda: now, **kw)


def test_seen_store_roundtrip(tmp_path):
    s = store(tmp_path); s.add("a"); s.save()
    assert store(tmp_path).has("a") and not store(tmp_path).has("b")


def test_seen_store_prunes_old_entries(tmp_path):
    s = store(tmp_path, window_days=7); s.add("old"); s.save()
    later = store(tmp_path, now=NOW + timedelta(days=8), window_days=7)
    later.add("new"); later.save()
    data = json.loads((tmp_path / "demo-loop" / "seen.json").read_text())
    assert set(data) == {"new"}


def test_seen_store_corrupt_file_is_empty(tmp_path):
    (tmp_path / "demo-loop").mkdir(); (tmp_path / "demo-loop" / "seen.json").write_text("{nope")
    assert not store(tmp_path).has("a")


def test_seen_store_non_dict_json_is_empty(tmp_path):
    (tmp_path / "demo-loop").mkdir(); (tmp_path / "demo-loop" / "seen.json").write_text("[1]")
    assert not store(tmp_path).has("a")


def test_seen_store_drops_unparseable_dates_on_save(tmp_path):
    (tmp_path / "demo-loop").mkdir()
    (tmp_path / "demo-loop" / "seen.json").write_text(json.dumps({"bad": "x", "ok": NOW.isoformat()}))
    s = store(tmp_path); s.save()
    data = json.loads((tmp_path / "demo-loop" / "seen.json").read_text())
    assert set(data) == {"ok"}


def test_seen_store_rejects_path_traversal_loop_name(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        seen_store.SeenStore("../x", state_dir=tmp_path)


def test_seen_store_unsaved_adds_not_persisted(tmp_path):
    s = store(tmp_path); s.add("a")
    assert not store(tmp_path).has("a")
