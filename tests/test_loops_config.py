import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import loops_config as lc

REPO_ROOT = Path(__file__).resolve().parent.parent
_REAL_TEMPLATE_PATH = lc.TEMPLATE_PATH


@pytest.fixture(autouse=True)
def _no_template_backfill(tmp_path, monkeypatch):
    # Keep the exact-registry tests below independent of whatever loops
    # the real config/loops.json.template happens to register today.
    monkeypatch.setattr(lc, "TEMPLATE_PATH", tmp_path / "no-template.json")


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


def test_template_registers_inbox_triage_loop_disabled():
    entries = json.loads((REPO_ROOT / "config" / "loops.json.template").read_text())
    entry = next(e for e in entries if e["name"] == "inbox-triage-loop")
    assert entry["entry_point"] == "bin.inbox_triage_runner"
    assert entry["enabled"] is False
    assert entry["schedule"] == {"frequency": "weekly", "weekdays": [1, 2, 3, 4, 5], "hour": 9, "minute": 0}
    assert entry["log_suffix"] == "-inbox-triage-loop"


def test_list_loops_backfills_template_loops_missing_from_an_older_registry(tmp_path):
    # An install whose loops.json predates a newly shipped loop (e.g.
    # inbox-triage-loop) must still see it, or run-loop-now.sh fails with
    # "No loop named ... in the loops registry".
    config_path = tmp_path / "loops.json"
    template_path = tmp_path / "template.json"
    existing = [{"name": "gitlab-loop", "entry_point": "bin.gitlab_loop_runner", "enabled": False}]
    _write_registry(config_path, existing)
    _write_registry(template_path, [
        {"name": "gitlab-loop", "entry_point": "bin.gitlab_loop_runner", "enabled": True},
        {"name": "inbox-triage-loop", "entry_point": "bin.inbox_triage_runner", "enabled": False},
    ])

    loops = lc.list_loops(config_path=config_path, template_path=template_path)

    assert [l["name"] for l in loops] == ["gitlab-loop", "inbox-triage-loop"]
    assert loops[0]["enabled"] is False  # the user's own entry wins
    assert lc.get_loop("inbox-triage-loop", config_path=config_path, template_path=template_path)["entry_point"] == "bin.inbox_triage_runner"


def test_set_enabled_persists_a_backfilled_template_loop(tmp_path, monkeypatch):
    config_path = tmp_path / "loops.json"
    template_path = tmp_path / "template.json"
    _write_registry(config_path, [{"name": "gitlab-loop", "entry_point": "bin.gitlab_loop_runner"}])
    _write_registry(template_path, [{"name": "inbox-triage-loop", "entry_point": "bin.inbox_triage_runner", "enabled": False}])
    monkeypatch.setattr(lc, "TEMPLATE_PATH", template_path)

    ok, _ = lc.set_enabled("inbox-triage-loop", True, config_path=config_path)

    assert ok is True
    saved = json.loads(config_path.read_text())
    assert [e["name"] for e in saved] == ["gitlab-loop", "inbox-triage-loop"]
    assert saved[1]["enabled"] is True


def test_real_template_is_used_by_default():
    assert _REAL_TEMPLATE_PATH == REPO_ROOT / "config" / "loops.json.template"


def test_set_notify_writes_ids(tmp_path):
    p = tmp_path / "loops.json"
    _write_registry(p, [{"name": "gitlab-loop", "entry_point": "x"}])
    ok, _ = lc.set_notify("gitlab-loop", ["feishu-team"], config_path=p, template_path=tmp_path / "none")
    assert ok is True
    assert lc.get_loop("gitlab-loop", config_path=p, template_path=tmp_path / "none")["notify"] == ["feishu-team"]


