import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import topic_config


def write_config(path, topics=None):
    if topics is None:
        topics = [
            {"name": "ai-news", "label": "AI news", "brief": "Major AI news.", "slack_bundle": None},
            {"name": "rust-lang", "label": "Rust language", "brief": "Rust releases and RFCs.", "slack_bundle": "eng-bundle"},
        ]
    path.write_text(json.dumps(topics))
    return path


def test_load_config_reads_file(tmp_path):
    config_path = write_config(tmp_path / "topics.json")

    topics = topic_config.load_config(config_path)

    assert [t["name"] for t in topics] == ["ai-news", "rust-lang"]


def test_load_config_missing_file_raises_helpful_error(tmp_path):
    missing_path = tmp_path / "does-not-exist.json"

    with pytest.raises(FileNotFoundError, match="topics.json.template"):
        topic_config.load_config(missing_path)


def test_load_config_rejects_non_list_shape(tmp_path):
    path = tmp_path / "topics.json"
    path.write_text(json.dumps({"not": "a list"}))

    with pytest.raises(ValueError, match="JSON array"):
        topic_config.load_config(path)


def test_list_names(tmp_path):
    config_path = write_config(tmp_path / "topics.json")

    assert topic_config.list_names(config_path) == ["ai-news", "rust-lang"]


def test_get_topic_returns_full_entry(tmp_path):
    config_path = write_config(tmp_path / "topics.json")

    topic = topic_config.get_topic("rust-lang", config_path)

    assert topic["label"] == "Rust language"
    assert topic["brief"] == "Rust releases and RFCs."
    assert topic["slack_bundle"] == "eng-bundle"


def test_get_topic_raises_key_error_for_unknown_name(tmp_path):
    config_path = write_config(tmp_path / "topics.json")

    with pytest.raises(KeyError):
        topic_config.get_topic("does-not-exist", config_path)


def test_main_names_prints_one_per_line(tmp_path, monkeypatch, capsys):
    config_path = write_config(tmp_path / "topics.json")
    monkeypatch.setattr(sys, "argv", ["topic_config.py", "names"])
    monkeypatch.setattr(topic_config, "DEFAULT_CONFIG_PATH", config_path)

    topic_config.main()

    assert capsys.readouterr().out.splitlines() == ["ai-news", "rust-lang"]


def test_main_topic_prints_json(tmp_path, monkeypatch, capsys):
    config_path = write_config(tmp_path / "topics.json")
    monkeypatch.setattr(sys, "argv", ["topic_config.py", "topic", "ai-news"])
    monkeypatch.setattr(topic_config, "DEFAULT_CONFIG_PATH", config_path)

    topic_config.main()

    printed = json.loads(capsys.readouterr().out)
    assert printed["label"] == "AI news"


def test_main_topic_unknown_name_exits_nonzero(tmp_path, monkeypatch, capsys):
    config_path = write_config(tmp_path / "topics.json")
    monkeypatch.setattr(sys, "argv", ["topic_config.py", "topic", "nope"])
    monkeypatch.setattr(topic_config, "DEFAULT_CONFIG_PATH", config_path)

    with pytest.raises(SystemExit) as exc_info:
        topic_config.main()

    assert exc_info.value.code != 0


def test_upsert_topic_creates_file_when_missing(tmp_path):
    config_path = tmp_path / "topics.json"

    ok, message = topic_config.upsert_topic("ai-news", "AI news", "Major AI news.", "", config_path)

    assert ok, message
    assert "Added" in message
    topics = topic_config.load_config(config_path)
    assert topics == [{"name": "ai-news", "label": "AI news", "brief": "Major AI news.", "slack_bundle": None}]


def test_upsert_topic_adds_to_existing_list(tmp_path):
    config_path = write_config(tmp_path / "topics.json")

    ok, message = topic_config.upsert_topic("new-topic", "New Topic", "Something new.", "", config_path)

    assert ok, message
    names = topic_config.list_names(config_path)
    assert names == ["ai-news", "rust-lang", "new-topic"]


def test_upsert_topic_updates_existing_entry_in_place(tmp_path):
    config_path = write_config(tmp_path / "topics.json")

    ok, message = topic_config.upsert_topic("ai-news", "AI News Updated", "New brief.", "eng-bundle", config_path)

    assert ok, message
    assert "Updated" in message
    topics = topic_config.load_config(config_path)
    assert [t["name"] for t in topics] == ["ai-news", "rust-lang"]
    updated = topic_config.get_topic("ai-news", config_path)
    assert updated == {"name": "ai-news", "label": "AI News Updated", "brief": "New brief.", "slack_bundle": "eng-bundle"}


def test_upsert_topic_blank_slack_bundle_means_default_webhook(tmp_path):
    config_path = write_config(tmp_path / "topics.json")

    topic_config.upsert_topic("ai-news", "AI news", "Major AI news.", "", config_path)

    assert topic_config.get_topic("ai-news", config_path)["slack_bundle"] is None


def test_upsert_topic_requires_name(tmp_path):
    config_path = tmp_path / "topics.json"

    ok, message = topic_config.upsert_topic("", "Label", "Brief", "", config_path)

    assert not ok
    assert "required" in message.lower()
    assert not config_path.exists()


def test_upsert_topic_requires_label(tmp_path):
    config_path = tmp_path / "topics.json"

    ok, message = topic_config.upsert_topic("name", "", "Brief", "", config_path)

    assert not ok
    assert "required" in message.lower()


def test_upsert_topic_requires_brief(tmp_path):
    config_path = tmp_path / "topics.json"

    ok, message = topic_config.upsert_topic("name", "Label", "", "", config_path)

    assert not ok
    assert "required" in message.lower()


def test_delete_topic_removes_entry(tmp_path):
    config_path = write_config(tmp_path / "topics.json")

    ok, message = topic_config.delete_topic("ai-news", config_path)

    assert ok, message
    assert topic_config.list_names(config_path) == ["rust-lang"]


def test_delete_topic_unknown_name_returns_false(tmp_path):
    config_path = write_config(tmp_path / "topics.json")

    ok, message = topic_config.delete_topic("does-not-exist", config_path)

    assert not ok
    assert "Unknown" in message
    assert topic_config.list_names(config_path) == ["ai-news", "rust-lang"]
