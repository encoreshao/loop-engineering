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
        {"name": "gitlab-loop", "entry_point": "bin.gitlab_loop_runner"},
        {"name": "topic-loop", "entry_point": "bin.topic_monitor_runner"},
    ]
    _write_registry(config_path, entries)

    assert lc.list_loops(config_path=config_path) == entries


def test_get_loop_returns_matching_entry(tmp_path):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [
        {"name": "gitlab-loop", "entry_point": "bin.gitlab_loop_runner"},
        {"name": "topic-loop", "entry_point": "bin.topic_monitor_runner"},
    ])

    loop = lc.get_loop("topic-loop", config_path=config_path)

    assert loop["entry_point"] == "bin.topic_monitor_runner"


def test_get_loop_unknown_name_raises_key_error(tmp_path):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [{"name": "gitlab-loop", "entry_point": "bin.gitlab_loop_runner"}])

    with pytest.raises(KeyError):
        lc.get_loop("nonexistent", config_path=config_path)


def test_get_loop_unsafe_name_raises_value_error(tmp_path):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [{"name": "gitlab-loop", "entry_point": "bin.gitlab_loop_runner"}])

    with pytest.raises(ValueError):
        lc.get_loop("../../etc/passwd", config_path=config_path)


def test_cli_names_lists_every_loop_name(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [
        {"name": "gitlab-loop", "entry_point": "bin.gitlab_loop_runner"},
        {"name": "topic-loop", "entry_point": "bin.topic_monitor_runner"},
    ])
    monkeypatch.setattr(lc, "DEFAULT_CONFIG_PATH", config_path)
    monkeypatch.setattr(sys, "argv", ["loops_config.py", "names"])

    lc.main()

    assert capsys.readouterr().out == "gitlab-loop\ntopic-loop\n"


def test_cli_entry_point_prints_the_named_loops_entry_point(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [{"name": "topic-loop", "entry_point": "bin.topic_monitor_runner"}])
    monkeypatch.setattr(lc, "DEFAULT_CONFIG_PATH", config_path)
    monkeypatch.setattr(sys, "argv", ["loops_config.py", "entry-point", "topic-loop"])

    lc.main()

    assert capsys.readouterr().out == "bin.topic_monitor_runner\n"


def test_cli_bash_env_prints_four_bash_assignments(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [
        {
            "name": "gitlab-loop",
            "entry_point": "bin.gitlab_loop_runner",
            "timeout_seconds": 21600,
            "log_suffix": "",
            "emit_run_events": True,
        },
    ])
    monkeypatch.setattr(lc, "DEFAULT_CONFIG_PATH", config_path)
    monkeypatch.setattr(sys, "argv", ["loops_config.py", "bash-env", "gitlab-loop"])

    lc.main()

    out = capsys.readouterr().out
    assert out == (
        "ENTRY_POINT=bin.gitlab_loop_runner\n"
        "TIMEOUT_SECONDS=21600\n"
        "LOG_SUFFIX=''\n"
        "EMIT_RUN_EVENTS=true\n"
    )


def test_cli_bash_env_unknown_loop_exits_nonzero_with_stderr_message(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [{"name": "gitlab-loop", "entry_point": "bin.gitlab_loop_runner"}])
    monkeypatch.setattr(lc, "DEFAULT_CONFIG_PATH", config_path)
    monkeypatch.setattr(sys, "argv", ["loops_config.py", "bash-env", "nonexistent"])

    with pytest.raises(SystemExit) as exc_info:
        lc.main()

    assert exc_info.value.code != 0
    assert capsys.readouterr().err.strip() != ""


def test_cli_emit_run_events_prints_true_or_false(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [
        {"name": "gitlab-loop", "entry_point": "x", "emit_run_events": True},
        {"name": "topic-loop", "entry_point": "y", "emit_run_events": False},
    ])
    monkeypatch.setattr(lc, "DEFAULT_CONFIG_PATH", config_path)

    monkeypatch.setattr(sys, "argv", ["loops_config.py", "emit-run-events", "gitlab-loop"])
    lc.main()
    assert capsys.readouterr().out == "true\n"

    monkeypatch.setattr(sys, "argv", ["loops_config.py", "emit-run-events", "topic-loop"])
    lc.main()
    assert capsys.readouterr().out == "false\n"