def test_set_notify_rejects_invalid_id_without_writing(tmp_path):
    p = tmp_path / "loops.json"
    _write_registry(p, [{"name": "gitlab-loop", "entry_point": "x"}])
    before = p.read_text()
    for bad in (["Bad Id"], [""], [5], ["-x"], ["a" * 49]):
        ok, msg = lc.set_notify("gitlab-loop", bad, config_path=p, template_path=tmp_path / "none")
        assert ok is False and msg
    assert p.read_text() == before


def test_set_notify_unknown_loop(tmp_path):
    p = tmp_path / "loops.json"
    _write_registry(p, [{"name": "gitlab-loop", "entry_point": "x"}])
    before = p.read_text()
    ok, msg = lc.set_notify("nope", ["a"], config_path=p, template_path=tmp_path / "none")
    assert ok is False and "nope" in msg and p.read_text() == before


def test_set_notify_empty_list_removes_key(tmp_path):
    p = tmp_path / "loops.json"
    _write_registry(p, [{"name": "gitlab-loop", "entry_point": "x", "notify": ["a"]}])
    ok, _ = lc.set_notify("gitlab-loop", [], config_path=p, template_path=tmp_path / "none")
    assert ok is True
    assert "notify" not in json.loads(p.read_text())[0]


def test_replace_notify_id_removes_a_deleted_connector_everywhere(tmp_path):
    p = tmp_path / "loops.json"
    _write_registry(p, [{"name": "a", "notify": ["x", "y"]}, {"name": "b", "notify": ["x"]},
                        {"name": "c", "enabled": True}])
    assert lc.replace_notify_id("x", config_path=p) == 2
    assert json.loads(p.read_text()) == [{"name": "a", "notify": ["y"]}, {"name": "b"},
                                         {"name": "c", "enabled": True}]


def test_replace_notify_id_renames_without_duplicates(tmp_path):
    p = tmp_path / "loops.json"
    _write_registry(p, [{"name": "a", "notify": ["x", "y"]}, {"name": "b", "notify": ["x", "z"]}])
    assert lc.replace_notify_id("x", "z", config_path=p) == 2
    assert json.loads(p.read_text()) == [{"name": "a", "notify": ["z", "y"]}, {"name": "b", "notify": ["z"]}]


def test_replace_notify_id_no_match_or_missing_file_writes_nothing(tmp_path):
    p = tmp_path / "loops.json"
    assert lc.replace_notify_id("x", config_path=p) == 0
    assert not p.exists()
    _write_registry(p, [{"name": "a", "notify": ["y"]}])
    before = p.stat().st_mtime_ns
    assert lc.replace_notify_id("x", config_path=p) == 0
    assert p.stat().st_mtime_ns == before


def test_set_settings_only_declared_keys(tmp_path):
    p = tmp_path / "loops.json"
    p.write_text('[{"name": "rss-watch-loop", "entry_point": "x", "timeout_seconds": 1}]')
    ok, _ = lc.set_settings("rss-watch-loop", {"interests": "rails", "evil": "x"},
                                      allowed_keys=("interests",), config_path=p, template_path=tmp_path / "none")
    assert ok and json.loads(p.read_text())[0]["settings"] == {"interests": "rails"}


def test_set_settings_limits_and_unknown_loop(tmp_path):
    p = tmp_path / "loops.json"
    p.write_text('[{"name": "a", "entry_point": "x", "timeout_seconds": 1, "settings": {"keep": "1"}}]')
    ok, _ = lc.set_settings("a", {"interests": "x" * 2000, "n": 5}, allowed_keys=("interests", "n"),
                                      config_path=p, template_path=tmp_path / "none")
    saved = json.loads(p.read_text())[0]["settings"]
    assert ok and len(saved["interests"]) == 1000 and "n" not in saved and saved["keep"] == "1"
    before = p.read_text()
    ok, _ = lc.set_settings("nope", {"interests": "x"}, allowed_keys=("interests",),
                                      config_path=p, template_path=tmp_path / "none")
    assert not ok and p.read_text() == before
