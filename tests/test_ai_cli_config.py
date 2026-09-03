import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import ai_cli_config


def test_get_selected_cli_defaults_to_claude_when_file_missing(tmp_path):
    config_path = tmp_path / "does-not-exist.json"

    assert ai_cli_config.get_selected_cli(config_path) == "claude"


def test_get_selected_cli_reads_stored_value(tmp_path):
    config_path = tmp_path / "ai_cli.json"
    config_path.write_text(json.dumps({"cli": "codex"}))

    assert ai_cli_config.get_selected_cli(config_path) == "codex"


def test_get_selected_cli_falls_back_to_claude_on_invalid_value(tmp_path):
    config_path = tmp_path / "ai_cli.json"
    config_path.write_text(json.dumps({"cli": "gpt5"}))

    assert ai_cli_config.get_selected_cli(config_path) == "claude"


def test_get_selected_cli_falls_back_to_claude_on_corrupt_json(tmp_path):
    config_path = tmp_path / "ai_cli.json"
    config_path.write_text("{not valid json")

    assert ai_cli_config.get_selected_cli(config_path) == "claude"


def test_set_selected_cli_writes_file(tmp_path):
    config_path = tmp_path / "nested" / "ai_cli.json"

    ok, message = ai_cli_config.set_selected_cli("codex", config_path)

    assert ok is True
    assert "codex" in message
    assert ai_cli_config.get_selected_cli(config_path) == "codex"


def test_set_selected_cli_rejects_unknown_cli(tmp_path):
    config_path = tmp_path / "ai_cli.json"

    ok, message = ai_cli_config.set_selected_cli("bogus", config_path)

    assert ok is False
    assert "Unknown AI CLI" in message
    assert not config_path.exists()


def test_set_selected_cli_round_trips_back_to_claude(tmp_path):
    config_path = tmp_path / "ai_cli.json"
    ai_cli_config.set_selected_cli("codex", config_path)

    ai_cli_config.set_selected_cli("claude", config_path)

    assert ai_cli_config.get_selected_cli(config_path) == "claude"
