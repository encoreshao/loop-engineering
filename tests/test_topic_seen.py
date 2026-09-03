import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import topic_seen


def test_get_seen_returns_empty_when_no_state_file(tmp_path):
    assert topic_seen.get_seen("ai-news", state_dir=tmp_path) == []


def test_add_then_get_seen_round_trips(tmp_path):
    topic_seen.add_seen("ai-news", "https://example.com/a", "Story A", state_dir=tmp_path)

    seen = topic_seen.get_seen("ai-news", state_dir=tmp_path)

    assert seen == [{"url": "https://example.com/a", "title": "Story A"}]


def test_add_seen_is_scoped_per_topic(tmp_path):
    topic_seen.add_seen("ai-news", "https://example.com/a", "Story A", state_dir=tmp_path)
    topic_seen.add_seen("rust-lang", "https://example.com/b", "Story B", state_dir=tmp_path)

    assert topic_seen.get_seen("ai-news", state_dir=tmp_path) == [{"url": "https://example.com/a", "title": "Story A"}]
    assert topic_seen.get_seen("rust-lang", state_dir=tmp_path) == [{"url": "https://example.com/b", "title": "Story B"}]


def test_get_seen_prunes_entries_older_than_the_window(tmp_path):
    now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    old = now - timedelta(days=topic_seen.SEEN_WINDOW_DAYS + 1)
    topic_seen.add_seen("ai-news", "https://example.com/old", "Old story", state_dir=tmp_path, now=old)
    topic_seen.add_seen("ai-news", "https://example.com/new", "New story", state_dir=tmp_path, now=now)

    seen = topic_seen.get_seen("ai-news", state_dir=tmp_path)

    assert seen == [{"url": "https://example.com/new", "title": "New story"}]


def test_get_seen_returns_empty_on_malformed_json(tmp_path):
    (tmp_path / "ai-news.json").write_text("not json")

    assert topic_seen.get_seen("ai-news", state_dir=tmp_path) == []


def test_main_add_then_get(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(topic_seen, "DEFAULT_STATE_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["topic_seen.py", "add", "ai-news", "https://example.com/a", "Story A"])
    topic_seen.main()
    capsys.readouterr()

    monkeypatch.setattr(sys, "argv", ["topic_seen.py", "get", "ai-news"])
    topic_seen.main()

    printed = json.loads(capsys.readouterr().out)
    assert printed == [{"url": "https://example.com/a", "title": "Story A"}]