def test_set_enabled_flips_the_matching_entrys_enabled_field(tmp_path):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [
        {"name": "gitlab-loop", "entry_point": "bin.gitlab_loop_runner", "enabled": True},
        {"name": "topic-loop", "entry_point": "bin.topic_monitor_runner", "enabled": True},
    ])

    ok, message = lc.set_enabled("topic-loop", False, config_path=config_path)

    assert ok is True
    assert "topic-loop" in message
    loops = lc.list_loops(config_path=config_path)
    assert [loop["enabled"] for loop in loops] == [True, False]


def test_set_enabled_unknown_loop_returns_false_without_writing(tmp_path):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [{"name": "gitlab-loop", "entry_point": "x", "enabled": True}])
    before = config_path.read_text()

    ok, message = lc.set_enabled("nonexistent", False, config_path=config_path)

    assert ok is False
    assert "nonexistent" in message
    assert config_path.read_text() == before


def test_set_schedule_writes_a_valid_daily_schedule(tmp_path):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [{"name": "topic-loop", "entry_point": "x"}])

    ok, message = lc.set_schedule(
        "topic-loop", {"frequency": "daily", "hour": 9, "minute": 30}, config_path=config_path,
    )

    assert ok is True
    loop = lc.get_loop("topic-loop", config_path=config_path)
    assert loop["schedule"] == {"frequency": "daily", "hour": 9, "minute": 30}


def test_set_schedule_writes_a_valid_weekly_schedule(tmp_path):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [{"name": "gitlab-loop", "entry_point": "x"}])

    ok, message = lc.set_schedule(
        "gitlab-loop", {"frequency": "weekly", "weekdays": [1, 2, 3, 4, 5], "hour": 10, "minute": 0},
        config_path=config_path,
    )

    assert ok is True
    loop = lc.get_loop("gitlab-loop", config_path=config_path)
    assert loop["schedule"]["weekdays"] == [1, 2, 3, 4, 5]


def test_set_schedule_writes_a_valid_monthly_schedule(tmp_path):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [{"name": "topic-loop", "entry_point": "x"}])

    ok, message = lc.set_schedule(
        "topic-loop", {"frequency": "monthly", "day": 31, "hour": 9, "minute": 0}, config_path=config_path,
    )

    assert ok is True
    loop = lc.get_loop("topic-loop", config_path=config_path)
    assert loop["schedule"] == {"frequency": "monthly", "day": 31, "hour": 9, "minute": 0}


def test_set_schedule_writes_a_valid_hourly_schedule(tmp_path):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [{"name": "topic-loop", "entry_point": "x"}])

    ok, message = lc.set_schedule(
        "topic-loop", {"frequency": "hourly", "interval_hours": 4}, config_path=config_path,
    )

    assert ok is True
    loop = lc.get_loop("topic-loop", config_path=config_path)
    assert loop["schedule"] == {"frequency": "hourly", "interval_hours": 4}


def test_set_schedule_rejects_unknown_frequency(tmp_path):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [{"name": "topic-loop", "entry_point": "x"}])

    ok, message = lc.set_schedule("topic-loop", {"frequency": "yearly"}, config_path=config_path)

    assert ok is False
    assert "yearly" in message


def test_set_schedule_rejects_out_of_range_hour(tmp_path):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [{"name": "topic-loop", "entry_point": "x"}])

    ok, message = lc.set_schedule(
        "topic-loop", {"frequency": "daily", "hour": 24, "minute": 0}, config_path=config_path,
    )

    assert ok is False


def test_set_schedule_rejects_out_of_range_day_of_month(tmp_path):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [{"name": "topic-loop", "entry_point": "x"}])

    ok, message = lc.set_schedule(
        "topic-loop", {"frequency": "monthly", "day": 32, "hour": 9, "minute": 0}, config_path=config_path,
    )

    assert ok is False


def test_set_schedule_rejects_out_of_range_interval_hours(tmp_path):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [{"name": "topic-loop", "entry_point": "x"}])

    ok, message = lc.set_schedule(
        "topic-loop", {"frequency": "hourly", "interval_hours": 0}, config_path=config_path,
    )

    assert ok is False


def test_set_schedule_unknown_loop_returns_false_without_writing(tmp_path):
    config_path = tmp_path / "loops.json"
    _write_registry(config_path, [{"name": "topic-loop", "entry_point": "x"}])
    before = config_path.read_text()

    ok, message = lc.set_schedule(
        "nonexistent", {"frequency": "daily", "hour": 9, "minute": 0}, config_path=config_path,
    )

    assert ok is False
    assert config_path.read_text() == before
