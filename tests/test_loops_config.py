import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import loops_config as lc


def _write_registry(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries))


def test_list_loops_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        lc.list_loops(config_path=tmp_path / "loops.json")


def test_list_loops_returns_registered_entries_in_order(tmp_path):
    config_path = tmp_path / "loops.json"
    entries = [
        {"name": "gitlab-issue-loop", "entry_point": "bin.gitlab_loop_runner"},
        {"name": "topic-monitor", "entry_point": "bin.topic_monitor_runner"},
    ]
    _write_registry(config_path, entries)

    assert lc.list_loops(config_path=config_path) == entries


def test_get_loop_returns_matching_entry(tmp_path):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [
        {"name": "gitlab-issue-loop", "entry_point": "bin.gitlab_loop_runner"},
        {"name": "topic-monitor", "entry_point": "bin.topic_monitor_runner"},
    ])

    loop = lc.get_loop("topic-monitor", config_path=config_path)

    assert loop["entry_point"] == "bin.topic_monitor_runner"


def test_get_loop_unknown_name_raises_key_error(tmp_path):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [{"name": "gitlab-issue-loop", "entry_point": "bin.gitlab_loop_runner"}])

    with pytest.raises(KeyError):
        lc.get_loop("nonexistent", config_path=config_path)


def test_cli_names_lists_every_loop_name(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [
        {"name": "gitlab-issue-loop", "entry_point": "bin.gitlab_loop_runner"},
        {"name": "topic-monitor", "entry_point": "bin.topic_monitor_runner"},
    ])
    monkeypatch.setattr(lc, "DEFAULT_CONFIG_PATH", config_path)
    monkeypatch.setattr(sys, "argv", ["loops_config.py", "names"])

    lc.main()

    assert capsys.readouterr().out == "gitlab-issue-loop\ntopic-monitor\n"


def test_cli_entry_point_prints_the_named_loops_entry_point(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [{"name": "topic-monitor", "entry_point": "bin.topic_monitor_runner"}])
    monkeypatch.setattr(lc, "DEFAULT_CONFIG_PATH", config_path)
    monkeypatch.setattr(sys, "argv", ["loops_config.py", "entry-point", "topic-monitor"])

    lc.main()

    assert capsys.readouterr().out == "bin.topic_monitor_runner\n"


def test_cli_emit_run_events_prints_true_or_false(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [
        {"name": "gitlab-issue-loop", "entry_point": "x", "emit_run_events": True},
        {"name": "topic-monitor", "entry_point": "y", "emit_run_events": False},
    ])
    monkeypatch.setattr(lc, "DEFAULT_CONFIG_PATH", config_path)

    monkeypatch.setattr(sys, "argv", ["loops_config.py", "emit-run-events", "gitlab-issue-loop"])
    lc.main()
    assert capsys.readouterr().out == "true\n"

    monkeypatch.setattr(sys, "argv", ["loops_config.py", "emit-run-events", "topic-monitor"])
    lc.main()
    assert capsys.readouterr().out == "false\n"
