import contextlib
import fcntl
import html
import http.client
import json
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin" / "web"))
import dashboard_server as ds  # noqa: E402
import ai_cli_config  # noqa: E402
import events  # noqa: E402
import loop_config  # noqa: E402
import topic_config  # noqa: E402

# Captured at import time, before any test monkeypatches ds.LAUNCHD_DIR, so
# the isolation tests below can assert the real repo directory is never
# reached even while ds.LAUNCHD_DIR points somewhere else.
REAL_LAUNCHD_DIR = ds.LAUNCHD_DIR


def test_read_status_missing_file_returns_never_run(tmp_path):
    status_path = tmp_path / "status.json"

    assert ds.read_status(status_path) == {"state": "never_run"}


def test_read_status_corrupt_json_returns_unknown(tmp_path):
    status_path = tmp_path / "status.json"
    status_path.write_text("{not valid json")

    assert ds.read_status(status_path) == {"state": "unknown"}


def test_write_status_then_read_status_round_trips(tmp_path):
    status_path = tmp_path / "nested" / "status.json"

    written = ds.write_status("idle", status_path=status_path, last_exit_code=1)

    assert written["state"] == "idle"
    assert written["last_exit_code"] == 1
    assert "updated_at" in written

    read_back = ds.read_status(status_path)
    assert read_back == written


def test_read_topic_status_missing_file_returns_empty_topics(tmp_path):
    missing = tmp_path / "status.json"

    assert ds.read_topic_status(missing) == {"topics": {}}


def test_read_topic_status_corrupt_json_returns_empty_topics(tmp_path):
    path = tmp_path / "status.json"
    path.write_text("not json")

    assert ds.read_topic_status(path) == {"topics": {}}


def test_read_topic_status_default_arg_honors_monkeypatched_constant(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(ds, "TOPIC_MONITOR_STATUS_PATH", status_path)
    ds.write_topic_status("ai-news", "idle", status_path=status_path)

    # No status_path argument - must resolve TOPIC_MONITOR_STATUS_PATH fresh
    # at call time, not from a def-time-bound default, or this would read
    # from the real module constant's path instead of tmp_path.
    data = ds.read_topic_status()

    assert data["topics"]["ai-news"]["state"] == "idle"


def test_write_topic_status_then_read_topic_status_round_trips(tmp_path):
    status_path = tmp_path / "status.json"

    written = ds.write_topic_status("ai-news", "idle", status_path=status_path, current_step="done")

    assert written["topics"]["ai-news"]["state"] == "idle"
    assert written["topics"]["ai-news"]["current_step"] == "done"
    assert "updated_at" in written["topics"]["ai-news"]
    assert ds.read_topic_status(status_path) == written


def test_write_topic_status_preserves_other_topics(tmp_path):
    status_path = tmp_path / "status.json"
    ds.write_topic_status("ai-news", "idle", status_path=status_path)

    ds.write_topic_status("rust-lang", "running", status_path=status_path)

    data = ds.read_topic_status(status_path)
    assert data["topics"]["ai-news"]["state"] == "idle"
    assert data["topics"]["rust-lang"]["state"] == "running"


def test_main_write_topic_status_writes_expected_json(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(ds, "TOPIC_MONITOR_STATUS_PATH", status_path)
    monkeypatch.setattr(sys, "argv", ["dashboard_server.py", "write-topic-status", "ai-news", "running", "--current-step", "researching"])

    ds.main()

    data = ds.read_topic_status(status_path)
    assert data["topics"]["ai-news"]["state"] == "running"
    assert data["topics"]["ai-news"]["current_step"] == "researching"


def test_list_run_history_sorted_descending_ignores_non_md(tmp_path):
    (tmp_path / "2026-08-01.md").write_text("a")
    (tmp_path / "2026-08-03.md").write_text("b")
    (tmp_path / "2026-08-02.md").write_text("c")
    (tmp_path / "2026-08-01.log").write_text("d")
    (tmp_path / "notes.txt").write_text("e")

    result = ds.list_run_history(tmp_path)

    assert result == ["2026-08-03.md", "2026-08-02.md", "2026-08-01.md"]


def test_list_run_history_missing_dir_returns_empty_list(tmp_path):
    missing = tmp_path / "does-not-exist"

    assert ds.list_run_history(missing) == []


def test_list_topic_history_missing_dir_returns_empty_list(tmp_path):
    missing = tmp_path / "does-not-exist"

    assert ds.list_topic_history(history_dir=missing) == []


def test_list_topic_history_sorted_descending(tmp_path):
    (tmp_path / "2026-08-20-ai-news.md").write_text("a")
    (tmp_path / "2026-08-22-ai-news.md").write_text("b")
    (tmp_path / "2026-08-21-ai-news.md").write_text("c")

    names = ds.list_topic_history(history_dir=tmp_path)

    assert names == ["2026-08-22-ai-news.md", "2026-08-21-ai-news.md", "2026-08-20-ai-news.md"]


def test_list_topic_history_filters_by_topic_name(tmp_path):
    (tmp_path / "2026-08-22-ai-news.md").write_text("a")
    (tmp_path / "2026-08-22-rust-lang.md").write_text("b")

    names = ds.list_topic_history("rust-lang", history_dir=tmp_path)

    assert names == ["2026-08-22-rust-lang.md"]


def test_list_topic_history_does_not_match_a_longer_topic_name_suffix(tmp_path):
    """Filenames are <date>-<topic_name>.md, so a plain endswith() filter for
    topic "news" also matches "ai-news"'s files. Overlapping topic-name
    suffixes (news/ai-news, rust/async-rust) are a realistic topics.json."""
    (tmp_path / "2026-08-22-news.md").write_text("a")
    (tmp_path / "2026-08-22-ai-news.md").write_text("b")

    assert ds.list_topic_history("news", history_dir=tmp_path) == ["2026-08-22-news.md"]
    assert ds.list_topic_history("ai-news", history_dir=tmp_path) == ["2026-08-22-ai-news.md"]


def test_read_history_file_returns_real_content(tmp_path):
    (tmp_path / "2026-08-01.md").write_text("# Daily Review\nhello")

    content = ds.read_history_file("2026-08-01.md", tmp_path)

    assert content == "# Daily Review\nhello"


def test_read_history_file_rejects_path_traversal(tmp_path):
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    secret = tmp_path / "etc_passwd_stand_in.md"
    secret.write_text("TOP SECRET CONTENT")

    result = ds.read_history_file("../etc_passwd_stand_in.md", history_dir)

    assert result is None

    # Also prove a deep traversal attempt targeting a real absolute path
    # (mimicking ../../../etc/passwd) can't escape either.
    deep_result = ds.read_history_file("../../../../etc/passwd", history_dir)
    assert deep_result is None


def test_read_history_file_returns_none_for_missing_file(tmp_path):
    assert ds.read_history_file("nope.md", tmp_path) is None


def test_read_history_file_returns_none_for_non_md_suffix(tmp_path):
    (tmp_path / "secret.txt").write_text("data")

    assert ds.read_history_file("secret.txt", tmp_path) is None


def test_get_project_learnings_returns_empty_dict_when_no_config(tmp_path, monkeypatch):
    missing_config = tmp_path / "does-not-exist" / "projects.json"

    assert ds.get_project_learnings(config_path=missing_config) == {}


def test_get_project_learnings_resolves_instance_per_project(tmp_path, monkeypatch):
    config_path = tmp_path / "projects.json"
    config_path.write_text(json.dumps({
        "gitlab_instance": "acme",
        "assignee_username": "encore",
        "worktree_root": "/tmp/wt",
        "projects": {
            "harbor": {"project_id": "acme/harbor"},
            "other-org-project": {"project_id": "other-org/some-project", "instance": "other-gitlab"},
        },
    }))
    calls = []

    def fake_get_learnings(instance, project_id):
        calls.append((instance, project_id))
        return []

    monkeypatch.setattr(ds.project_memory, "get_learnings", fake_get_learnings)

    ds.get_project_learnings(config_path=config_path)

    assert ("acme", "acme/harbor") in calls
    assert ("other-gitlab", "other-org/some-project") in calls


def test_read_loop_projects_config_returns_empty_dict_when_missing(tmp_path):
    missing = tmp_path / "does-not-exist" / "projects.json"

    assert ds.read_loop_projects_config(missing) == {}


def test_read_loop_projects_config_returns_empty_dict_when_malformed(tmp_path):
    path = tmp_path / "projects.json"
    path.write_text("not json")

    assert ds.read_loop_projects_config(path) == {}


def test_write_loop_projects_config_round_trips(tmp_path):
    path = tmp_path / "nested" / "projects.json"

    ds.write_loop_projects_config({"assignee_username": "encore"}, path)

    assert ds.read_loop_projects_config(path) == {"assignee_username": "encore"}


def test_upsert_tracked_project_adds_new_project(tmp_path):
    config_path = tmp_path / "projects.json"
    ds.write_loop_projects_config({
        "gitlab_instance": "acme", "assignee_username": "encore", "worktree_root": "/tmp/wt", "projects": {},
    }, config_path)

    ok, message = ds.upsert_tracked_project(
        "harbor", "acme/harbor", "/tmp/harbor", "staging", "npm ci", "npm run lint", "npm run test",
        instance="", config_path=config_path,
    )

    assert ok is True
    assert "Added" in message
    project = ds.read_loop_projects_config(config_path)["projects"]["harbor"]
    assert project == {
        "project_id": "acme/harbor",
        "local_path": "/tmp/harbor",
        "target_branch": "staging",
        "install_cmd": "npm ci",
        "lint_cmd": "npm run lint",
        "test_cmd": "npm run test",
    }


def test_upsert_tracked_project_stores_instance_override_when_given(tmp_path):
    config_path = tmp_path / "projects.json"
    ds.write_loop_projects_config({
        "gitlab_instance": "acme", "assignee_username": "encore", "worktree_root": "/tmp/wt", "projects": {},
    }, config_path)

    ds.upsert_tracked_project(
        "other-org-project", "other-org/some-project", "/tmp/x", "main", "npm ci", "npm run lint", "npm run test",
        instance="other-gitlab", config_path=config_path,
    )

    project = ds.read_loop_projects_config(config_path)["projects"]["other-org-project"]
    assert project["instance"] == "other-gitlab"


def test_upsert_tracked_project_updates_existing_and_can_clear_instance_override(tmp_path):
    config_path = tmp_path / "projects.json"
    ds.write_loop_projects_config({
        "gitlab_instance": "acme", "assignee_username": "encore", "worktree_root": "/tmp/wt",
        "projects": {"harbor": {"project_id": "acme/harbor", "instance": "other-gitlab"}},
    }, config_path)

    ok, message = ds.upsert_tracked_project(
        "harbor", "acme/harbor", "/tmp/harbor", "staging", "npm ci", "npm run lint", "npm run test",
        instance="", config_path=config_path,
    )

    assert ok is True
    assert "Updated" in message
    project = ds.read_loop_projects_config(config_path)["projects"]["harbor"]
    assert "instance" not in project


def test_upsert_tracked_project_renames_existing_entry(tmp_path):
    config_path = tmp_path / "projects.json"
    ds.write_loop_projects_config({
        "projects": {"harbor": {"project_id": "acme/harbor", "instance": "other-gitlab"}},
    }, config_path)

    ok, message = ds.upsert_tracked_project(
        "harbor-renamed", "acme/harbor", "/tmp/harbor", "staging", "npm ci", "npm run lint", "npm run test",
        instance="other-gitlab", config_path=config_path, original_alias="harbor",
    )

    assert ok is True
    assert "harbor" in message and "harbor-renamed" in message
    projects = ds.read_loop_projects_config(config_path)["projects"]
    assert "harbor" not in projects
    assert projects["harbor-renamed"]["project_id"] == "acme/harbor"


def test_upsert_tracked_project_rename_rejects_unknown_original_alias(tmp_path):
    config_path = tmp_path / "projects.json"
    ds.write_loop_projects_config({"projects": {}}, config_path)

    ok, message = ds.upsert_tracked_project(
        "harbor-renamed", "acme/harbor", "", "", "", "", "",
        config_path=config_path, original_alias="no-such-alias",
    )

    assert ok is False
    assert "Unknown project" in message
    assert ds.read_loop_projects_config(config_path)["projects"] == {}


def test_upsert_tracked_project_rename_rejects_alias_already_in_use(tmp_path):
    config_path = tmp_path / "projects.json"
    ds.write_loop_projects_config({
        "projects": {
            "harbor": {"project_id": "acme/harbor"},
            "vault": {"project_id": "acme/vault"},
        },
    }, config_path)

    ok, message = ds.upsert_tracked_project(
        "vault", "acme/harbor", "", "", "", "", "",
        config_path=config_path, original_alias="harbor",
    )

    assert ok is False
    assert "already in use" in message.lower()
    projects = ds.read_loop_projects_config(config_path)["projects"]
    assert projects["harbor"]["project_id"] == "acme/harbor"
    assert projects["vault"]["project_id"] == "acme/vault"


def test_upsert_tracked_project_original_alias_equal_to_alias_is_plain_update(tmp_path):
    config_path = tmp_path / "projects.json"
    ds.write_loop_projects_config({
        "projects": {"harbor": {"project_id": "acme/harbor"}},
    }, config_path)

    ok, message = ds.upsert_tracked_project(
        "harbor", "acme/harbor-2", "", "", "", "", "",
        config_path=config_path, original_alias="harbor",
    )

    assert ok is True
    assert "Updated" in message
    projects = ds.read_loop_projects_config(config_path)["projects"]
    assert projects["harbor"]["project_id"] == "acme/harbor-2"


def test_upsert_tracked_project_requires_alias_and_project_id(tmp_path):
    config_path = tmp_path / "projects.json"
    ds.write_loop_projects_config({"projects": {}}, config_path)

    ok, message = ds.upsert_tracked_project("", "acme/harbor", "", "", "", "", "", config_path=config_path)
    assert ok is False
    assert "alias" in message.lower()

    ok, message = ds.upsert_tracked_project("harbor", "", "", "", "", "", "", config_path=config_path)
    assert ok is False
    assert "project" in message.lower()


def test_delete_tracked_project_removes_entry(tmp_path):
    config_path = tmp_path / "projects.json"
    ds.write_loop_projects_config({"projects": {"harbor": {"project_id": "acme/harbor"}}}, config_path)

    ok, message = ds.delete_tracked_project("harbor", config_path)

    assert ok is True
    assert ds.read_loop_projects_config(config_path)["projects"] == {}


def test_delete_tracked_project_unknown_alias_fails(tmp_path):
    config_path = tmp_path / "projects.json"
    ds.write_loop_projects_config({"projects": {}}, config_path)

    ok, message = ds.delete_tracked_project("no-such-alias", config_path)

    assert ok is False
    assert "Unknown" in message


def test_update_loop_project_settings_saves_all_three_fields(tmp_path):
    config_path = tmp_path / "projects.json"
    gitlab_config_path = tmp_path / "gitlab.json"
    gitlab_config_path.write_text(json.dumps({"instances": {"acme": {"url": "https://gitlab.acme.com"}}}))
    ds.write_loop_projects_config({"projects": {}}, config_path)

    ok, message = ds.update_loop_project_settings(
        "encore", "/tmp/worktrees", "acme", config_path=config_path, gitlab_config_path=gitlab_config_path,
    )

    assert ok is True
    config = ds.read_loop_projects_config(config_path)
    assert config["assignee_username"] == "encore"
    assert config["worktree_root"] == "/tmp/worktrees"
    assert config["gitlab_instance"] == "acme"
    assert config["projects"] == {}


def test_update_loop_project_settings_rejects_unknown_instance(tmp_path):
    config_path = tmp_path / "projects.json"
    gitlab_config_path = tmp_path / "gitlab.json"
    gitlab_config_path.write_text(json.dumps({"instances": {"acme": {"url": "https://gitlab.acme.com"}}}))
    ds.write_loop_projects_config({"projects": {}}, config_path)

    ok, message = ds.update_loop_project_settings(
        "encore", "/tmp/worktrees", "bogus", config_path=config_path, gitlab_config_path=gitlab_config_path,
    )

    assert ok is False
    assert "Unknown instance" in message


def test_get_configured_topics_returns_empty_list_when_no_config(tmp_path):
    missing = tmp_path / "topics.json"

    assert ds.get_configured_topics(missing) == []


def test_get_configured_topics_returns_topics_from_file(tmp_path):
    config_path = tmp_path / "topics.json"
    config_path.write_text(json.dumps([{"name": "ai-news", "label": "AI news", "brief": "x", "slack_bundle": None}]))

    topics = ds.get_configured_topics(config_path)

    assert topics == [{"name": "ai-news", "label": "AI news", "brief": "x", "slack_bundle": None}]


def test_get_configured_topics_returns_empty_list_on_malformed_shape(tmp_path):
    config_path = tmp_path / "topics.json"
    config_path.write_text(json.dumps({"not": "a list"}))

    assert ds.get_configured_topics(config_path) == []


def test_get_live_gitlab_state_returns_empty_dict_when_no_config(tmp_path):
    missing_config = tmp_path / "does-not-exist" / "projects.json"

    assert ds.get_live_gitlab_state(config_path=missing_config) == {}


def test_get_live_gitlab_state_sets_error_when_subprocess_fails(tmp_path, monkeypatch):
    config_path = tmp_path / "projects.json"
    config_path.write_text(json.dumps({
        "assignee_username": "encore",
        "projects": {"myproj": {"project_id": "a/b"}},
    }))

    def fake_run(alias, subcommand):
        raise subprocess.CalledProcessError(1, ["gitlab_api.py"], stderr="Error: something broke\n")

    monkeypatch.setattr(ds, "_run_gitlab_api", fake_run)

    state = ds.get_live_gitlab_state(config_path=config_path)

    assert state["myproj"]["issues"] == []
    assert state["myproj"]["issues_error"] == "Error: something broke"
    assert state["myproj"]["mrs"] == []
    assert state["myproj"]["mrs_error"] == "Error: something broke"


def test_get_live_gitlab_state_fetches_aliases_concurrently(tmp_path, monkeypatch):
    """Regression test for the "Live GitLab is slow" complaint: with N
    aliases each taking ~0.2s per call, sequential fetching would take
    roughly N * 2 * 0.2s. Concurrent fetching should take roughly one
    alias's worth of time, not the sum of all of them."""
    import time

    config_path = tmp_path / "projects.json"
    config_path.write_text(json.dumps({
        "assignee_username": "encore",
        "projects": {f"proj{i}": {"project_id": f"a/proj{i}"} for i in range(5)},
    }))

    def slow_run(alias, subcommand):
        time.sleep(0.2)
        return []

    monkeypatch.setattr(ds, "_run_gitlab_api", slow_run)

    started = time.monotonic()
    state = ds.get_live_gitlab_state(config_path=config_path)
    elapsed = time.monotonic() - started

    assert len(state) == 5
    # Sequential would be 5 aliases * 2 calls * 0.2s = 2.0s; concurrent
    # should land close to one alias's own 2 calls (~0.4s). 1.0s leaves
    # generous headroom for scheduling jitter while still failing fast if
    # this regresses to sequential.
    assert elapsed < 1.0, f"expected concurrent fetching, took {elapsed:.2f}s"


def test_get_live_gitlab_state_no_error_on_success(tmp_path, monkeypatch):
    config_path = tmp_path / "projects.json"
    config_path.write_text(json.dumps({
        "assignee_username": "encore",
        "projects": {"myproj": {"project_id": "a/b"}},
    }))

    monkeypatch.setattr(ds, "_run_gitlab_api", lambda alias, subcommand: [])

    state = ds.get_live_gitlab_state(config_path=config_path)

    assert state["myproj"]["issues_error"] is None
    assert state["myproj"]["mrs_error"] is None


def test_describe_gitlab_api_error_prefers_stderr_over_generic_message():
    exc = subprocess.CalledProcessError(1, ["gitlab_api.py"], stderr="Error: Instance 'x' not found\n")

    assert ds._describe_gitlab_api_error(exc) == "Error: Instance 'x' not found"


def test_describe_gitlab_api_error_skips_warning_lines():
    exc = subprocess.CalledProcessError(
        1, ["gitlab_api.py"],
        stderr="Warning: some dependency mismatch\n\nModuleNotFoundError: No module named 'requests'\n",
    )

    assert ds._describe_gitlab_api_error(exc) == "ModuleNotFoundError: No module named 'requests'"


def test_describe_gitlab_api_error_falls_back_to_str_when_no_stderr():
    exc = ValueError("boom")

    assert ds._describe_gitlab_api_error(exc) == "boom"


def test_get_daemons_status_missing_dir_returns_empty_list(tmp_path):
    missing = tmp_path / "does-not-exist"

    assert ds.get_daemons_status(missing, launchctl_output="") == []


def test_get_daemons_status_parses_scheduled_plist(tmp_path):
    plist_path = tmp_path / "com.example.scheduled.plist"
    with open(plist_path, "wb") as f:
        plistlib.dump(
            {
                "Label": "com.example.scheduled",
                "ProgramArguments": ["/usr/bin/true", "--flag"],
                "StartCalendarInterval": [
                    {"Weekday": 1, "Hour": 10, "Minute": 0},
                    {"Weekday": 2, "Hour": 10, "Minute": 0},
                ],
                "StandardOutPath": "/tmp/out.log",
                "StandardErrorPath": "/tmp/err.log",
                "RunAtLoad": False,
            },
            f,
        )

    [result] = ds.get_daemons_status(tmp_path, launchctl_output="")

    assert result["file"] == "com.example.scheduled.plist"
    assert result["label"] == "com.example.scheduled"
    assert result["program_arguments"] == ["/usr/bin/true", "--flag"]
    assert result["run_at_load"] is False
    assert result["keep_alive"] is False
    assert result["schedule"] == [
        {"Weekday": 1, "Hour": 10, "Minute": 0},
        {"Weekday": 2, "Hour": 10, "Minute": 0},
    ]
    assert result["stdout_path"] == "/tmp/out.log"
    assert result["stderr_path"] == "/tmp/err.log"
    assert result["loaded"] is False
    assert result["pid"] is None


def test_get_daemons_status_parses_always_on_plist(tmp_path):
    plist_path = tmp_path / "com.example.always-on.plist"
    with open(plist_path, "wb") as f:
        plistlib.dump(
            {
                "Label": "com.example.always-on",
                "ProgramArguments": ["/usr/bin/python3", "server.py"],
                "RunAtLoad": True,
                "KeepAlive": True,
            },
            f,
        )

    [result] = ds.get_daemons_status(tmp_path, launchctl_output="")

    assert result["label"] == "com.example.always-on"
    assert result["program_arguments"] == ["/usr/bin/python3", "server.py"]
    assert result["run_at_load"] is True
    assert result["keep_alive"] is True
    assert result["schedule"] is None


def test_get_daemons_status_reports_loaded_with_pid_from_launchctl_output(tmp_path):
    plist_path = tmp_path / "com.example.always-on.plist"
    with open(plist_path, "wb") as f:
        plistlib.dump(
            {
                "Label": "com.example.always-on",
                "ProgramArguments": ["/usr/bin/python3", "server.py"],
                "RunAtLoad": True,
                "KeepAlive": True,
            },
            f,
        )
    launchctl_output = (
        "PID\tStatus\tLabel\n"
        "12345\t0\tcom.example.always-on\n"
    )

    [result] = ds.get_daemons_status(tmp_path, launchctl_output=launchctl_output)

    assert result["loaded"] is True
    assert result["pid"] == "12345"


def test_get_daemons_status_reports_not_loaded_when_label_absent(tmp_path):
    plist_path = tmp_path / "com.example.always-on.plist"
    with open(plist_path, "wb") as f:
        plistlib.dump(
            {
                "Label": "com.example.always-on",
                "ProgramArguments": ["/usr/bin/python3", "server.py"],
            },
            f,
        )
    launchctl_output = "PID\tStatus\tLabel\n99\t0\tcom.some.other.thing\n"

    [result] = ds.get_daemons_status(tmp_path, launchctl_output=launchctl_output)

    assert result["loaded"] is False
    assert result["pid"] is None


def test_get_daemons_status_handles_malformed_plist_without_raising(tmp_path):
    good_path = tmp_path / "com.example.good.plist"
    with open(good_path, "wb") as f:
        plistlib.dump({"Label": "com.example.good", "ProgramArguments": ["/bin/true"]}, f)
    bad_path = tmp_path / "com.example.bad.plist"
    bad_path.write_text("this is not a plist at all {{{ garbage")

    results = ds.get_daemons_status(tmp_path, launchctl_output="")

    by_file = {r["file"]: r for r in results}
    assert "error" in by_file["com.example.bad.plist"]
    assert by_file["com.example.good.plist"]["label"] == "com.example.good"


def test_get_skills_status_reports_installed_when_present(tmp_path):
    script_path = tmp_path / "skills" / "gitlab-config" / "scripts" / "gitlab_api.py"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("# stub")

    [result] = ds.get_skills_status(tmp_path)

    assert result["key"] == "gitlab-config"
    assert result["installed"] is True
    assert result["path"] == str(script_path)


def test_get_skills_status_reports_missing_when_absent(tmp_path):
    [result] = ds.get_skills_status(tmp_path)

    assert result["key"] == "gitlab-config"
    assert result["installed"] is False


def test_get_skills_status_includes_registry_metadata(tmp_path):
    [result] = ds.get_skills_status(tmp_path)

    assert result["name"]
    assert result["description"]
    assert result["used_by"]
    assert "bin/web/dashboard_server.py" in result["used_by"]


def test_trigger_skills_install_refuses_when_already_installing(tmp_path):
    status_path = tmp_path / "skills_install_status.json"
    ds.write_status("installing", status_path=status_path)

    ok, message = ds.trigger_skills_install(status_path=status_path, setup_script_path=tmp_path / "setup.sh")

    assert not ok
    assert "already in progress" in message


def test_trigger_skills_install_refuses_when_script_missing(tmp_path):
    status_path = tmp_path / "skills_install_status.json"

    ok, message = ds.trigger_skills_install(
        status_path=status_path, setup_script_path=tmp_path / "does-not-exist.sh")

    assert not ok
    assert "not found" in message


def test_trigger_skills_install_launches_background_command(tmp_path, monkeypatch):
    status_path = tmp_path / "skills_install_status.json"
    setup_script_path = tmp_path / "setup.sh"
    setup_script_path.write_text("#!/bin/bash\ntrue\n")
    setup_script_path.chmod(0o755)
    log_path = tmp_path / "skills-install.log"

    captured = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    ok, message = ds.trigger_skills_install(
        status_path=status_path, setup_script_path=setup_script_path,
        log_path=log_path, daemon_label="com.example.dashboard",
    )

    assert ok, message
    assert captured["args"][0] == "bash"
    assert captured["args"][1] == "-c"
    command = captured["args"][2]
    assert str(setup_script_path) in command
    assert str(log_path) in command
    assert "write-skills-install-status" in command
    assert "launchctl kickstart -k" in command
    assert "com.example.dashboard" in command
    assert captured["kwargs"]["start_new_session"] is True


def _set_skills(monkeypatch):
    monkeypatch.setattr(ds, "get_skills_status", lambda *a, **k: [
        {"key": "gitlab-config", "name": "gitlab-config", "description": "Wires up GitLab access.",
         "used_by": ("bin/web/dashboard_server.py",), "installed": True, "path": "/tmp/installed/gitlab_api.py"},
        {"key": "other-skill", "name": "other-skill", "description": "Not installed yet.",
         "used_by": ("bin/other.py",), "installed": False, "path": "/tmp/missing/other.py"},
    ])


def test_render_skills_page_shows_installed_and_missing_pills(monkeypatch, tmp_path):
    _set_skills(monkeypatch)
    monkeypatch.setattr(ds, "SKILLS_INSTALL_STATUS_PATH", tmp_path / "does-not-exist.json")

    output = ds.render_skills_page()

    assert "gitlab-config" in output
    assert "Wires up GitLab access." in output
    assert "bin/web/dashboard_server.py" in output
    assert "<span class='pill pill-green'>" in output
    assert "<span class='pill pill-grey'>not installed</span>" in output
    assert "/tmp/installed/gitlab_api.py" in output


def test_render_skills_page_has_no_used_by_or_path_column_headers(monkeypatch, tmp_path):
    """Used by/Path aren't columns at all - they're not worth a header the
    user sees on every visit just to stay empty until a row is clicked."""
    _set_skills(monkeypatch)
    monkeypatch.setattr(ds, "SKILLS_INSTALL_STATUS_PATH", tmp_path / "does-not-exist.json")

    output = ds.render_skills_page()

    thead = output.split("<thead>")[1].split("</thead>")[0]
    assert "Used by" not in thead
    assert "Path" not in thead
    assert ["Skill", "Status", "What it does"] == re.findall(r"<th>([^<]+)</th>", thead)


def test_render_skills_page_used_by_and_path_hidden_until_row_expanded(monkeypatch, tmp_path):
    """Used by/Path live in a second row directly under the summary row,
    revealed by a CSS sibling rule keyed off `is-expanded` on the summary
    row - not server logic, so this checks the structural hooks that
    toggle relies on."""
    _set_skills(monkeypatch)
    monkeypatch.setattr(ds, "SKILLS_INSTALL_STATUS_PATH", tmp_path / "does-not-exist.json")

    output = ds.render_skills_page()

    assert "class='skill-row'" in output
    assert "class='skill-detail-row'" in output
    assert "toggle('is-expanded')" in output
    assert "table.skills tr.skill-detail-row" in ds._STYLE
    assert "skill-row.is-expanded + tr.skill-detail-row" in ds._STYLE
    # the detail row must immediately follow its own summary row, not just
    # exist somewhere on the page, or the CSS sibling selector can't find it
    assert re.search(r"class='skill-row'[^>]*>.*?</tr>\s*<tr class='skill-detail-row'", output)


def test_render_skills_page_shows_setup_button_for_missing_skill(monkeypatch, tmp_path):
    _set_skills(monkeypatch)
    monkeypatch.setattr(ds, "SKILLS_INSTALL_STATUS_PATH", tmp_path / "does-not-exist.json")

    output = ds.render_skills_page()

    assert "action='/skills/install'" in output
    csrf_input = f"<input type='hidden' name='csrf_token' value=\"{ds._CSRF_TOKEN}\">"
    setup_form = output.split("action='/skills/install'")[1].split("</form>")[0]
    assert csrf_input in setup_form


def test_render_skills_page_hides_setup_button_while_installing(monkeypatch, tmp_path):
    _set_skills(monkeypatch)
    status_path = tmp_path / "skills_install_status.json"
    ds.write_status("installing", status_path=status_path)
    monkeypatch.setattr(ds, "SKILLS_INSTALL_STATUS_PATH", status_path)

    output = ds.render_skills_page()

    assert "action='/skills/install'" not in output
    assert "in progress" in output.lower()
    # the CSS rule for .md-spinner always exists on every page regardless
    # of state - check the icon is actually applied next to the pill
    # text, not just that the class is defined somewhere on the page
    assert ds._SPINNER_ICON + "setup in progress" in output


def test_material_design_3_palette_values():
    assert "--md-primary: #9CC0FC;" in ds._STYLE
    assert "--md-surface: #121416;" in ds._STYLE
    assert "--md-surface-dim: #0E0F11;" in ds._STYLE
    assert "--md-on-surface: #E3E5E8;" in ds._STYLE
    assert "--color-bg" not in ds._STYLE, "old color-bg token must be fully replaced"
    assert "--color-surface:" not in ds._STYLE, "old color-surface token must be fully replaced"
    assert "--color-primary:" not in ds._STYLE, "old color-primary token must be fully replaced"
    assert "--color-text:" not in ds._STYLE, "old color-text token must be fully replaced"


def test_all_named_accent_colors_define_the_four_nav_tokens():
    for accent in ("indigo", "blue", "green", "red", "gray"):
        rule = ds._STYLE.split(f':root[data-accent="{accent}"] {{')[1].split("}")[0]
        assert "--md-nav-surface:" in rule
        assert "--md-nav-on-surface:" in rule
        assert "--md-nav-active-surface:" in rule
        assert "--md-nav-active-on-surface:" in rule
        # fixed, mode-independent washes - never a var() reference to a
        # mode-aware token like the neutral "default" accent uses
        assert "var(" not in rule


def test_default_accent_keeps_the_sidebar_neutral_and_mode_aware():
    """"Default" is the first accent choice and what a fresh install
    starts on - it must not tint the sidebar/topbar at all, unlike every
    named color accent, and must stay mode-aware (var() references)
    since it's meant to look exactly like this app's original design."""
    rule = ds._STYLE.split(':root[data-accent="default"] {')[1].split("}")[0]

    assert "--md-nav-surface: var(--md-surface-container-low);" in rule
    assert "--md-nav-on-surface: var(--md-on-surface-variant);" in rule


def test_sidebar_and_topbar_use_the_accent_nav_surface_token():
    assert ".sidebar {" in ds._STYLE
    sidebar_rule = ds._STYLE.split(".sidebar {")[1].split("}")[0]
    assert "background: var(--md-nav-surface);" in sidebar_rule

    topbar_rule = ds._STYLE.split(".topbar {")[1].split("}")[0]
    assert "background: var(--md-nav-surface);" in topbar_rule


def test_active_nav_item_does_not_collide_with_an_accent_tinted_sidebar():
    """The active nav item must use a background distinct from
    --md-nav-surface (the sidebar's own, now accent-tintable,
    background) or it would disappear into it."""
    active_rule = ds._STYLE.split(".sidebar-nav a.active {")[1].split("}")[0]
    assert "var(--md-nav-surface)" not in active_rule
    assert "--md-nav-active-surface" in active_rule


def test_light_color_mode_defined_for_auto_and_explicit_choice():
    explicit_light_rule = ds._STYLE.split(':root[data-color-mode="light"] {')[1].split("}")[0]
    assert "--md-on-surface: #1C1B1E;" in explicit_light_rule

    auto_light_block = ds._STYLE.split('@media (prefers-color-scheme: light) {')[1]
    assert ':root:not([data-color-mode="dark"]) {' in auto_light_block
    assert "--md-on-surface: #1C1B1E;" in auto_light_block


def test_roboto_is_the_default_font_family():
    """Roboto is the bare :root default (see _FONT_CHOICES/_FONT_FACE_VARS in
    render_preferences_page's font picker) - every other name in _STYLE is a
    legitimate picker choice, not a leftover experiment like Poppins."""
    default_root_block = ds._STYLE.split(":root {")[1].split("\n}")[0]
    assert "--font-family-stack: 'Roboto'," in default_root_block
    assert "Poppins" not in ds._STYLE


def test_no_font_weight_600_remains():
    assert "font-weight: 600" not in ds._STYLE


def test_buttons_are_pill_shaped():
    assert "border-radius: 999px" in ds._STYLE.split(".btn {")[1].split("\n}")[0]


def test_btn_warning_uses_warning_container():
    section = ds._STYLE.split(".btn-warning {")[1].split("\n}")[0]
    assert "var(--md-warning-container)" in section
    assert "var(--md-on-warning-container)" in section


def test_custom_select_menu_uses_fixed_positioning_to_avoid_table_wrap_clipping():
    section = ds._STYLE.split(".custom-select-menu {")[1].split("\n}")[0]
    assert "position: fixed" in section


def test_btn_neutral_is_outlined():
    section = ds._STYLE.split(".btn-neutral {")[1].split("\n}")[0]
    assert "transparent" in section
    assert "var(--md-outline)" in section
    assert "var(--md-on-surface)" in section


def test_inputs_use_outline_and_small_radius():
    section = ds._STYLE.split(".daemon-action-form input[type='text'],")[1].split("\n}")[0]
    assert "border-radius: 8px" in section
    assert "var(--md-outline)" in section
    assert ".daemon-action-form input[type='text']:focus," in ds._STYLE
    assert "var(--md-primary)" in ds._STYLE.split(".daemon-action-form input[type='text']:focus,")[1].split("\n}")[0]


def test_card_has_no_border_and_larger_radius():
    section = ds._STYLE.split(".card {")[1].split("\n}")[0]
    assert "border-radius: 12px" in section
    assert "border:" not in section


def test_table_header_has_surface_container_background():
    section = ds._STYLE.split("table.daemons th {")[1].split("\n}")[0]
    assert "background: var(--md-surface-container-high)" in section


def test_sidebar_nav_items_are_pill_shaped():
    section = ds._STYLE.split(".sidebar-nav a {")[1].split("\n}")[0]
    assert "border-radius: 999px" in section


def test_pills_badges_flash_use_solid_container_colors_not_rgba_tints():
    """MD3 pills/badges/flash banners use a solid *-container background
    with an on-*-container text color, not a translucent rgba() tint over
    the page background - the old tinted-overlay technique this used to
    use before the MD3 restyle. `.btn-warning`'s matching rgba(251, 146,
    60, ...) conversion is Task 4's job, not this task's - deliberately
    not asserted here, so this test is fully satisfied by this task alone."""
    assert "rgba(59, 130, 246," not in ds._STYLE, "old primary rgba tint must be gone"
    assert "rgba(34, 197, 94," not in ds._STYLE, "old success rgba tint must be gone"
    assert "rgba(248, 113, 113," not in ds._STYLE, "old danger rgba tint must be gone"

    assert "background: var(--md-primary-container)" in ds._STYLE  # pill-blue, badge-count
    assert "background: var(--md-success-container)" in ds._STYLE  # pill-green
    assert "background: var(--md-error-container)" in ds._STYLE  # pill-red


def test_nav_active_state_css_rule_present():
    assert ".sidebar-nav a.active" in ds._STYLE


def test_sidebar_collapse_css_rules_present():
    assert "html.collapsed .sidebar" in ds._STYLE
    assert "html.collapsed .content-area" in ds._STYLE
    assert "html.collapsed .nav-label" in ds._STYLE


def test_mobile_breakpoint_forces_collapsed_sidebar_and_hides_toggle():
    assert "@media (max-width: 720px)" in ds._STYLE
    media_block = ds._STYLE.split("@media (max-width: 720px)")[1]
    assert ".sidebar-toggle" in media_block


def test_old_top_nav_css_removed():
    assert "header.site-header" not in ds._STYLE
    assert ".nav-links-wrap" not in ds._STYLE
    assert ".nav-links {" not in ds._STYLE


def test_anchor_scroll_margin_rules_removed():
    """Section anchor IDs and their responsive scroll-margin-top hacks only
    existed because the nav used to scroll within one long page - once every
    nav link is a real page (later tasks), they're dead weight."""
    assert "scroll-margin-top" not in ds._STYLE


def test_brand_mark_icon_is_two_circle_infinity_glyph():
    assert ds._BRAND_MARK_ICON.count("<circle") == 2
    assert "cx='8' cy='12' r='4.5'" in ds._BRAND_MARK_ICON
    assert "cx='16' cy='12' r='4.5'" in ds._BRAND_MARK_ICON
    assert "M12 4a8 8 0 1 0 8 8" not in ds._BRAND_MARK_ICON, "old circular-arrow path must be gone"


def test_dashboard_server_integration_serves_root_page():
    """Start the real ThreadingHTTPServer on an OS-assigned port, GET /, and
    confirm it renders without crashing even against whatever config exists
    (or doesn't) on this machine."""
    server = ds.ThreadingHTTPServer(("127.0.0.1", 0), ds.DashboardHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as response:
            assert response.status == 200
            body = response.read().decode("utf-8")
            assert "Loop X Engineering" in body
            assert "<h2>Conversation</h2>" in body
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_dashboard_server_integration_history_route(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "HISTORY_DIR", tmp_path)
    (tmp_path / "2026-08-01.md").write_text("# Review\nsome content")

    server = ds.ThreadingHTTPServer(("127.0.0.1", 0), ds.DashboardHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/history/2026-08-01.md", timeout=10) as response:
            assert response.status == 200
            body = response.read().decode("utf-8")
            assert "some content" in body
            assert "class='sidebar-nav'" in body
            assert "<a href='/gitlab'" in body
            assert "auto-refreshes every 30s" not in body

        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/history/../../etc/passwd", timeout=10)
            assert False, "expected HTTPError for path traversal / missing file"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_dashboard_server_integration_history_list_route():
    with _running_server() as port:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/history", timeout=10) as response:
            assert response.status == 200
            assert "Run History" in response.read().decode("utf-8")


def test_dashboard_server_integration_daemons_route():
    with _running_server() as port:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/daemons", timeout=10) as response:
            assert response.status == 200
            assert "Launchd Daemons" in response.read().decode("utf-8")


def test_dashboard_server_integration_topic_monitor_route():
    with _running_server() as port:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/topic-monitor", timeout=10) as resp:
            assert resp.status == 200
            assert b"Topic Monitor" in resp.read()


def test_dashboard_server_integration_topic_settings_route():
    with _running_server() as port:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/topic-monitor/settings", timeout=10) as resp:
            assert resp.status == 200
            body = resp.read()
            assert b"Topic Settings" in body
            assert b"<a href='/topic-monitor/settings' title='Topic Settings' class='active'>" in body


def test_dashboard_server_integration_topic_monitor_history_route(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path)
    (tmp_path / "2026-08-22-ai-news.md").write_text("# Briefing\n\nNothing notable.")

    with _running_server() as port:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/topic-monitor/history/2026-08-22-ai-news.md", timeout=10) as resp:
            body = resp.read()
            assert resp.status == 200
            assert b"Nothing notable" in body


def test_dashboard_server_integration_html_responses_are_never_cached():
    """Every page here reflects live, fast-changing state (run status,
    sidebar collapse behavior, etc.) - a browser serving a stale cached
    copy on reload would show outdated UI. No-store rules that out."""
    with _running_server() as port:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as response:
            assert response.headers.get("Cache-Control") == "no-store"


def test_dashboard_server_integration_favicon_route(tmp_path, monkeypatch):
    fake_favicon = tmp_path / "favicon.ico"
    fake_favicon.write_bytes(b"\x00\x00\x01\x00fake-ico-bytes")
    monkeypatch.setattr(ds, "FAVICON_PATH", fake_favicon)

    with _running_server() as port:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico", timeout=10) as response:
            assert response.status == 200
            assert response.headers.get("Content-Type") == "image/x-icon"
            assert response.read() == fake_favicon.read_bytes()


def test_dashboard_server_integration_favicon_route_404s_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "FAVICON_PATH", tmp_path / "does-not-exist.ico")

    with _running_server() as port:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico", timeout=10)
            assert False, "expected HTTPError when favicon file is missing"
        except urllib.error.HTTPError as e:
            assert e.code == 404


def test_render_shell_links_favicon_in_head():
    body = ds._render_shell("Test Page", "overview", "<span>badge</span>", "<p>body</p>")
    assert '<link rel="icon" href="/favicon.ico?v=' in body
    assert 'type="image/x-icon">' in body


def test_render_shell_shows_selected_ai_cli_badge_for_claude(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_cli_config, "DEFAULT_CONFIG_PATH", tmp_path / "does-not-exist.json")

    body = ds._render_shell("Test Page", "overview", "<span>badge</span>", "<p>body</p>")

    assert "Claude Code" in body
    assert "href='/ai-cli'" in body


def test_render_shell_shows_selected_ai_cli_badge_for_codex(monkeypatch, tmp_path):
    config_path = tmp_path / "ai_cli.json"
    config_path.write_text('{"cli": "codex"}')
    monkeypatch.setattr(ai_cli_config, "DEFAULT_CONFIG_PATH", config_path)

    body = ds._render_shell("Test Page", "overview", "<span>badge</span>", "<p>body</p>")

    assert "Codex CLI" in body
    assert "Claude Code" not in body


def test_dashboard_server_integration_assets_fonts_route_is_gone(tmp_path):
    with _running_server() as port:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/assets/fonts/anything.woff2", timeout=10)
            assert False, "expected HTTPError now that fonts are loaded from Google Fonts, not self-hosted"
        except urllib.error.HTTPError as e:
            assert e.code == 404


def test_style_has_no_local_font_face_rules():
    assert "@font-face" not in ds._STYLE
    assert "/assets/fonts/" not in ds._STYLE


def test_render_shell_links_google_fonts_roboto_and_material_symbols():
    body = ds._render_shell("Test Page", "overview", "<span>badge</span>", "<p>body</p>")
    assert '<link rel="preconnect" href="https://fonts.googleapis.com">' in body
    assert '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>' in body
    body_fonts_link = re.search(r'href="(https://fonts\.googleapis\.com/css2\?family=Roboto[^"]+)"', body)
    assert body_fonts_link is not None
    fonts_url = body_fonts_link.group(1)
    assert fonts_url.endswith("&display=swap")
    for key, label, name in ds._FONT_CHOICES:
        assert f"family={name.replace(' ', '+')}:wght@400;500;700" in fonts_url
    material_link = re.search(
        r'href="(https://fonts\.googleapis\.com/css2\?family=Material\+Symbols\+Outlined[^"]+)"', body
    )
    assert material_link is not None
    icons_url = material_link.group(1)
    assert "display=block" in icons_url
    for name in [
        "add", "bolt", "check_circle", "chevron_left", "circle", "delete",
        "description", "dns", "edit_note", "error", "expand_more", "extension", "history",
        "lightbulb", "palette", "send", "settings", "space_dashboard",
    ]:
        assert name in icons_url


def test_favicon_version_changes_when_file_contents_change(tmp_path, monkeypatch):
    favicon = tmp_path / "favicon.ico"
    favicon.write_bytes(b"version one")
    monkeypatch.setattr(ds, "FAVICON_PATH", favicon)
    v1 = ds._favicon_version()

    favicon.write_bytes(b"version two")
    v2 = ds._favicon_version()

    assert v1 != v2


def test_favicon_version_is_stable_string_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "FAVICON_PATH", tmp_path / "does-not-exist.ico")
    assert ds._favicon_version() == "0"


def test_dashboard_server_integration_gitlab_and_learnings_routes_survive_missing_config(
        tmp_path, monkeypatch):
    """Extra variant for the "no config yet" case, now targeted at the two
    routes that actually depend on project config after the page split -
    confirm they still render 200 rather than crashing."""
    monkeypatch.setattr(loop_config, "DEFAULT_CONFIG_PATH", tmp_path / "does-not-exist" / "projects.json")

    with _running_server() as port:
        for path in ("/gitlab", "/learnings"):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
                assert response.status == 200


class _FakeCompletedProcess:
    """Stand-in for subprocess.CompletedProcess, just the attributes
    enable_daemon/disable_daemon/get_daemons_status actually read."""

    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def _fake_runner_success(*args, **kwargs):
    return _FakeCompletedProcess(returncode=0, stderr="")


def _fake_runner_failure(*args, **kwargs):
    return _FakeCompletedProcess(returncode=1, stderr="launchctl: some failure")


# A `launchctl list` body in which com.example.foo IS currently loaded - the
# schedule editor only reloads launchd for a daemon that's actually running.
_LOADED_FOO_LAUNCHCTL_OUTPUT = "PID\tStatus\tLabel\n1234\t0\tcom.example.foo\n"


def test_enable_daemon_copies_to_launch_agents_dir_and_loads(tmp_path):
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    launch_agents_dir = tmp_path / "LaunchAgents"
    plist_path = launchd_dir / "com.example.foo.plist"
    with open(plist_path, "wb") as f:
        plistlib.dump({"Label": "com.example.foo", "ProgramArguments": ["/bin/true"]}, f)

    ok, message = ds.enable_daemon(
        "com.example.foo.plist",
        launchd_dir=launchd_dir,
        launch_agents_dir=launch_agents_dir,
        runner=_fake_runner_success,
    )

    assert ok is True
    assert "com.example.foo.plist" in message
    assert (launch_agents_dir / "com.example.foo.plist").exists()
    assert (launch_agents_dir / "com.example.foo.plist").read_bytes() == plist_path.read_bytes()


def test_enable_daemon_passes_w_flag_so_it_clears_a_prior_disable(tmp_path):
    """A daemon that was ever disabled via disable_daemon's `unload -w` has a
    persistent "Disabled" override that a plain `launchctl load` (no `-w`)
    cannot clear: real launchctl exits 0 in that case while stderr says
    `Load failed: 5: Input/output error`, and nothing actually loads - the
    UI would flash "Loaded" while the daemon silently stays off. `load -w`
    clears the override, so enable_daemon must always pass `-w`, mirroring
    disable_daemon's own use of it."""
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    launch_agents_dir = tmp_path / "LaunchAgents"
    with open(launchd_dir / "com.example.foo.plist", "wb") as f:
        plistlib.dump({"Label": "com.example.foo"}, f)

    captured = {}

    def capturing_runner(argv, **kwargs):
        captured["argv"] = argv
        return _FakeCompletedProcess(returncode=0, stderr="")

    ok, _message = ds.enable_daemon(
        "com.example.foo.plist",
        launchd_dir=launchd_dir,
        launch_agents_dir=launch_agents_dir,
        runner=capturing_runner,
    )

    assert ok is True
    assert captured["argv"][:3] == ["launchctl", "load", "-w"]


def test_enable_daemon_reports_failure_from_runner(tmp_path):
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    launch_agents_dir = tmp_path / "LaunchAgents"
    with open(launchd_dir / "com.example.foo.plist", "wb") as f:
        plistlib.dump({"Label": "com.example.foo"}, f)

    ok, message = ds.enable_daemon(
        "com.example.foo.plist",
        launchd_dir=launchd_dir,
        launch_agents_dir=launch_agents_dir,
        runner=_fake_runner_failure,
    )

    assert ok is False
    assert "launchctl: some failure" in message


def test_enable_daemon_removes_copied_plist_when_launchctl_load_fails(tmp_path):
    """A failed enable must leave nothing behind: the plist is copied to
    ~/Library/LaunchAgents BEFORE launchctl load runs, and launchd auto-loads
    whatever is sitting there at the next login - so a copy left behind after
    a reported failure would silently enable the daemon anyway."""
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    launch_agents_dir = tmp_path / "LaunchAgents"
    with open(launchd_dir / "com.example.foo.plist", "wb") as f:
        plistlib.dump({"Label": "com.example.foo"}, f)

    ok, _message = ds.enable_daemon(
        "com.example.foo.plist",
        launchd_dir=launchd_dir,
        launch_agents_dir=launch_agents_dir,
        runner=_fake_runner_failure,
    )

    assert ok is False
    assert not (launch_agents_dir / "com.example.foo.plist").exists()
    # The repo's own source of truth must of course still be there.
    assert (launchd_dir / "com.example.foo.plist").exists()


def test_enable_daemon_removes_copied_plist_when_launchctl_raises(tmp_path):
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    launch_agents_dir = tmp_path / "LaunchAgents"
    with open(launchd_dir / "com.example.foo.plist", "wb") as f:
        plistlib.dump({"Label": "com.example.foo"}, f)

    def exploding_runner(*args, **kwargs):
        raise OSError("launchctl not found")

    ok, message = ds.enable_daemon(
        "com.example.foo.plist",
        launchd_dir=launchd_dir,
        launch_agents_dir=launch_agents_dir,
        runner=exploding_runner,
    )

    assert ok is False
    assert "failed to run" in message
    assert not (launch_agents_dir / "com.example.foo.plist").exists()


def test_enable_daemon_missing_source_plist_returns_false(tmp_path):
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    launch_agents_dir = tmp_path / "LaunchAgents"

    ok, message = ds.enable_daemon(
        "com.example.does-not-exist.plist",
        launchd_dir=launchd_dir,
        launch_agents_dir=launch_agents_dir,
        runner=_fake_runner_success,
    )

    assert ok is False
    assert "not found" in message
    assert not launch_agents_dir.exists() or list(launch_agents_dir.iterdir()) == []


def test_enable_daemon_rejects_non_plist_filename(tmp_path):
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    launch_agents_dir = tmp_path / "LaunchAgents"

    ok, message = ds.enable_daemon(
        "not-a-plist.txt",
        launchd_dir=launchd_dir,
        launch_agents_dir=launch_agents_dir,
        runner=_fake_runner_success,
    )

    assert ok is False
    assert "Invalid plist filename" in message
    assert not launch_agents_dir.exists() or list(launch_agents_dir.iterdir()) == []


def test_enable_daemon_rejects_path_traversal(tmp_path):
    """A malicious filename reaching enable_daemon (e.g. via the URL path
    segment in a /daemons/<filename>/enable request) must never let
    launchctl load/copy anything outside launchd_dir/launch_agents_dir.
    Path(filename).name collapses the traversal down to a bare filename
    ("evil.plist"), which then legitimately fails the "not found in
    launchd_dir" check because it was never actually placed there."""
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    launch_agents_dir = tmp_path / "LaunchAgents"
    # A real file elsewhere on the "filesystem" that a successful traversal
    # would have to reach - it must be left completely untouched.
    outside_target = tmp_path / "etc" / "cron.d"
    outside_target.mkdir(parents=True)
    (outside_target / "evil.plist").write_text("not touched")

    ok, message = ds.enable_daemon(
        "../../../etc/cron.d/evil.plist",
        launchd_dir=launchd_dir,
        launch_agents_dir=launch_agents_dir,
        runner=_fake_runner_success,
    )

    assert ok is False
    assert "not found" in message
    assert (outside_target / "evil.plist").read_text() == "not touched"
    assert not launch_agents_dir.exists() or list(launch_agents_dir.iterdir()) == []


def _make_project_launchd_dir(tmp_path, *names):
    """A stand-in for this repo's own launchd/ source-of-truth directory,
    containing real plists for each given name."""
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir(exist_ok=True)
    for name in names:
        with open(launchd_dir / name, "wb") as f:
            plistlib.dump({"Label": Path(name).stem}, f)
    return launchd_dir


def test_disable_daemon_unloads_installed_plist(tmp_path):
    launchd_dir = _make_project_launchd_dir(tmp_path, "com.example.foo.plist")
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()
    with open(launch_agents_dir / "com.example.foo.plist", "wb") as f:
        plistlib.dump({"Label": "com.example.foo"}, f)

    ok, message = ds.disable_daemon(
        "com.example.foo.plist",
        launchd_dir=launchd_dir,
        launch_agents_dir=launch_agents_dir,
        runner=_fake_runner_success,
    )

    assert ok is True
    assert "com.example.foo.plist" in message


def test_disable_daemon_uses_w_flag_so_it_stays_disabled_after_login(tmp_path):
    """`launchctl unload` without -w only unloads for the current session;
    since the plist file is deliberately left in place, launchd would
    auto-load it again at the next login and silently undo the Disable."""
    launchd_dir = _make_project_launchd_dir(tmp_path, "com.example.foo.plist")
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()
    with open(launch_agents_dir / "com.example.foo.plist", "wb") as f:
        plistlib.dump({"Label": "com.example.foo"}, f)
    calls = []

    def capturing_runner(argv, **kwargs):
        calls.append(argv)
        return _FakeCompletedProcess(returncode=0)

    ok, _message = ds.disable_daemon(
        "com.example.foo.plist",
        launchd_dir=launchd_dir,
        launch_agents_dir=launch_agents_dir,
        runner=capturing_runner,
    )

    assert ok is True
    [argv] = calls
    assert argv[:3] == ["launchctl", "unload", "-w"]
    assert argv[3] == str(launch_agents_dir / "com.example.foo.plist")


def test_disable_daemon_refuses_plists_that_are_not_this_projects_own(tmp_path):
    """~/Library/LaunchAgents is shared with the rest of the system, so
    "unload whatever is installed under this name" would let the dashboard
    unload homebrew.mxcl.postgresql, redis, etc. Only names that exist in
    this project's own launchd/ dir may be disabled."""
    launchd_dir = _make_project_launchd_dir(tmp_path, "com.example.ours.plist")
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()
    # A third-party daemon that exists ONLY in ~/Library/LaunchAgents.
    with open(launch_agents_dir / "homebrew.mxcl.postgresql@17.plist", "wb") as f:
        plistlib.dump({"Label": "homebrew.mxcl.postgresql@17"}, f)
    calls = []

    def capturing_runner(argv, **kwargs):
        calls.append(argv)
        return _FakeCompletedProcess(returncode=0)

    ok, message = ds.disable_daemon(
        "homebrew.mxcl.postgresql@17.plist",
        launchd_dir=launchd_dir,
        launch_agents_dir=launch_agents_dir,
        runner=capturing_runner,
    )

    assert ok is False
    assert message == "homebrew.mxcl.postgresql@17.plist is not a known project daemon"
    # launchctl must never have been invoked at all, and the third party's
    # own plist must be left completely untouched.
    assert calls == []
    assert (launch_agents_dir / "homebrew.mxcl.postgresql@17.plist").exists()


def test_disable_daemon_reports_failure_from_runner(tmp_path):
    launchd_dir = _make_project_launchd_dir(tmp_path, "com.example.foo.plist")
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()
    with open(launch_agents_dir / "com.example.foo.plist", "wb") as f:
        plistlib.dump({"Label": "com.example.foo"}, f)

    ok, message = ds.disable_daemon(
        "com.example.foo.plist",
        launchd_dir=launchd_dir,
        launch_agents_dir=launch_agents_dir,
        runner=_fake_runner_failure,
    )

    assert ok is False
    assert "launchctl: some failure" in message


def test_disable_daemon_noop_success_when_not_installed(tmp_path):
    launchd_dir = _make_project_launchd_dir(tmp_path, "com.example.never-installed.plist")
    launch_agents_dir = tmp_path / "LaunchAgents"

    ok, message = ds.disable_daemon(
        "com.example.never-installed.plist",
        launchd_dir=launchd_dir,
        launch_agents_dir=launch_agents_dir,
        runner=_fake_runner_success,
    )

    assert ok is True
    assert "was not loaded" in message


def test_disable_daemon_rejects_non_plist_filename(tmp_path):
    launchd_dir = _make_project_launchd_dir(tmp_path)
    launch_agents_dir = tmp_path / "LaunchAgents"

    ok, message = ds.disable_daemon(
        "not-a-plist.txt",
        launchd_dir=launchd_dir,
        launch_agents_dir=launch_agents_dir,
        runner=_fake_runner_success,
    )

    assert ok is False
    assert "Invalid plist filename" in message


def test_build_calendar_interval_specific_weekdays():
    result = ds.build_calendar_interval(9, 30, [1, 3, 5])

    assert result == [
        {"Weekday": 1, "Hour": 9, "Minute": 30},
        {"Weekday": 3, "Hour": 9, "Minute": 30},
        {"Weekday": 5, "Hour": 9, "Minute": 30},
    ]


def test_build_calendar_interval_empty_weekdays_means_every_day():
    result = ds.build_calendar_interval(9, 0, [])

    assert result == {"Hour": 9, "Minute": 0}


def test_build_calendar_interval_all_seven_weekdays_means_every_day():
    result = ds.build_calendar_interval(9, 0, [0, 1, 2, 3, 4, 5, 6])

    assert result == {"Hour": 9, "Minute": 0}


def test_build_calendar_interval_dedupes_and_sorts_weekdays():
    result = ds.build_calendar_interval(9, 0, [5, 1, 1, 3])

    assert [e["Weekday"] for e in result] == [1, 3, 5]


def test_describe_schedule_every_day():
    assert ds._describe_schedule({"Hour": 9, "Minute": 0}) == "Every day 09:00"


def test_describe_schedule_mon_fri_unchanged():
    schedule = [
        {"Weekday": d, "Hour": 10, "Minute": 0} for d in (1, 2, 3, 4, 5)
    ]
    assert ds._describe_schedule(schedule) == "Mon–Fri 10:00"


def test_describe_schedule_arbitrary_subset():
    schedule = [{"Weekday": 2, "Hour": 8, "Minute": 15}, {"Weekday": 4, "Hour": 8, "Minute": 15}]
    assert ds._describe_schedule(schedule) == "Tue, Thu 08:15"


def test_describe_schedule_monthly():
    assert ds._describe_schedule({"Day": 15, "Hour": 9, "Minute": 0}) == "Monthly on day 15 09:00"


def test_build_calendar_interval_day_of_month():
    result = ds.build_calendar_interval(9, 0, [], day_of_month=15)

    assert result == {"Day": 15, "Hour": 9, "Minute": 0}


def test_build_calendar_interval_day_of_month_takes_precedence_over_weekdays():
    result = ds.build_calendar_interval(9, 0, [1, 2, 3], day_of_month=15)

    assert result == {"Day": 15, "Hour": 9, "Minute": 0}


def test_update_daemon_schedule_rewrites_source_plist_when_not_installed(tmp_path):
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    plist_path = launchd_dir / "com.example.foo.plist"
    with open(plist_path, "wb") as f:
        plistlib.dump({"Label": "com.example.foo", "StartCalendarInterval": {"Hour": 10, "Minute": 0}}, f)
    launch_agents_dir = tmp_path / "LaunchAgents"

    ok, message = ds.update_daemon_schedule(
        "com.example.foo.plist", 9, 30, [1, 2, 3, 4, 5],
        launchd_dir=launchd_dir, launch_agents_dir=launch_agents_dir,
    )

    assert ok is True
    with open(plist_path, "rb") as f:
        data = plistlib.load(f)
    assert data["StartCalendarInterval"] == [
        {"Weekday": 1, "Hour": 9, "Minute": 30},
        {"Weekday": 2, "Hour": 9, "Minute": 30},
        {"Weekday": 3, "Hour": 9, "Minute": 30},
        {"Weekday": 4, "Hour": 9, "Minute": 30},
        {"Weekday": 5, "Hour": 9, "Minute": 30},
    ]


def test_update_daemon_schedule_reloads_installed_copy(tmp_path):
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    with open(launchd_dir / "com.example.foo.plist", "wb") as f:
        plistlib.dump({"Label": "com.example.foo", "StartCalendarInterval": {"Hour": 10, "Minute": 0}}, f)
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()
    with open(launch_agents_dir / "com.example.foo.plist", "wb") as f:
        plistlib.dump({"Label": "com.example.foo", "StartCalendarInterval": {"Hour": 10, "Minute": 0}}, f)

    captured = []

    def capturing_runner(argv, **kwargs):
        captured.append(argv)
        return _FakeCompletedProcess(returncode=0, stderr="")

    ok, message = ds.update_daemon_schedule(
        "com.example.foo.plist", 9, 0, [],
        launchd_dir=launchd_dir, launch_agents_dir=launch_agents_dir, runner=capturing_runner,
        launchctl_output=_LOADED_FOO_LAUNCHCTL_OUTPUT,
    )

    assert ok is True
    assert captured[0][:3] == ["launchctl", "unload", "-w"]
    assert captured[1][:3] == ["launchctl", "load", "-w"]
    with open(launch_agents_dir / "com.example.foo.plist", "rb") as f:
        data = plistlib.load(f)
    assert data["StartCalendarInterval"] == {"Hour": 9, "Minute": 0}


def test_update_daemon_schedule_skips_launchctl_for_a_disabled_daemon(tmp_path):
    """disable_daemon deliberately LEAVES the plist in ~/Library/LaunchAgents/
    ("disable", not "uninstall"), so "the file is there" does not mean "it is
    loaded". Reloading on that basis would run `launchctl load -w`, whose -w
    clears the persisted disable override and silently re-enables a daemon the
    user turned off. The new schedule must still land on disk, ready for
    whenever it is re-enabled."""
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    src = launchd_dir / "com.example.foo.plist"
    with open(src, "wb") as f:
        plistlib.dump({"Label": "com.example.foo", "StartCalendarInterval": {"Hour": 10, "Minute": 0}}, f)
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()
    with open(launch_agents_dir / "com.example.foo.plist", "wb") as f:
        plistlib.dump({"Label": "com.example.foo", "StartCalendarInterval": {"Hour": 10, "Minute": 0}}, f)

    captured = []

    def capturing_runner(argv, **kwargs):
        captured.append(argv)
        return _FakeCompletedProcess(returncode=0, stderr="")

    ok, message = ds.update_daemon_schedule(
        "com.example.foo.plist", 7, 45, [],
        launchd_dir=launchd_dir, launch_agents_dir=launch_agents_dir, runner=capturing_runner,
        # com.example.foo is absent from `launchctl list` output => not loaded.
        launchctl_output="PID\tStatus\tLabel\n-\t0\tcom.example.other\n",
    )

    assert ok is True
    assert captured == []
    assert "disabled" in message
    with open(src, "rb") as f:
        assert plistlib.load(f)["StartCalendarInterval"] == {"Hour": 7, "Minute": 45}


def test_update_daemon_schedule_reports_failure_from_reload(tmp_path):
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    with open(launchd_dir / "com.example.foo.plist", "wb") as f:
        plistlib.dump({"Label": "com.example.foo"}, f)
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()
    with open(launch_agents_dir / "com.example.foo.plist", "wb") as f:
        plistlib.dump({"Label": "com.example.foo"}, f)

    ok, message = ds.update_daemon_schedule(
        "com.example.foo.plist", 9, 0, [],
        launchd_dir=launchd_dir, launch_agents_dir=launch_agents_dir, runner=_fake_runner_failure,
        launchctl_output=_LOADED_FOO_LAUNCHCTL_OUTPUT,
    )

    assert ok is False
    assert "launchctl: some failure" in message


def test_update_daemon_schedule_restores_and_reloads_when_load_fails(tmp_path):
    """`unload` succeeded and `load` didn't, so the daemon is down. Put the
    previously-loaded plist content back and make a second load attempt
    rather than walking away leaving a running daemon stopped."""
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    with open(launchd_dir / "com.example.foo.plist", "wb") as f:
        plistlib.dump({"Label": "com.example.foo", "StartCalendarInterval": {"Hour": 10, "Minute": 0}}, f)
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()
    dest = launch_agents_dir / "com.example.foo.plist"
    with open(dest, "wb") as f:
        plistlib.dump({"Label": "com.example.foo", "StartCalendarInterval": {"Hour": 10, "Minute": 0}}, f)

    captured = []

    def runner(argv, **kwargs):
        captured.append(argv)
        # unload works; the first load (of the new schedule) is rejected, the
        # restore load that follows succeeds.
        if argv[1] == "load" and len(captured) == 2:
            return _FakeCompletedProcess(returncode=1, stderr="Load failed: 5: Input/output error")
        return _FakeCompletedProcess(returncode=0, stderr="")

    ok, message = ds.update_daemon_schedule(
        "com.example.foo.plist", 9, 0, [],
        launchd_dir=launchd_dir, launch_agents_dir=launch_agents_dir, runner=runner,
        launchctl_output=_LOADED_FOO_LAUNCHCTL_OUTPUT,
    )

    assert ok is False
    assert "Load failed" in message
    assert [a[1] for a in captured] == ["unload", "load", "load"]
    with open(dest, "rb") as f:
        assert plistlib.load(f)["StartCalendarInterval"] == {"Hour": 10, "Minute": 0}


def test_update_daemon_schedule_rejects_non_plist_filename(tmp_path):
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()

    ok, message = ds.update_daemon_schedule(
        "not-a-plist.txt", 9, 0, [], launchd_dir=launchd_dir, launch_agents_dir=tmp_path / "LaunchAgents",
    )

    assert ok is False
    assert "Invalid plist filename" in message


def test_update_daemon_schedule_missing_source_returns_false(tmp_path):
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()

    ok, message = ds.update_daemon_schedule(
        "com.example.does-not-exist.plist", 9, 0, [],
        launchd_dir=launchd_dir, launch_agents_dir=tmp_path / "LaunchAgents",
    )

    assert ok is False
    assert "not found" in message


def test_update_daemon_schedule_rejects_out_of_range_time(tmp_path):
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    with open(launchd_dir / "com.example.foo.plist", "wb") as f:
        plistlib.dump({"Label": "com.example.foo"}, f)

    ok, message = ds.update_daemon_schedule(
        "com.example.foo.plist", 25, 0, [], launchd_dir=launchd_dir, launch_agents_dir=tmp_path / "LaunchAgents",
    )

    assert ok is False
    assert "Hour" in message


def test_update_daemon_schedule_saves_day_of_month(tmp_path):
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    plist_path = launchd_dir / "com.example.foo.plist"
    with open(plist_path, "wb") as f:
        plistlib.dump({"Label": "com.example.foo", "StartCalendarInterval": {"Hour": 10, "Minute": 0}}, f)

    ok, message = ds.update_daemon_schedule(
        "com.example.foo.plist", 9, 0, [], day_of_month=15,
        launchd_dir=launchd_dir, launch_agents_dir=tmp_path / "LaunchAgents",
    )

    assert ok is True, message
    with open(plist_path, "rb") as f:
        data = plistlib.load(f)
    assert data["StartCalendarInterval"] == {"Day": 15, "Hour": 9, "Minute": 0}


def test_update_daemon_schedule_rejects_out_of_range_day_of_month(tmp_path):
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    with open(launchd_dir / "com.example.foo.plist", "wb") as f:
        plistlib.dump({"Label": "com.example.foo"}, f)

    ok, message = ds.update_daemon_schedule(
        "com.example.foo.plist", 9, 0, [], day_of_month=32,
        launchd_dir=launchd_dir, launch_agents_dir=tmp_path / "LaunchAgents",
    )

    assert ok is False
    assert "Day" in message


def test_update_daemon_schedule_rejects_path_traversal(tmp_path):
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    outside_target = tmp_path / "etc" / "cron.d"
    outside_target.mkdir(parents=True)
    (outside_target / "evil.plist").write_text("not touched")

    ok, message = ds.update_daemon_schedule(
        "../../../etc/cron.d/evil.plist", 9, 0, [],
        launchd_dir=launchd_dir, launch_agents_dir=tmp_path / "LaunchAgents",
    )

    assert ok is False
    assert (outside_target / "evil.plist").read_text() == "not touched"


@contextlib.contextmanager
def _running_server():
    """Run the real ThreadingHTTPServer on an OS-assigned port for the
    duration of the block, yielding the port."""
    server = ds.ThreadingHTTPServer(("127.0.0.1", 0), ds.DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _post(port, path, fields=None):
    """POST an application/x-www-form-urlencoded body - exactly the shape a
    cross-origin <form method="POST"> would send. Returns (status, headers,
    body_text). `fields=None` sends no body at all."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        if fields is None:
            conn.request("POST", path)
        else:
            body = urllib.parse.urlencode(fields)
            conn.request("POST", path, body=body,
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode("utf-8")
    finally:
        conn.close()


def _flash_from_location(location, prefix="/daemons?"):
    assert location is not None and location.startswith(prefix)
    return urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)


def _fetch_csrf_token(port, path="/daemons"):
    """Fetch a real rendered page and pull the token out of the hidden
    input, the way a legitimate browser session would."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
        body = response.read().decode("utf-8")
    match = re.search(r"name='csrf_token' value=\"([^\"]+)\"", body)
    assert match, "no csrf_token hidden input found in the rendered page"
    return match.group(1)


class _CapturingRun:
    """Stands in for subprocess.run and records every argv it was handed, so
    a test can prove exactly which launchctl invocations happened - and,
    crucially, which paths they were pointed at."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append([str(arg) for arg in argv])
        return _FakeCompletedProcess(returncode=0, stderr="", stdout="")

    @property
    def mutating_calls(self):
        """Everything except the read-only `launchctl list` that rendering
        the daemons table performs on every page load."""
        return [call for call in self.calls if call[:2] != ["launchctl", "list"]]

    def all_args(self):
        return [arg for call in self.calls for arg in call]


def test_do_post_enable_without_csrf_token_is_forbidden_and_mutates_nothing(tmp_path, monkeypatch):
    """The core CSRF proof. A cross-origin HTML form POST (or a bare
    `curl -X POST`) carries no token, needs no JavaScript and triggers no
    CORS preflight - being POST-only never stopped it. It must now 403, and
    enable_daemon/disable_daemon must never be reached at all."""
    monkeypatch.setattr(ds, "LAUNCHD_DIR", _make_project_launchd_dir(
        tmp_path, "com.example.loop-engineering.plist"))
    called = []
    monkeypatch.setattr(ds, "enable_daemon",
                        lambda *a, **k: called.append(("enable", a, k)) or (True, "should not happen"))
    monkeypatch.setattr(ds, "disable_daemon",
                        lambda *a, **k: called.append(("disable", a, k)) or (True, "should not happen"))
    captured_run = _CapturingRun()
    monkeypatch.setattr(ds.subprocess, "run", captured_run)

    with _running_server() as port:
        for path in ("/daemons/com.example.loop-engineering.plist/enable",
                     "/daemons/com.example.loop-engineering.plist/disable"):
            # No body at all (curl -X POST).
            status, _headers, body = _post(port, path)
            assert status == 403, f"{path} with no body should be forbidden"
            assert "CSRF" in body

            # An empty token.
            status, _headers, _body = _post(port, path, {"csrf_token": ""})
            assert status == 403, f"{path} with an empty token should be forbidden"

            # A wrong token of the right shape.
            status, _headers, _body = _post(port, path, {"csrf_token": "x" * 43})
            assert status == 403, f"{path} with a wrong token should be forbidden"

    assert called == [], "a state-changing function was invoked despite the failed CSRF check"
    assert captured_run.mutating_calls == [], "launchctl was invoked despite the failed CSRF check"


def test_do_post_enable_with_valid_csrf_token_from_rendered_page_succeeds(tmp_path, monkeypatch):
    """The other half of the proof: a real browser session reads the token
    out of the page this server rendered and the request then works."""
    launchd_dir = _make_project_launchd_dir(tmp_path, "com.example.toggle.plist")
    scratch_agents = tmp_path / "LaunchAgents"
    monkeypatch.setattr(ds, "LAUNCHD_DIR", launchd_dir)
    monkeypatch.setattr(ds, "_installed_plist_path",
                        lambda filename, launch_agents_dir=None: scratch_agents / filename)
    captured_run = _CapturingRun()
    monkeypatch.setattr(ds.subprocess, "run", captured_run)

    with _running_server() as port:
        token = _fetch_csrf_token(port)
        assert token == ds._CSRF_TOKEN

        status, headers, _body = _post(
            port, "/daemons/com.example.toggle.plist/enable", {"csrf_token": token})

        assert status == 303
        parsed = _flash_from_location(headers.get("Location"))
        assert parsed["ok"] == ["1"]
        assert "Loaded com.example.toggle.plist" in parsed["flash"][0]


def test_daemons_page_shows_flash_banner_after_a_real_post_redirect(tmp_path, monkeypatch):
    """Closes the loop on the one thing Task 4 actually changed: that a
    POST's 303 redirect to /daemons?flash=...&ok=... actually results in
    that banner appearing in the next GET's rendered body - not just that
    the redirect Location has the right query params (already covered) and
    not just that render_daemons_page(flash=...) escapes correctly in
    isolation (already covered), but that do_GET actually wires the two
    together."""
    launchd_dir = _make_project_launchd_dir(tmp_path, "com.example.toggle.plist")
    scratch_agents = tmp_path / "LaunchAgents"
    monkeypatch.setattr(ds, "LAUNCHD_DIR", launchd_dir)
    monkeypatch.setattr(ds, "_installed_plist_path",
                        lambda filename, launch_agents_dir=None: scratch_agents / filename)
    captured_run = _CapturingRun()
    monkeypatch.setattr(ds.subprocess, "run", captured_run)

    with _running_server() as port:
        token = _fetch_csrf_token(port)
        status, headers, _body = _post(
            port, "/daemons/com.example.toggle.plist/enable", {"csrf_token": token})
        assert status == 303
        location = headers.get("Location")

        with urllib.request.urlopen(f"http://127.0.0.1:{port}{location}", timeout=10) as response:
            assert response.status == 200
            body = response.read().decode("utf-8")
            assert "<div class='flash flash-success'>" in body
            assert "Loaded com.example.toggle.plist" in body


def test_do_post_uses_current_module_level_launchd_dir_not_the_defs_bound_default(
        tmp_path, monkeypatch):
    """Test-isolation regression test for the def-time-default gotcha.

    enable_daemon's `launchd_dir=LAUNCHD_DIR` default was bound once when the
    function was defined, so monkeypatching ds.LAUNCHD_DIR did NOT affect
    calls that relied on that default - a "unit test" could reach the real
    repo's launchd/ dir and the real ~/Library/LaunchAgents. do_POST now
    passes LAUNCHD_DIR explicitly (a bare global reference, resolved at call
    time), so the monkeypatch takes effect.

    Making this conclusive takes care, because asserting only on the launchctl
    argv is NOT enough: launchctl is pointed at the ~/Library/LaunchAgents
    destination, and the source directory (the part LAUNCHD_DIR actually
    controls) never appears in that argv at all. So this checks both halves:

      * enable uses the SAME filename as this repo's real main-loop plist, and
        asserts the bytes that got installed are the scratch file's, not the
        real repo file's. With the bug present, enable_daemon's def-time
        default would silently copy the REAL main GitLab loop plist.
      * disable uses a filename that exists ONLY in the scratch dir, so the
        new "is not a known project daemon" check can only pass if the
        monkeypatched LAUNCHD_DIR really reached disable_daemon.
    """
    real_name = "com.hermes.loop-engineering.plist"
    scratch_only_name = "com.example.scratch-only.plist"
    assert (REAL_LAUNCHD_DIR / real_name).exists(), (
        "precondition: this filename must really exist in the repo's launchd/ dir "
        "for this test to be a meaningful isolation proof")
    assert not (REAL_LAUNCHD_DIR / scratch_only_name).exists(), (
        "precondition: this filename must NOT exist in the repo's launchd/ dir")

    scratch_launchd = _make_project_launchd_dir(tmp_path, real_name, scratch_only_name)
    scratch_agents = tmp_path / "LaunchAgents"
    scratch_agents.mkdir()
    # Pre-install the scratch-only daemon so disable has something to unload.
    shutil.copyfile(scratch_launchd / scratch_only_name, scratch_agents / scratch_only_name)
    monkeypatch.setattr(ds, "LAUNCHD_DIR", scratch_launchd)
    monkeypatch.setattr(ds, "_installed_plist_path",
                        lambda filename, launch_agents_dir=None: scratch_agents / filename)
    captured_run = _CapturingRun()
    monkeypatch.setattr(ds.subprocess, "run", captured_run)

    with _running_server() as port:
        token = _fetch_csrf_token(port)
        status, headers, _body = _post(
            port, f"/daemons/{real_name}/enable", {"csrf_token": token})
        assert status == 303
        assert _flash_from_location(headers.get("Location"))["ok"] == ["1"]

        status, headers, _body = _post(
            port, f"/daemons/{scratch_only_name}/disable", {"csrf_token": token})
        assert status == 303
        parsed = _flash_from_location(headers.get("Location"))
        assert parsed["ok"] == ["1"], (
            f"disable did not see the monkeypatched LAUNCHD_DIR: {parsed['flash'][0]}")

    # The decisive assertion: what got installed came from the scratch dir,
    # NOT from the real repo. Comparing only the launchctl argv would miss
    # this entirely, since the source path never appears there.
    installed = (scratch_agents / real_name).read_bytes()
    assert installed == (scratch_launchd / real_name).read_bytes()
    assert installed != (REAL_LAUNCHD_DIR / real_name).read_bytes(), (
        "the REAL main GitLab loop plist was installed - LAUNCHD_DIR isolation is broken")

    # Everything launchctl was pointed at lives inside the scratch dir, and
    # neither the real repo dir nor the real ~/Library/LaunchAgents was named.
    assert captured_run.mutating_calls == [
        ["launchctl", "load", "-w", str(scratch_agents / real_name)],
        ["launchctl", "unload", "-w", str(scratch_agents / scratch_only_name)],
    ]
    real_home_agents = str(Path.home() / "Library" / "LaunchAgents")
    for arg in captured_run.all_args():
        assert str(REAL_LAUNCHD_DIR) not in arg, f"real repo launchd/ dir leaked into {arg!r}"
        assert real_home_agents not in arg, f"real ~/Library/LaunchAgents leaked into {arg!r}"


def test_do_post_disable_rejects_a_plist_that_is_not_a_project_daemon(tmp_path, monkeypatch):
    """End-to-end version of the disable-scoping fix: a plist present only in
    (the fake) ~/Library/LaunchAgents and absent from the project's launchd/
    dir must be refused before launchctl is touched."""
    scratch_launchd = _make_project_launchd_dir(tmp_path, "com.example.ours.plist")
    scratch_agents = tmp_path / "LaunchAgents"
    scratch_agents.mkdir()
    with open(scratch_agents / "homebrew.mxcl.postgresql@17.plist", "wb") as f:
        plistlib.dump({"Label": "homebrew.mxcl.postgresql@17"}, f)
    monkeypatch.setattr(ds, "LAUNCHD_DIR", scratch_launchd)
    monkeypatch.setattr(ds, "_installed_plist_path",
                        lambda filename, launch_agents_dir=None: scratch_agents / filename)
    captured_run = _CapturingRun()
    monkeypatch.setattr(ds.subprocess, "run", captured_run)

    with _running_server() as port:
        token = _fetch_csrf_token(port)
        status, headers, _body = _post(
            port, "/daemons/homebrew.mxcl.postgresql@17.plist/disable", {"csrf_token": token})

    assert status == 303
    parsed = _flash_from_location(headers.get("Location"))
    assert parsed["ok"] == ["0"]
    assert "is not a known project daemon" in parsed["flash"][0]
    assert captured_run.mutating_calls == []
    assert (scratch_agents / "homebrew.mxcl.postgresql@17.plist").exists()


def test_do_post_schedule_without_csrf_token_is_forbidden_and_mutates_nothing(tmp_path, monkeypatch):
    """Same CSRF proof as test_do_post_enable_without_csrf_token_is_forbidden_and_mutates_nothing,
    for the new /schedule route."""
    monkeypatch.setattr(ds, "LAUNCHD_DIR", _make_project_launchd_dir(
        tmp_path, "com.example.foo.plist"))
    called = []
    monkeypatch.setattr(ds, "update_daemon_schedule",
                        lambda *a, **k: called.append(a) or (True, "should not happen"))

    with _running_server() as port:
        status, _headers, body = _post(
            port, "/daemons/com.example.foo.plist/schedule", {"time": "09:00", "weekday": "1"})
        assert status == 403
        assert "CSRF" in body

    assert called == []


def test_do_post_schedule_with_valid_csrf_parses_time_and_weekdays(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "LAUNCHD_DIR", _make_project_launchd_dir(
        tmp_path, "com.example.foo.plist"))
    captured = {}

    def fake_update(filename, hour, minute, weekdays, day_of_month=None, launchd_dir=None, **kwargs):
        captured["args"] = (filename, hour, minute, sorted(weekdays))
        return True, "Updated"

    monkeypatch.setattr(ds, "update_daemon_schedule", fake_update)

    with _running_server() as port:
        token = _fetch_csrf_token(port)
        # A list of (key, value) pairs, not a dict, because _post's
        # urlencode(fields) call doesn't pass doseq=True - a dict value
        # that is itself a list ("weekday": ["1", "3"]) would str()-encode
        # the whole list as one value instead of repeating the key.
        # urlencode over a sequence of pairs doesn't have that problem: a
        # repeated key here is already exactly "weekday=1&weekday=3".
        status, headers, _body = _post(
            port, "/daemons/com.example.foo.plist/schedule",
            [("csrf_token", token), ("time", "09:30"), ("weekday", "1"), ("weekday", "3")])

        assert status == 303
        parsed = _flash_from_location(headers.get("Location"))
        assert parsed["ok"] == ["1"]

    assert captured["args"] == ("com.example.foo.plist", 9, 30, [1, 3])


def test_do_post_schedule_invalid_time_reports_error_without_calling_update(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "LAUNCHD_DIR", _make_project_launchd_dir(
        tmp_path, "com.example.foo.plist"))
    called = []
    monkeypatch.setattr(ds, "update_daemon_schedule", lambda *a, **k: called.append(a) or (True, "nope"))

    with _running_server() as port:
        token = _fetch_csrf_token(port)
        status, headers, _body = _post(
            port, "/daemons/com.example.foo.plist/schedule",
            {"csrf_token": token, "time": "not-a-time"})

        assert status == 303
        parsed = _flash_from_location(headers.get("Location"))
        assert parsed["ok"] == ["0"]

    assert called == []


def test_do_post_schedule_monthly_saves_day_of_month_and_ignores_weekday_field(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "LAUNCHD_DIR", _make_project_launchd_dir(
        tmp_path, "com.example.foo.plist"))
    captured = {}

    def fake_update(filename, hour, minute, weekdays, day_of_month=None, launchd_dir=None, **kwargs):
        captured["args"] = (filename, hour, minute, weekdays, day_of_month)
        return True, "Updated"

    monkeypatch.setattr(ds, "update_daemon_schedule", fake_update)

    with _running_server() as port:
        token = _fetch_csrf_token(port)
        # weekday=2 is submitted too (the weekly checkboxes stay in the form
        # even when hidden by "Monthly" mode) - frequency=Monthly must win.
        status, headers, _body = _post(
            port, "/daemons/com.example.foo.plist/schedule",
            [("csrf_token", token), ("time", "09:00"), ("frequency", "Monthly"),
             ("day_of_month", "15"), ("weekday", "2")])

        assert status == 303
        parsed = _flash_from_location(headers.get("Location"))
        assert parsed["ok"] == ["1"]

    assert captured["args"] == ("com.example.foo.plist", 9, 0, [], 15)


def test_do_post_schedule_weekly_ignores_day_of_month_field(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "LAUNCHD_DIR", _make_project_launchd_dir(
        tmp_path, "com.example.foo.plist"))
    captured = {}

    def fake_update(filename, hour, minute, weekdays, day_of_month=None, launchd_dir=None, **kwargs):
        captured["args"] = (filename, hour, minute, sorted(weekdays), day_of_month)
        return True, "Updated"

    monkeypatch.setattr(ds, "update_daemon_schedule", fake_update)

    with _running_server() as port:
        token = _fetch_csrf_token(port)
        status, headers, _body = _post(
            port, "/daemons/com.example.foo.plist/schedule",
            [("csrf_token", token), ("time", "09:30"), ("frequency", "Weekly"),
             ("weekday", "1"), ("weekday", "3"), ("day_of_month", "15")])

        assert status == 303

    assert captured["args"] == ("com.example.foo.plist", 9, 30, [1, 3], None)


def test_do_post_schedule_invalid_day_of_month_reports_error_without_calling_update(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "LAUNCHD_DIR", _make_project_launchd_dir(
        tmp_path, "com.example.foo.plist"))
    called = []
    monkeypatch.setattr(ds, "update_daemon_schedule", lambda *a, **k: called.append(a) or (True, "nope"))

    with _running_server() as port:
        token = _fetch_csrf_token(port)
        status, headers, _body = _post(
            port, "/daemons/com.example.foo.plist/schedule",
            [("csrf_token", token), ("time", "09:00"), ("frequency", "Monthly"), ("day_of_month", "not-a-day")])

        assert status == 303
        parsed = _flash_from_location(headers.get("Location"))
        assert parsed["ok"] == ["0"]

    assert called == []


def test_do_post_unknown_path_is_404_and_get_never_triggers_actions(tmp_path, monkeypatch):
    """A plain GET to an action path must still fall through to 404 - do_GET
    has no route for it - and an unrelated POST path is a 404 too."""
    monkeypatch.setattr(ds, "LAUNCHD_DIR", _make_project_launchd_dir(tmp_path))
    called = []
    monkeypatch.setattr(ds, "enable_daemon", lambda *a, **k: called.append(a) or (True, "nope"))

    with _running_server() as port:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/daemons/com.example.nope.plist/enable", timeout=10)
            assert False, "expected 404 for a GET to an action path"
        except urllib.error.HTTPError as e:
            assert e.code == 404

        status, _headers, _body = _post(port, "/daemons/nope", {"csrf_token": ds._CSRF_TOKEN})
        assert status == 404

    assert called == []


def test_render_daemons_page_includes_csrf_token_in_both_enable_and_disable_forms(monkeypatch):
    monkeypatch.setattr(ds, "get_daemons_status", lambda *a, **k: [
        {"file": "com.example.on.plist", "label": "com.example.on", "loaded": True,
         "pid": "123", "program_arguments": ["/bin/true"], "run_at_load": True,
         "keep_alive": True, "schedule": None},
        {"file": "com.example.off.plist", "label": "com.example.off", "loaded": False,
         "pid": None, "program_arguments": ["/bin/true"], "run_at_load": False,
         "keep_alive": False, "schedule": None},
    ])

    output = ds.render_daemons_page()

    enable_form = output.split("action='/daemons/com.example.off.plist/enable'")[1].split("</form>")[0]
    disable_form = output.split("action='/daemons/com.example.on.plist/disable'")[1].split("</form>")[0]
    expected_input = f"<input type='hidden' name='csrf_token' value=\"{ds._CSRF_TOKEN}\">"
    assert expected_input in enable_form
    assert expected_input in disable_form


def test_render_daemons_page_confirm_text_survives_an_html_entity_in_the_label(monkeypatch):
    """A Label carrying an HTML entity must not be able to break out of the
    data-confirm attribute early: the browser's HTML parser decodes entities
    in an attribute value, so a Label containing e.g. `&#34;` must not
    decode back into a real `"` that ends the attribute before the real
    closing quote."""
    hostile = 'pwn&#34;-alert(document.domain)-&#34;'
    monkeypatch.setattr(ds, "get_daemons_status", lambda *a, **k: [
        {"file": "com.example.off.plist", "label": hostile, "loaded": False,
         "pid": None, "program_arguments": [], "run_at_load": False,
         "keep_alive": False, "schedule": None},
    ])

    output = ds.render_daemons_page()

    # Scope to the daemons table so page chrome (e.g. the sidebar's own
    # data-confirm-adjacent attributes, if any) can't shift which attribute
    # this test picks up.
    table_html = output.split("<table class='daemons'>")[1]
    attr_value = table_html.split('data-confirm="')[1].split('"')[0]
    # The raw entity must not survive into the attribute - its `&` is escaped,
    # so the HTML parser can never decode it back into a quote.
    assert "&#34;" not in attr_value
    assert "&amp;#34;" in attr_value

    # Now model what the browser actually does: the HTML parser decodes the
    # attribute value. After that decode the hostile Label must still be
    # inert text with no real `"` characters at all - none of them can have
    # closed the attribute early.
    decoded = html.unescape(attr_value)
    expected_msg = (
        f"Enable {hostile}? This will let it start running on its schedule."
    )
    assert decoded == expected_msg
    assert decoded.count('"') == 0


def test_shared_shell_includes_custom_confirm_dialog():
    output = ds.render_overview_page()

    assert '<dialog class="confirm-dialog" id="confirm-dialog">' in output
    assert "data-confirm-cancel" in output
    assert "data-confirm-ok" in output


def test_no_native_confirm_calls_remain(monkeypatch):
    """Every destructive action must go through the custom MD3 dialog
    (data-confirm), never the browser's native, unstyled confirm()."""
    monkeypatch.setattr(ds, "get_daemons_status", lambda *a, **k: [
        {"file": "com.example.off.plist", "label": "off", "loaded": False,
         "pid": None, "program_arguments": [], "run_at_load": False,
         "keep_alive": False, "schedule": None},
    ])

    output = ds.render_daemons_page()

    assert "confirm(" not in output
    assert "data-confirm=" in output


def test_render_daemons_page_malformed_plist_error_row_spans_the_whole_table(monkeypatch):
    monkeypatch.setattr(ds, "get_daemons_status", lambda *a, **k: [
        {"file": "com.example.bad.plist", "error": "not a plist"},
    ])

    output = ds.render_daemons_page()

    header = output.split("<thead>")[1].split("</thead>")[0]
    column_count = header.count("<th>")
    assert column_count == 6
    # First cell holds the filename, so the error cell spans the rest.
    assert f"<td colspan='{column_count - 1}'>error parsing plist:" in output


def test_schedule_form_html_prefills_time_and_checks_current_weekdays():
    daemon = {
        "file": "com.example.foo.plist",
        "schedule": [
            {"Weekday": 1, "Hour": 10, "Minute": 30},
            {"Weekday": 3, "Hour": 10, "Minute": 30},
        ],
    }

    form_html = ds._schedule_form_html(daemon, "<input type='hidden' name='csrf_token' value='tok'>")

    assert "value='10:30'" in form_html
    assert "action='/daemons/com.example.foo.plist/schedule'" in form_html
    assert "name='weekday' value='1' checked" in form_html
    assert "name='weekday' value='3' checked" in form_html
    assert "name='weekday' value='2'>" in form_html  # not checked


def test_schedule_form_html_prechecks_every_day_when_no_weekday_key():
    daemon = {"file": "com.example.foo.plist", "schedule": {"Hour": 9, "Minute": 0}}

    form_html = ds._schedule_form_html(daemon, "<input type='hidden' name='csrf_token' value='tok'>")

    for value in range(7):
        assert f"name='weekday' value='{value}' checked" in form_html


def test_schedule_form_html_daily_mode_hides_weekly_and_monthly_controls():
    daemon = {"file": "com.example.foo.plist", "schedule": {"Hour": 9, "Minute": 0}}

    form_html = ds._schedule_form_html(daemon, "<input type='hidden' name='csrf_token' value='tok'>")

    weekly_block = form_html.split("class='weekday-checks weekly-controls'")[1].split(">")[0]
    monthly_block = form_html.split("class='monthly-controls'")[1].split(">")[0]
    assert "display:none" in weekly_block
    assert "display:none" in monthly_block


def test_schedule_form_html_weekly_mode_shows_weekly_hides_monthly():
    daemon = {
        "file": "com.example.foo.plist",
        "schedule": [{"Weekday": 1, "Hour": 10, "Minute": 30}, {"Weekday": 3, "Hour": 10, "Minute": 30}],
    }

    form_html = ds._schedule_form_html(daemon, "<input type='hidden' name='csrf_token' value='tok'>")

    weekly_block = form_html.split("class='weekday-checks weekly-controls'")[1].split(">")[0]
    monthly_block = form_html.split("class='monthly-controls'")[1].split(">")[0]
    assert "display:none" not in weekly_block
    assert "display:none" in monthly_block


def test_schedule_form_html_monthly_mode_shows_monthly_hides_weekly_and_prefills_day():
    daemon = {"file": "com.example.foo.plist", "schedule": {"Day": 15, "Hour": 9, "Minute": 0}}

    form_html = ds._schedule_form_html(daemon, "<input type='hidden' name='csrf_token' value='tok'>")

    weekly_block = form_html.split("class='weekday-checks weekly-controls'")[1].split(">")[0]
    monthly_block = form_html.split("class='monthly-controls'")[1].split(">")[0]
    assert "display:none" in weekly_block
    assert "display:none" not in monthly_block
    assert "value='15'" in form_html


def test_schedule_form_html_includes_frequency_select():
    daemon = {"file": "com.example.foo.plist", "schedule": {"Hour": 9, "Minute": 0}}

    form_html = ds._schedule_form_html(daemon, "<input type='hidden' name='csrf_token' value='tok'>")

    assert "name='frequency'" in form_html
    assert "value='Daily' selected" in form_html


def test_render_daemons_page_includes_schedule_form_for_scheduled_daemon(monkeypatch):
    monkeypatch.setattr(ds, "get_daemons_status", lambda *a, **k: [
        {"file": "com.example.foo.plist", "label": "com.example.foo", "loaded": False, "pid": None,
         "program_arguments": ["/bin/true"], "run_at_load": False, "keep_alive": False,
         "schedule": {"Hour": 9, "Minute": 0}, "stdout_path": None, "stderr_path": None},
    ])

    output = ds.render_daemons_page()

    assert "/daemons/com.example.foo.plist/schedule" in output


def test_render_daemons_page_omits_schedule_form_for_always_on_daemon(monkeypatch):
    monkeypatch.setattr(ds, "get_daemons_status", lambda *a, **k: [
        {"file": "com.example.always-on.plist", "label": "com.example.always-on", "loaded": True, "pid": "123",
         "program_arguments": ["/bin/true"], "run_at_load": True, "keep_alive": True,
         "schedule": None, "stdout_path": None, "stderr_path": None},
    ])

    output = ds.render_daemons_page()

    assert "/schedule" not in output


def test_nav_link_marks_matching_key_active():
    link = ds._nav_link("history", "/history", "Run History", "<svg>icon</svg>", active_page="history")
    assert link == (
        "<a href='/history' title='Run History' class='active'>"
        "<span class='nav-icon'><svg>icon</svg></span><span class='nav-label'>Run History</span></a>"
    )


def test_nav_link_not_active_for_non_matching_key():
    link = ds._nav_link("history", "/history", "Run History", "<svg>icon</svg>", active_page="overview")
    assert link == (
        "<a href='/history' title='Run History'>"
        "<span class='nav-icon'><svg>icon</svg></span><span class='nav-label'>Run History</span></a>"
    )


def test_nav_items_each_carry_a_material_symbols_icon():
    # "notifications" and "gitlab" are excluded: they render real brand
    # marks (inline SVGs), not Material Symbols glyphs - see
    # test_slack_nav_item_present_with_icon and
    # test_gitlab_nav_item_present_with_icon.
    expected_names = {
        "overview": "space_dashboard",
        "analytics": "monitoring",
        "history": "history",
        "memory": "lightbulb",
        "daemons": "dns",
        "skills": "extension",
        "settings": "settings",
        "activity": "bolt",
        "readme": "description",
        "preferences": "palette",
        "instructions": "edit_note",
        "topic_monitor": "newspaper",
        "topic_settings": "settings",
        "logs": "terminal",
        "ai_cli": "smart_toy",
    }
    for key, href, label, icon in ds._NAV_ITEMS:
        if key in ("notifications", "gitlab"):
            continue
        assert icon == f"<span class='material-symbols-outlined' aria-hidden='true'>{expected_names[key]}</span>"


def test_gitlab_nav_item_present_with_icon():
    matching = [item for item in ds._NAV_ITEMS if item[0] == "gitlab"]
    assert len(matching) == 1
    key, href, label, icon = matching[0]
    assert href == "/gitlab"
    assert label == "Live GitLab"
    # The real GitLab "tanuki" brand mark - an inline SVG, not a Material
    # Symbols glyph (there's no generic "GitLab" glyph in that icon set) -
    # drawn in currentColor, same reasoning as the Slack mark, so it
    # matches every other nav icon's color across accent/theme changes.
    assert icon == ds._SECTION_ICON_GITLAB
    assert icon.startswith("<svg")
    assert "fill='currentColor'" in icon


def test_settings_nav_item_present_with_icon():
    matching = [item for item in ds._NAV_ITEMS if item[0] == "settings"]
    assert len(matching) == 1
    key, href, label, icon = matching[0]
    assert href == "/settings"
    assert label == "GitLab"
    assert icon == "<span class='material-symbols-outlined' aria-hidden='true'>settings</span>"


def test_slack_nav_item_present_with_icon():
    matching = [item for item in ds._NAV_ITEMS if item[0] == "notifications"]
    assert len(matching) == 1
    key, href, label, icon = matching[0]
    assert href == "/notifications"
    assert label == "Notifications"
    # The Slack mark - an inline SVG, not a Material Symbols glyph (there's
    # no generic "Slack" glyph in that icon set) - but drawn in
    # currentColor so it matches every other nav icon's color exactly,
    # including across accent/theme changes, rather than Slack's fixed
    # brand colors.
    assert icon == ds._SECTION_ICON_SLACK
    assert icon.startswith("<svg")
    assert "fill='currentColor'" in icon
    assert "#e01e5a" not in icon and "#36c5f0" not in icon and "#2eb67d" not in icon and "#ecb22e" not in icon


def test_activity_nav_item_present_with_icon():
    matching = [item for item in ds._NAV_ITEMS if item[0] == "activity"]
    assert len(matching) == 1
    key, href, label, icon = matching[0]
    assert href == "/activity"
    assert label == "Activity"
    assert icon == "<span class='material-symbols-outlined' aria-hidden='true'>bolt</span>"


def test_topic_monitor_nav_item_present_with_icon():
    matching = [item for item in ds._NAV_ITEMS if item[0] == "topic_monitor"]
    assert len(matching) == 1
    key, href, label, icon = matching[0]
    assert href == "/topic-monitor"
    assert label == "Topic Monitor"
    assert icon == "<span class='material-symbols-outlined' aria-hidden='true'>newspaper</span>"


def test_topic_settings_nav_item_present_with_icon():
    matching = [item for item in ds._NAV_ITEMS if item[0] == "topic_settings"]
    assert len(matching) == 1
    key, href, label, icon = matching[0]
    assert href == "/topic-monitor/settings"
    assert label == "Topic Settings"
    assert icon == "<span class='material-symbols-outlined' aria-hidden='true'>settings</span>"


def test_check_and_dot_icons_are_material_symbols():
    assert ds._CHECK_ICON == "<span class='material-symbols-outlined' aria-hidden='true'>check_circle</span>"
    assert ds._DOT_ICON_TEMPLATE.format(cls="") == "<span class='material-symbols-outlined ' aria-hidden='true'>circle</span>"


def test_status_badge_uses_the_spinner_icon_while_running():
    """The "running" state used to show a plain pulsing dot - a different,
    less lively treatment than the Activity page's own two-ring spinner
    for the exact same "actively working" concept. Both now use the same
    _SPINNER_ICON, sized to fit inline in a pill via em units."""
    badge_class, icon = ds._status_badge("running")
    assert badge_class == "pill-blue"
    assert icon == ds._SPINNER_ICON
    assert icon == "<span class='md-spinner md-spinner-pill' aria-hidden='true'></span>"


def test_other_states_still_use_the_plain_dot_or_check_icon():
    assert ds._status_badge("idle") == ("pill-green", ds._CHECK_ICON)
    assert ds._status_badge("never_run") == ("pill-grey", ds._DOT_ICON_TEMPLATE.format(cls=""))
    assert ds._status_badge("failed") == ("pill-red", ds._DOT_ICON_TEMPLATE.format(cls=""))


def test_material_symbols_icons_are_aria_hidden():
    assert "aria-hidden='true'" in ds._CHECK_ICON
    assert "aria-hidden='true'" in ds._DOT_ICON_TEMPLATE
    for key, href, label, icon in ds._NAV_ITEMS:
        assert "aria-hidden='true'" in icon


def test_sidebar_toggle_icon_is_material_symbols():
    assert ds._SIDEBAR_TOGGLE_ICON == "<span class='material-symbols-outlined'>chevron_left</span>"


def test_brand_mark_icon_is_still_the_hand_drawn_svg():
    """The brand mark is explicitly excluded from the Material Symbols
    migration - it must stay exactly the SVG it already was."""
    assert ds._BRAND_MARK_ICON.count("<circle") == 2
    assert "material-symbols-outlined" not in ds._BRAND_MARK_ICON


def test_topbar_page_title_starts_hidden_and_reveals_via_a_class():
    assert ".topbar-page-title {" in ds._STYLE
    base_rule = ds._STYLE.split(".topbar-page-title {")[1].split("}")[0]
    assert "opacity: 0" in base_rule
    assert ".topbar-page-title.is-visible {" in ds._STYLE
    visible_rule = ds._STYLE.split(".topbar-page-title.is-visible {")[1].split("}")[0]
    assert "opacity: 1" in visible_rule


def test_topbar_page_title_is_large_and_bold():
    base_rule = ds._STYLE.split(".topbar-page-title {")[1].split("}")[0]
    assert "font-weight: 700" in base_rule
    size = base_rule.split("font-size:")[1].split(";")[0].strip()
    assert size not in ("0.95rem", "1rem")  # bigger than the earlier, non-bold size


def test_style_includes_material_symbols_base_and_sizing_rules():
    assert ".material-symbols-outlined {" in ds._STYLE
    assert ".sidebar-toggle .material-symbols-outlined" in ds._STYLE
    assert "html.collapsed .sidebar-toggle .material-symbols-outlined" in ds._STYLE
    assert ".section-header .material-symbols-outlined" in ds._STYLE
    assert ".sidebar-toggle svg" not in ds._STYLE
    # No blanket ".section-header svg" rule - the Slack mark gets its own
    # specific ".slack-mark" selector instead (see
    # test_slack_mark_matches_other_section_header_icon_color), same as
    # every other section-header icon getting its own glyph rather than a
    # generic element-type rule.
    assert ".section-header svg" not in ds._STYLE


def test_overview_layout_goes_two_column_on_wide_screens():
    assert "@media (min-width: 901px)" in ds._STYLE
    rule = ds._STYLE.split("@media (min-width: 901px)")[1].split("}}")[0]
    assert ".overview-layout" in rule
    assert "grid-template-columns:" in rule


def test_pill_lg_is_bigger_than_the_base_pill():
    assert ".pill-lg {" in ds._STYLE
    base_padding = ds._STYLE.split(".pill {")[1].split("padding:")[1].split(";")[0].strip()
    lg_padding = ds._STYLE.split(".pill-lg {")[1].split("padding:")[1].split(";")[0].strip()
    assert base_padding != lg_padding


def test_run_now_action_has_a_separating_top_border():
    """.run-now-action - shared by the GitLab loop and Topic Monitor
    sections of render_activity_page, and render_topic_monitor_page's own
    button, not overview-specific despite the earlier name."""
    assert ".run-now-action {" in ds._STYLE
    rule = ds._STYLE.split(".run-now-action {")[1].split("}")[0]
    assert "border-top" in rule


def test_run_now_action_disabled_button_looks_disabled():
    assert ".run-now-action button:disabled {" in ds._STYLE
    rule = ds._STYLE.split(".run-now-action button:disabled {")[1].split("}")[0]
    assert "cursor: not-allowed" in rule


def test_slack_mark_matches_other_section_header_icon_color():
    # The section-header rule that colors every Material Symbols icon
    # --md-primary must also cover the Slack mark, via its currentColor
    # fill, so it looks identical to every other section-header icon in
    # both light/dark mode and every accent choice.
    assert ".section-header .slack-mark" in ds._STYLE
    rule = ds._STYLE.split(".section-header .slack-mark")[1].split("}")[0]
    assert "var(--md-primary)" in rule


def test_gitlab_mark_matches_other_section_header_icon_color():
    assert ".section-header .gitlab-mark" in ds._STYLE
    rule = ds._STYLE.split(".section-header .gitlab-mark")[1].split("}")[0]
    assert "var(--md-primary)" in rule


def test_gitlab_mark_sized_to_match_the_tab_buttons_material_icon():
    """The GitLab Monitor tab button's SVG mark and the Topic Monitor tab
    button's Material Symbols glyph sit side by side - they must render at
    the same size."""
    assert ".tab-button svg {" in ds._STYLE
    rule = ds._STYLE.split(".tab-button svg {")[1].split("}")[0]
    material_icon_rule = ds._STYLE.split(".tab-button .material-symbols-outlined {")[1].split("}")[0]
    assert "width: 16px" in rule and "height: 16px" in rule
    assert "font-size: 16px" in material_icon_rule


def test_settings_add_and_delete_buttons_carry_icons(monkeypatch, tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({"default": "a", "instances": {"a": {"url": "https://a.example.com", "token": "t"}}, "projects": {"p": {"project_id": "ns/p", "instance": "a"}}}, gitlab_path)
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", gitlab_path)
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "slack.json")

    output = ds.render_settings_page()

    assert "<span class='material-symbols-outlined' aria-hidden='true'>add</span> Add instance" in output
    assert "<span class='material-symbols-outlined' aria-hidden='true'>add</span> Add project" in output
    assert output.count("<span class='material-symbols-outlined' aria-hidden='true'>delete</span> Delete</button>") == 2


def test_settings_page_icon_buttons_are_aria_hidden(monkeypatch, tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({"default": "a", "instances": {"a": {"url": "https://a.example.com", "token": "t"}}, "projects": {"p": {"project_id": "ns/p", "instance": "a"}}}, gitlab_path)
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", gitlab_path)
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "slack.json")

    output = ds.render_settings_page()

    assert "<span class='material-symbols-outlined' aria-hidden='true'>add</span> Add instance" in output
    assert "<span class='material-symbols-outlined' aria-hidden='true'>add</span> Add project" in output
    assert output.count("<span class='material-symbols-outlined' aria-hidden='true'>delete</span> Delete</button>") == 2


def test_sidebar_html_includes_brand_and_toggle_button():
    sidebar = ds._sidebar_html("overview")
    assert "<a class='brand' href='/'>" in sidebar
    assert "<span class='brand-name'>Loop X</span>" in sidebar
    assert "class='sidebar-toggle'" in sidebar
    assert "classList.toggle('collapsed')" in sidebar
    assert "loop-dashboard-sidebar" in sidebar


def test_brand_shows_name_when_expanded_and_icon_when_collapsed():
    """Expanded: name only (the icon mark stays hidden). Collapsed (either
    via the .collapsed toggle or the narrow-viewport rail): icon only."""
    brand_mark_rule = ds._STYLE.split(".brand-mark {")[1].split("}")[0]
    assert "display: none;" in brand_mark_rule

    collapsed_rule = ds._STYLE.split("html.collapsed .brand-mark {")[1].split("}")[0]
    assert "display: inline-flex;" in collapsed_rule

    mobile_block = ds._STYLE.split("@media (max-width: 720px) {")[1].split("}}")[0]
    assert ".brand-mark { display: inline-flex; }" in mobile_block


def test_collapsed_sidebar_top_stacks_brand_and_toggle_instead_of_squeezing_them():
    """Side by side, the brand icon (~20px) + gap + toggle button (~28px,
    flex-shrink: 0) don't fit the collapsed rail's ~32px content box - the
    icon (in the only shrinkable child, .brand) got crushed down to
    nothing by flex-shrink + overflow: hidden. Stacking them vertically
    means neither has to shrink."""
    collapsed_rule = ds._STYLE.split("html.collapsed .sidebar-top {")[1].split("}")[0]

    assert "flex-direction: column;" in collapsed_rule
    sidebar = ds._sidebar_html("overview")
    for label in ("Dashboard", "Run History", "Live GitLab", "Memory", "Daemons", "GitLab", "Activity"):
        assert label in sidebar
    # The GitLab settings page lives in the Configuration group, which
    # comes after System in _NAV_GROUPS, regardless of _NAV_ITEMS's own
    # ordering. Order here follows _NAV_GROUPS (Monitor:
    # gitlab/memory/activity/history, then System: daemons/skills), not
    # _NAV_ITEMS's own tuple order.
    assert (
        sidebar.index("Dashboard") < sidebar.index("Live GitLab")
        < sidebar.index("Memory") < sidebar.index("Activity")
        < sidebar.index("Run History") < sidebar.index("Daemons") < sidebar.index("title='GitLab'")
    )


def test_sidebar_html_groups_settings_preferences_instructions_under_configuration():
    sidebar = ds._sidebar_html("overview")
    assert "sidebar-group-label'>Configuration<" in sidebar
    config_index = sidebar.index("sidebar-group-label'>Configuration<")
    config_block = sidebar[config_index:]
    assert "title='GitLab'" in config_block
    assert "Notifications" in config_block
    assert "Preferences" in config_block
    assert "Instructions" in config_block
    assert "Topic Settings" in config_block
    # Activity is a Monitor-group page, not Configuration - it must appear
    # before the Configuration label, not inside its block.
    assert sidebar.index("title='Activity'") < config_index


def test_sidebar_html_marks_active_page():
    sidebar = ds._sidebar_html("daemons")
    assert "<a href='/daemons' title='Daemons' class='active'>" in sidebar
    assert "<a href='/' title='Dashboard' class='active'>" not in sidebar


def test_sidebar_html_groups_main_nav_items_with_labels():
    """Dashboard stands alone (no label - it's the landing page, not part
    of a group); the rest cluster under Monitor/System/Configuration/Docs
    labels, with Docs deliberately last (least-visited group)."""
    sidebar = ds._sidebar_html("overview")

    overview_index = sidebar.index("title='Dashboard'")
    monitor_index = sidebar.index("sidebar-group-label'>Monitor<")
    history_index = sidebar.index("title='Run History'")
    system_index = sidebar.index("sidebar-group-label'>System<")
    daemons_index = sidebar.index("title='Daemons'")
    docs_index = sidebar.index("sidebar-group-label'>Docs<")
    readme_index = sidebar.index("title='README'")
    config_index = sidebar.index("sidebar-group-label'>Configuration<")
    settings_index = sidebar.index("title='GitLab'")

    # Dashboard comes before any group label - it isn't inside one
    assert overview_index < monitor_index
    for label in ("Monitor", "System", "Docs", "Configuration"):
        assert f"sidebar-group-label'>{label}<" in sidebar

    assert monitor_index < history_index < system_index
    assert system_index < daemons_index < config_index
    assert config_index < settings_index < docs_index < readme_index

    gitlab_index = sidebar.index("title='Live GitLab'")
    memory_index = sidebar.index("title='Memory'")
    activity_index = sidebar.index("title='Activity'")
    # Within Monitor, Run History is last (gitlab/memory/activity/history)
    assert monitor_index < gitlab_index < memory_index < activity_index < history_index

    for key in (
        "history", "gitlab", "memory", "activity", "daemons", "skills", "readme",
        "settings", "preferences", "instructions", "topic_settings",
    ):
        assert ds._NAV_GROUP_OF[key]

    assert ds._NAV_GROUP_OF["history"] == "Monitor"
    assert ds._NAV_GROUP_OF["gitlab"] == "Monitor"
    assert ds._NAV_GROUP_OF["memory"] == "Monitor"
    assert ds._NAV_GROUP_OF["activity"] == "Monitor"
    assert ds._NAV_GROUP_OF["daemons"] == "System"
    assert ds._NAV_GROUP_OF["skills"] == "System"
    assert ds._NAV_GROUP_OF["readme"] == "Docs"
    assert ds._NAV_GROUP_OF["settings"] == "Configuration"
    assert ds._NAV_GROUP_OF["preferences"] == "Configuration"
    assert ds._NAV_GROUP_OF["instructions"] == "Configuration"
    assert ds._NAV_GROUP_OF["topic_settings"] == "Configuration"
    assert "overview" not in ds._NAV_GROUP_OF


def test_sidebar_group_labels_hide_when_collapsed():
    assert ".sidebar-group-label" in ds._STYLE
    assert "html.collapsed .sidebar-group-label" in ds._STYLE


def test_status_badge_markup_escapes_and_labels_state():
    markup = ds._status_badge_markup({"state": "idle"})
    assert "pill-green" in markup
    assert "Idle" in markup


def test_render_shell_omits_auto_refresh_by_default():
    """Auto-refresh defaults to off - only render_gitlab_page,
    render_topic_monitor_page, and render_activity_page opt in explicitly
    (refresh=True), since those are the only pages whose data changes out
    from under a reader while they watch it. Every other page must pass
    refresh=True explicitly to get it."""
    page = ds._render_shell("Test Title", "overview", "<span>badge</span>", "<p>body</p>")
    assert "location.reload()" not in page
    assert "auto-refreshes every 30s" not in page
    assert "<title>Test Title</title>" in page
    assert "<span>badge</span>" in page
    assert "<p>body</p>" in page


def test_render_shell_schedules_a_configurable_auto_refresh_when_enabled():
    """Auto-refresh is JS-driven (setTimeout + reload), not a fixed
    <meta http-equiv="refresh">, so the interval can be a per-browser
    preference (see render_preferences_page) rather than hardcoded."""
    page = ds._render_shell("Test Title", "overview", "<span>badge</span>", "<p>body</p>", refresh=True)
    assert '<meta http-equiv="refresh"' not in page
    assert "loop-dashboard-refresh-interval" in page
    assert "setTimeout(function() {" in page
    assert "location.reload();" in page
    assert "refreshSeconds * 1000);" in page


def test_render_shell_omits_refresh_scheduling_when_disabled():
    page = ds._render_shell("Test Title", "overview", "<span>badge</span>", "<p>body</p>", refresh=False)
    assert "location.reload()" not in page


def test_render_shell_escapes_title():
    page = ds._render_shell("<script>x</script>", "overview", "<span>badge</span>", "<p>b</p>")
    assert "<script>x</script>" not in page
    assert "&lt;script&gt;x&lt;/script&gt;" in page


def test_render_shell_marks_current_page_active_and_includes_badge():
    page = ds._render_shell(
        "Test", "gitlab", "<span class='pill pill-green'>Idle</span>", "<p>b</p>", refresh_note=True
    )
    assert "<a href='/gitlab' title='Live GitLab' class='active'>" in page
    assert "<a href='/' title='Dashboard' class='active'>" not in page
    assert "<span class='pill pill-green'>Idle</span>" in page
    assert "auto-refreshes every 30s" in page


def test_render_shell_omits_refresh_note_when_disabled_but_keeps_sidebar_and_badge():
    page = ds._render_shell("Test", "history", "<span class='pill pill-green'>Idle</span>", "<p>b</p>", refresh_note=False)
    assert "auto-refreshes every 30s" not in page
    assert "<a href='/history' title='Run History' class='active'>" in page
    assert "<span class='pill pill-green'>Idle</span>" in page


def test_render_shell_updates_refresh_note_text_from_the_saved_interval():
    page = ds._render_shell("Test", "overview", "<span>badge</span>", "<p>b</p>", refresh_note=True)
    assert "id='refresh-note-text'" in page
    assert "getElementById('refresh-note-text')" in page


def test_render_shell_omits_refresh_note_script_when_note_disabled():
    page = ds._render_shell("Test", "history", "<span>badge</span>", "<p>b</p>", refresh_note=False)
    assert "getElementById('refresh-note-text')" not in page


def test_render_shell_head_script_reads_collapsed_state_before_paint():
    page = ds._render_shell("Test", "overview", "<span>badge</span>", "<p>b</p>")
    head, _, _ = page.partition("<body>")
    assert "loop-dashboard-sidebar" in head
    assert "classList.add('collapsed')" in head


def test_render_shell_head_script_restores_color_mode_and_accent_before_paint():
    page = ds._render_shell("Test", "overview", "<span>badge</span>", "<p>b</p>")
    head, _, _ = page.partition("<body>")

    assert "loop-dashboard-color-mode" in head
    assert "loop-dashboard-accent" in head
    assert "setAttribute('data-color-mode'" in head
    # accent always ends up set (defaulting to 'default' - no sidebar/
    # topbar tint), unlike color-mode which stays absent for "auto" -
    # never write data-color-mode for a value that isn't 'light'/'dark'.
    assert "setAttribute('data-accent', accent || 'default')" in head


def test_render_shell_wraps_content_in_sidebar_and_content_area():
    page = ds._render_shell("Test", "overview", "<span>badge</span>", "<p>body</p>")
    assert '<aside class="sidebar">' in page
    assert '<main class="content-area">' in page
    assert '<div class="topbar">' in page


def test_render_shell_topbar_has_a_page_title_slot_for_scroll_reveal():
    """Every page's own <h1> lives below the sticky topbar, so once it's
    scrolled out of view there's nothing left on screen saying which page
    this is. A page-title slot in the topbar itself (revealed via the
    IntersectionObserver script below, once the real <h1> scrolls behind
    it) fixes that without duplicating the title on every page's initial
    render."""
    page = ds._render_shell("Test", "overview", "<span>badge</span>", "<p><h1>Body Title</h1></p>")

    assert "<span class=\"topbar-page-title\" id=\"topbar-page-title\"></span>" in page


def test_render_shell_page_title_reveal_script_observes_the_real_h1():
    page = ds._render_shell("Test", "overview", "<span>badge</span>", "<p><h1>Body Title</h1></p>")
    head, _, _ = page.partition("<body>")

    assert "IntersectionObserver" in head
    assert "topbar-page-title" in head
    assert "content-area h1" in head


def test_render_activity_page_shows_latest_run_and_review(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "LOOP_DIR", tmp_path)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path / "does-not-exist")
    ds.write_status("idle", status_path=status_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "daily-review.md").write_text("All good.")

    output = ds.render_activity_page()

    assert "<h1>Activity</h1>" in output
    assert "GitLab Monitor" in output
    assert "Latest Run Review</h2>" in output
    assert "All good." in output


def test_render_activity_page_auto_refreshes(tmp_path, monkeypatch):
    """Activity is one of the three pages (with Live GitLab and Topic
    Monitor) whose data changes out from under a reader while they watch
    it, so it opts into _render_shell's auto-refresh explicitly."""
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "LOOP_DIR", tmp_path)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path / "does-not-exist")
    ds.write_status("idle", status_path=status_path)

    output = ds.render_activity_page()

    assert "auto-refreshes every 30s" in output
    assert "location.reload();" in output


def test_render_activity_page_gitlab_and_topic_monitor_are_stacked_cards_gitlab_first(tmp_path, monkeypatch):
    """GitLab Monitor and Topic Monitor used to be tabs of one panel, so
    only one showed at a time. They're now two always-visible stacked
    cards (see .activity-card-stack) so seeing either never requires a
    tab click, with GitLab Monitor first."""
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "LOOP_DIR", tmp_path)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path / "does-not-exist")
    ds.write_status("idle", status_path=status_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "daily-review.md").write_text("All good.")

    output = ds.render_activity_page()

    assert "tab-group" not in output
    assert "class=\"activity-card-stack\"" in output
    assert "<h2>GitLab Monitor</h2>" in output
    assert "<h2>Topic Monitor</h2>" in output
    assert output.index("<h2>GitLab Monitor</h2>") < output.index("<h2>Topic Monitor</h2>")


def test_render_activity_page_shows_state_as_a_large_hero_pill(tmp_path, monkeypatch):
    """State used to be just another <li> in the field list, the same
    visual weight as "Updated at" - it's the single most important value
    on the page, so it gets its own large pill instead, and drops out of
    the plain field list."""
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "LOOP_DIR", tmp_path)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path / "does-not-exist")
    ds.write_status("idle", status_path=status_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "daily-review.md").write_text("All good.")

    output = ds.render_activity_page()

    assert "<div class='status-hero'>" in output
    assert "pill pill-lg pill-green" in output
    assert "<span class='k'>State</span>" not in output


def test_render_activity_page_wraps_run_now_in_its_own_action_area(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "LOOP_DIR", tmp_path)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path / "does-not-exist")
    ds.write_status("idle", status_path=status_path)
    monkeypatch.setattr(ds, "read_topic_status", lambda *a, **k: {"topics": {}})
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "daily-review.md").write_text("All good.")

    output = ds.render_activity_page()

    # Present regardless of whether the button inside ends up enabled or
    # disabled (see test_render_activity_page_disables_gitlab_run_now_*
    # below) - the action area itself is unconditional, one per loop.
    assert output.count("<div class='run-now-action'>") == 2


def test_render_activity_page_puts_latest_run_and_review_in_one_responsive_layout(tmp_path, monkeypatch):
    """Latest Run (a short status summary) and Latest Run Review (a long
    report) used to sit in two separate full-width .grid wrappers, always
    stacked even on a wide desktop screen. They now share one
    .overview-layout container that goes two-column - a narrow status
    rail beside the wide report - once there's room (see the
    .overview-layout media rule in _STYLE)."""
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "LOOP_DIR", tmp_path)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path / "does-not-exist")
    ds.write_status("idle", status_path=status_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "daily-review.md").write_text("All good.")

    output = ds.render_activity_page()

    assert output.count("<div class=\"overview-layout\">") == 1
    assert "<div class=\"grid\">" not in output


def test_render_activity_page_subtitle_is_direct_and_active_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path / "does-not-exist")
    output = ds.render_activity_page()

    assert (
        "<p class=\"subtitle\">What each automated loop is doing right now, "
        "plus the GitLab loop's most recent report.</p>"
    ) in output


def test_render_activity_page_shows_run_now_button_when_idle(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "LOOP_DIR", tmp_path)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path / "does-not-exist")
    ds.write_status("idle", status_path=status_path)
    monkeypatch.setattr(ds, "read_loop_projects_config", lambda *a, **k: {"projects": {"demo": {}}})
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "daily-review.md").write_text("All good.")

    output = ds.render_activity_page()

    assert "action='/run-now'" in output
    assert "Run now" in output
    assert "class='btn btn-primary'" in output


def test_render_activity_page_hides_run_now_button_when_already_running(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "LOOP_DIR", tmp_path)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path / "does-not-exist")
    ds.write_status("running", status_path=status_path)
    monkeypatch.setattr(ds, "read_loop_projects_config", lambda *a, **k: {"projects": {"demo": {}}})
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "daily-review.md").write_text("All good.")

    output = ds.render_activity_page()

    assert "action='/run-now'" not in output


def test_render_activity_page_disables_gitlab_run_now_when_no_projects_configured(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "LOOP_DIR", tmp_path)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path / "does-not-exist")
    ds.write_status("idle", status_path=status_path)
    monkeypatch.setattr(ds, "read_loop_projects_config", lambda *a, **k: {})
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "daily-review.md").write_text("All good.")

    output = ds.render_activity_page()

    assert "action='/run-now'" not in output
    assert "<button type='button' class='btn btn-primary' disabled>" in output
    assert "No projects configured yet" in output
    assert "<a href='/settings'>" in output


def test_render_activity_page_shows_topic_monitor_section(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "LOOP_DIR", tmp_path)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path / "does-not-exist")
    ds.write_status("idle", status_path=status_path)
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [
        {"name": "ai-news", "label": "AI news", "brief": "x", "slack_bundle": None},
    ])
    monkeypatch.setattr(ds, "read_topic_status", lambda *a, **k: {
        "topics": {"ai-news": {"state": "idle", "updated_at": "2026-08-22T09:00:00+00:00"}}
    })
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "daily-review.md").write_text("All good.")

    output = ds.render_activity_page()

    assert "<h2>Topic Monitor</h2>" in output
    assert "action='/topic-monitor/run-now'" in output


def test_render_activity_page_shows_latest_topic_run_review(tmp_path, monkeypatch):
    """Latest Topic Run Review surfaces each configured topic's most
    recent saved briefing, stacked below Latest Run Review in the wide
    column - same _topic_latest_data_html rendering the Topic Monitor
    page's own Latest Data section uses, so this never requires leaving
    the Activity page to see the newest topic data."""
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "LOOP_DIR", tmp_path)
    ds.write_status("idle", status_path=status_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "daily-review.md").write_text("All good.")
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [
        {"name": "ai-news", "label": "AI news", "brief": "x", "slack_bundle": None},
    ])
    monkeypatch.setattr(ds, "read_topic_status", lambda *a, **k: {"topics": {}})
    topic_history_dir = tmp_path / "topic-history"
    topic_history_dir.mkdir()
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", topic_history_dir)
    (topic_history_dir / "2026-08-22-ai-news.md").write_text(
        "# AI news - 2026-08-22\n\nA new model shipped today with major gains.\n"
    )

    output = ds.render_activity_page()

    assert "<h2>Latest Topic Run Review</h2>" in output
    assert "A new model shipped today with major gains." in output
    assert output.index("<h2>Latest Run Review</h2>") < output.index("<h2>Latest Topic Run Review</h2>")


def test_render_activity_page_disables_topic_run_now_when_no_topics_configured(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "LOOP_DIR", tmp_path)
    ds.write_status("idle", status_path=status_path)
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [])
    monkeypatch.setattr(ds, "read_topic_status", lambda *a, **k: {"topics": {}})
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "daily-review.md").write_text("All good.")

    output = ds.render_activity_page()

    assert "action='/topic-monitor/run-now'" not in output
    assert "No topics configured yet" in output
    assert "<a href='/topic-monitor/settings'>" in output


def test_render_activity_page_hides_topic_run_now_while_a_topic_is_running(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "LOOP_DIR", tmp_path)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path / "does-not-exist")
    ds.write_status("idle", status_path=status_path)
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [
        {"name": "ai-news", "label": "AI news", "brief": "x", "slack_bundle": None},
    ])
    monkeypatch.setattr(ds, "read_topic_status", lambda *a, **k: {
        "topics": {"ai-news": {"state": "running", "current_step": "researching"}}
    })
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "daily-review.md").write_text("All good.")

    output = ds.render_activity_page()

    assert "action='/topic-monitor/run-now'" not in output
    assert "No topics configured yet" not in output
    assert "Researching" in output


def test_render_activity_page_formats_updated_at_as_relative_time(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "LOOP_DIR", tmp_path)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path / "does-not-exist")
    now = datetime.now(timezone.utc)
    stale_timestamp = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000000+00:00")
    status_path.write_text(json.dumps({"state": "idle", "updated_at": stale_timestamp}))
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "daily-review.md").write_text("All good.")

    output = ds.render_activity_page()

    # The full timestamp is intentionally kept, but only as a hover tooltip
    # (title=) - the visible text is the relative time.
    assert "<span class='k'>Updated at</span>" in output
    assert f"<span title='{stale_timestamp}'>3h ago</span>" in output


def test_render_markdown_headings():
    assert ds.render_markdown("# Title\n\n## Section") == (
        '<h1 id="title">Title</h1>\n<h2 id="section">Section</h2>'
    )


def test_render_markdown_unordered_and_ordered_lists():
    output = ds.render_markdown("- one\n- two\n\n1. first\n2. second")
    assert output == "<ul><li>one</li><li>two</li></ul>\n<ol><li>first</li><li>second</li></ol>"


def test_render_markdown_bold_italic_and_inline_code():
    output = ds.render_markdown("**bold** and *italic* and `code`")
    assert output == "<p><strong>bold</strong> and <em>italic</em> and <code>code</code></p>"


def test_render_markdown_code_span_contents_are_not_reformatted():
    output = ds.render_markdown("`**not bold**`")
    assert output == "<p><code>**not bold**</code></p>"


def test_render_markdown_fenced_code_block_is_not_processed_as_markdown():
    output = ds.render_markdown("```\n# not a heading\n**not bold**\n```")
    assert output == "<pre><code># not a heading\n**not bold**</code></pre>"


def test_render_markdown_markdown_link_survives_a_stray_underscore_later_in_the_paragraph():
    """Same regression as the gitlab-reference case, but for a plain
    [text](url) markdown link - its inserted `target="_blank"` is just as
    vulnerable to pairing with an unrelated later underscore if bold/italic
    ran before the link's markup was protected."""
    output = ds.render_markdown("[GitLab](https://gitlab.example.com/issue/1) — in ht_documents.")
    assert (
        '<a href="https://gitlab.example.com/issue/1" rel="noopener" target="_blank">GitLab</a> '
        '— in ht_documents.'
    ) in output
    assert "<em>" not in output


def test_render_markdown_link_with_safe_scheme():
    output = ds.render_markdown("[GitLab](https://gitlab.example.com/issue/1)")
    assert output == '<p><a href="https://gitlab.example.com/issue/1" rel="noopener" target="_blank">GitLab</a></p>'


def test_render_markdown_link_text_that_is_itself_a_code_span():
    """A code-span placeholder gets stashed before link processing runs, so
    a link like [`code`](url) ends up with a stash placeholder nested
    inside another stash placeholder. The restore loop must resolve the
    outer (link) placeholder before the inner (code span) one, or the
    inner marker is inserted into the final text unresolved."""
    output = ds.render_markdown("[`gitlab-config`](https://example.com/gitlab-config)")

    assert output == (
        '<p><a href="https://example.com/gitlab-config" rel="noopener" '
        'target="_blank"><code>gitlab-config</code></a></p>'
    )


def test_render_markdown_link_with_unsafe_scheme_is_left_as_plain_text():
    output = ds.render_markdown("[click me](javascript:alert(1))")
    assert "<a " not in output
    assert "click me" in output


def test_render_markdown_image():
    output = ds.render_markdown("![Loop Engineering](https://example.com/banner.jpeg)")
    assert output == '<p><img src="https://example.com/banner.jpeg" alt="Loop Engineering" loading="lazy"></p>'


def test_render_markdown_image_with_empty_alt():
    output = ds.render_markdown("![](https://example.com/badge.svg)")
    assert output == '<p><img src="https://example.com/badge.svg" alt="" loading="lazy"></p>'


def test_render_markdown_image_does_not_leave_a_stray_bang_before_a_link():
    """Without image handling, `![alt](url)` matches the plain link regex
    too (once the leading `!` is ignored), so this used to render as a
    literal "!" in front of an <a> tag instead of an <img> - exactly what
    happened with README.md's shields.io badges."""
    output = ds.render_markdown("![License](https://img.shields.io/badge/license-MIT-blue)")
    assert "!<a " not in output
    assert '<img src="https://img.shields.io/badge/license-MIT-blue" alt="License" loading="lazy">' in output


def test_render_markdown_image_with_unsafe_scheme_is_left_as_plain_text():
    output = ds.render_markdown("![x](javascript:alert(1))")
    assert "<img " not in output


def test_render_markdown_same_page_anchor_link():
    output = ds.render_markdown("[Dependencies](#dependencies)")
    assert output == '<p><a href="#dependencies" rel="noopener" target="_blank">Dependencies</a></p>'


def test_render_markdown_relative_path_link():
    output = ds.render_markdown("[TASK.md](TASK.md)")
    assert output == '<p><a href="TASK.md" rel="noopener" target="_blank">TASK.md</a></p>'


def test_render_markdown_protocol_relative_link_is_left_as_plain_text():
    output = ds.render_markdown("[click me](//evil.example.com/x)")
    assert "<a " not in output
    assert "click me" in output


def test_render_markdown_autolinks_a_bare_url():
    """A plain https://... mention that was never wrapped in markdown
    [text](url) syntax used to render as inert plain text."""
    output = ds.render_markdown("Check https://example.com/issue/1 for details.")
    assert output == (
        '<p>Check <a href="https://example.com/issue/1" rel="noopener" '
        'target="_blank">https://example.com/issue/1</a> for details.</p>'
    )


def test_render_markdown_autolink_strips_trailing_sentence_punctuation():
    output = ds.render_markdown("See https://example.com/x.")
    assert output == (
        '<p>See <a href="https://example.com/x" rel="noopener" '
        'target="_blank">https://example.com/x</a>.</p>'
    )


def test_render_markdown_autolink_does_not_double_link_an_explicit_markdown_link():
    output = ds.render_markdown("[GitLab](https://gitlab.example.com/issue/1)")
    assert output.count("<a ") == 1


def test_render_markdown_autolink_inside_code_span_is_not_linkified():
    output = ds.render_markdown("Run `curl https://example.com/x`.")
    assert "<a " not in output
    assert "<code>curl https://example.com/x</code>" in output


def test_render_markdown_escapes_embedded_html_so_it_cannot_inject_tags():
    """A review quoting a GitLab issue title verbatim must never let that
    title's content become a real tag, regardless of what markdown-like
    punctuation sits next to it in the source."""
    output = ds.render_markdown("Issue title: <script>alert(1)</script> and **bold**")
    assert "<script>" not in output
    assert "&lt;script&gt;" in output
    assert "<strong>bold</strong>" in output


def test_render_markdown_matches_a_real_daily_review_shape():
    review = (
        "# Daily Review — 2026-08-09\n\n"
        "## Summary\n"
        "Checked 5 open issues assigned to `encore`.\n\n"
        "## Issues checked\n"
        "- brightleaf.web #1206 — Connecting Claude Tag in Slack\n"
        "- orchard #409 — Indexing issue on Google Search Console\n"
    )
    # gitlab_url_prefixes={} keeps this hermetic - the real machine running
    # this test suite may have its own loop config with these exact aliases
    # ("brightleaf.web", "orchard"), which would otherwise silently linkify them
    # and break the plain-text assertion below depending on whose machine
    # the suite runs on.
    output = ds.render_markdown(review, gitlab_url_prefixes={})
    assert '<h1 id="daily-review-2026-08-09">Daily Review — 2026-08-09</h1>' in output
    assert '<h2 id="summary">Summary</h2>' in output
    assert "<p>Checked 5 open issues assigned to <code>encore</code>.</p>" in output
    assert "<ul><li>brightleaf.web #1206 — Connecting Claude Tag in Slack</li>" in output


def test_render_markdown_linkifies_gitlab_alias_references():
    prefixes = {"brightleaf.web": "https://gitlab.acme.com/acme/brightleaf/brightleaf.web"}
    output = ds.render_markdown("- brightleaf.web #1206 — some title", gitlab_url_prefixes=prefixes)
    assert (
        '<li><a href="https://gitlab.acme.com/acme/brightleaf/brightleaf.web/-/issues/1206" '
        'rel="noopener" target="_blank">brightleaf.web #1206</a> — some title</li>'
    ) in output


def test_render_markdown_gitlab_reference_inside_bold_nests_correctly():
    prefixes = {"brightleaf.web": "https://gitlab.acme.com/acme/brightleaf/brightleaf.web"}
    output = ds.render_markdown("**brightleaf.web #1206** — escalated", gitlab_url_prefixes=prefixes)
    assert (
        '<strong><a href="https://gitlab.acme.com/acme/brightleaf/brightleaf.web/-/issues/1206" '
        'rel="noopener" target="_blank">brightleaf.web #1206</a></strong> — escalated'
    ) in output


def test_render_markdown_does_not_linkify_unknown_alias():
    prefixes = {"brightleaf.web": "https://gitlab.acme.com/acme/brightleaf/brightleaf.web"}
    output = ds.render_markdown("some-other-project #55", gitlab_url_prefixes=prefixes)
    assert "<a " not in output


def test_render_markdown_gitlab_reference_inside_code_span_is_not_linkified():
    prefixes = {"brightleaf.web": "https://gitlab.acme.com/acme/brightleaf/brightleaf.web"}
    output = ds.render_markdown("`brightleaf.web #1206`", gitlab_url_prefixes=prefixes)
    assert output == "<p><code>brightleaf.web #1206</code></p>"


def test_render_markdown_linkified_reference_survives_a_stray_underscore_later_in_the_paragraph():
    """Regression test: a linkified <a> tag's `target="_blank"` has exactly
    one underscore. If bold/italic ran before that markup was protected, an
    unrelated single underscore later in the same paragraph (e.g. a bare
    word like `ht_documents`, not code-formatted) would pair with it and
    splice an <em> into the middle of the attribute, corrupting the tag."""
    prefixes = {"brightleaf.web": "https://gitlab.acme.com/acme/brightleaf/brightleaf.web"}
    output = ds.render_markdown(
        "brightleaf.web #1194 — Deleted in ht_documents.", gitlab_url_prefixes=prefixes)
    assert (
        '<a href="https://gitlab.acme.com/acme/brightleaf/brightleaf.web/-/issues/1194" '
        'rel="noopener" target="_blank">brightleaf.web #1194</a> — Deleted in ht_documents.'
    ) in output
    assert "<em>" not in output


def test_render_markdown_headings_get_id_slugs():
    output = ds.render_markdown("# Loop Engineering\n\n## How it works")

    assert '<h1 id="loop-engineering">Loop Engineering</h1>' in output
    assert '<h2 id="how-it-works">How it works</h2>' in output


def test_render_markdown_renders_gfm_table():
    output = ds.render_markdown("| Script | Purpose |\n|---|---|\n| `run-loop.sh` | Entry point |")

    assert "<div class='table-wrap'><table class='daemons md-table'>" in output
    assert "<thead><tr><th>Script</th><th>Purpose</th></tr></thead>" in output
    assert "<tbody><tr><td><code>run-loop.sh</code></td><td>Entry point</td></tr></tbody>" in output


def test_render_markdown_table_cells_get_inline_formatting_and_escaping():
    output = ds.render_markdown("| A | B |\n|---|---|\n| **bold** | <script>x</script> |")

    assert "<td><strong>bold</strong></td>" in output
    assert "<script>" not in output
    assert "&lt;script&gt;" in output


def test_markdown_h2_sections_extracts_in_order():
    text = "# Title\n\nintro\n\n## First section\ntext\n\n### Not a top-level section\n\n## Second section\n"

    assert ds._markdown_h2_sections(text) == [
        ("First section", "first-section"),
        ("Second section", "second-section"),
    ]


def test_markdown_h2_sections_empty_when_no_h2_headings():
    assert ds._markdown_h2_sections("# Just a title\n\nsome text") == []


def test_markdown_section_body_returns_text_between_headings():
    text = "# Title\n\n## Summary\nLine one.\nLine two.\n\n## Next section\nOther text.\n"

    assert ds._markdown_section_body(text, "Summary") == "Line one.\nLine two."


def test_markdown_section_body_case_insensitive_and_last_section():
    text = "## summary\nHello.\n"

    assert ds._markdown_section_body(text, "Summary") == "Hello."


def test_markdown_section_body_returns_none_when_heading_absent():
    assert ds._markdown_section_body("# Title\n\nsome text", "Summary") is None


def test_extract_history_overview_prefers_summary_section():
    content = "# Daily Review — 2026-08-21\n\n## Summary\nFour issues checked, all no-ops.\n\n## Issues checked\n- one\n"

    assert ds.extract_history_overview(content) == "Four issues checked, all no-ops."


def test_extract_history_overview_falls_back_to_leading_paragraph():
    content = "# AI news briefing — 2026-08-22\n\nAnthropic raises money. OpenAI ships ads.\n\n## Some story\nDetails.\n"

    assert ds.extract_history_overview(content) == "Anthropic raises money. OpenAI ships ads."


def test_extract_history_overview_truncates_long_text():
    content = "## Summary\n" + ("word " * 100).strip()

    overview = ds.extract_history_overview(content, max_length=50)

    assert len(overview) <= 51
    assert overview.endswith("…")


def test_gitlab_history_tags_counts_bullet_items_per_section():
    content = (
        "## Issues checked\n- a\n- b\n\n"
        "## MRs opened\n- fix #1\n- fix #2\n- fix #3\n\n"
        "## Answered directly\nNone.\n\n"
        "## Escalations\n- needs a decision\n"
    )

    tags = ds.gitlab_history_tags(content)

    assert "3 MRs" in tags
    assert "1 escalation" in tags
    assert not any("answered" in t for t in tags)


def test_gitlab_history_tags_quiet_day_when_nothing_happened():
    content = "## MRs opened\nNone.\n\n## Escalations\nNone.\n\n## Answered directly\nNone.\n"

    assert ds.gitlab_history_tags(content) == ["Quiet day"]


def test_gitlab_loop_stats_aggregates_totals_and_seven_day_strip(tmp_path):
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    two_days_ago = today - timedelta(days=2)

    (history_dir / f"{today.isoformat()}.md").write_text(
        "## MRs opened\n- fixed thing\n\n## Escalations\nNone.\n\n## Answered directly\nNone.\n"
    )
    (history_dir / f"{yesterday.isoformat()}.md").write_text(
        "## MRs opened\nNone.\n\n## Escalations\n- needs a human\n\n## Answered directly\nNone.\n"
    )
    (history_dir / f"{two_days_ago.isoformat()}.md").write_text(
        "## MRs opened\nNone.\n\n## Escalations\nNone.\n\n## Answered directly\n- answered one\n"
    )

    stats = ds._gitlab_loop_stats(history_dir)

    assert stats["runs"] == 3
    assert stats["mrs_opened"] == 1
    assert stats["escalations"] == 1
    assert stats["answered"] == 1
    assert len(stats["strip"]) == 7
    assert stats["strip"][-1] == {"date": today.isoformat(), "outcome": "mr"}
    assert stats["strip"][-2] == {"date": yesterday.isoformat(), "outcome": "escalation"}
    assert stats["strip"][-3] == {"date": two_days_ago.isoformat(), "outcome": "quiet"}
    assert stats["strip"][0]["outcome"] is None  # 6 days ago - nothing logged


def test_gitlab_loop_stats_escalation_outranks_mr_same_day(tmp_path):
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    today = datetime.now(timezone.utc).date()
    (history_dir / f"{today.isoformat()}.md").write_text(
        "## MRs opened\n- fixed thing\n\n## Escalations\n- needs a human\n\n## Answered directly\nNone.\n"
    )

    stats = ds._gitlab_loop_stats(history_dir)

    assert stats["strip"][-1]["outcome"] == "escalation"


def test_gitlab_loop_stats_empty_history_dir_returns_zero_totals_and_empty_strip(tmp_path):
    stats = ds._gitlab_loop_stats(tmp_path / "does-not-exist")

    assert stats["runs"] == 0
    assert stats["mrs_opened"] == 0
    assert stats["escalations"] == 0
    assert stats["answered"] == 0
    assert len(stats["strip"]) == 7
    assert all(day["outcome"] is None for day in stats["strip"])


def test_topic_history_tags_includes_topic_name_from_filename():
    tags = ds.topic_history_tags("2026-08-22-ai-news.md", "Some real content.\n\n## A story\nDetails.\n")

    assert "ai-news" in tags
    assert "Quiet" not in tags


def test_topic_history_tags_flags_quiet_briefing():
    tags = ds.topic_history_tags("2026-08-22-ai-news.md", "Nothing notable since the last run.\n")

    assert "Quiet" in tags


def test_render_readme_page_shows_quicknav_and_rendered_content(monkeypatch, tmp_path):
    readme_path = tmp_path / "README.md"
    readme_path.write_text(
        "# Loop Engineering\n\nIntro text.\n\n"
        "## Table of contents\n- [How it works](#how-it-works)\n\n"
        "## How it works\nDetails here.\n"
    )
    monkeypatch.setattr(ds, "README_PATH", readme_path)

    output = ds.render_readme_page()

    assert "<h1>README</h1>" in output
    assert "<a href='#how-it-works' class='readme-quicknav-link'>How it works</a>" in output
    assert '<h2 id="how-it-works">How it works</h2>' in output
    assert "Details here." in output
    # the quicknav replaces the written TOC for in-app browsing - it
    # shouldn't also list a chip that just points back at itself
    assert ">Table of contents<" not in output.split("<div class=\"markdown\">")[0]


def test_readme_quicknav_is_fixed_to_the_top_right(monkeypatch, tmp_path):
    readme_path = tmp_path / "README.md"
    readme_path.write_text("# Title\n\n## Section\ntext\n")
    monkeypatch.setattr(ds, "README_PATH", readme_path)

    output = ds.render_readme_page()

    assert "<a href='#section' class='readme-quicknav-link'>" in output
    assert ".readme-quicknav {" in output
    assert "position: fixed;" in output.split(".readme-quicknav {")[1].split("}")[0]
    assert "right: " in output.split(".readme-quicknav {")[1].split("}")[0]


def test_render_readme_page_handles_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "README_PATH", tmp_path / "does-not-exist.md")

    output = ds.render_readme_page()

    assert "<h1>README</h1>" in output
    assert "No README.md found" in output


def test_render_preferences_page_shows_color_mode_and_accent_controls():
    output = ds.render_preferences_page()

    assert "<h1>Preferences</h1>" in output
    for mode in ("light", "dark", "auto"):
        assert f"data-color-mode-choice=\"{mode}\"" in output
    for accent in ("default", "indigo", "blue", "green", "red", "gray"):
        assert f"data-accent-choice=\"{accent}\"" in output
    assert "loop-dashboard-color-mode" in output
    assert "loop-dashboard-accent" in output


def test_render_preferences_page_default_is_first_accent_and_default():
    output = ds.render_preferences_page()

    default_index = output.index('data-accent-choice="default"')
    blue_index = output.index('data-accent-choice="blue"')
    assert default_index < blue_index
    assert "localStorage.getItem('loop-dashboard-accent') || 'default'" in output


def test_render_preferences_page_color_mode_section_has_a_subtitle():
    output = ds.render_preferences_page()

    color_mode_section = output.split("<h2>Color mode</h2>")[1].split("</section>")[0]
    assert "<p class=\"section-subtitle\">" in color_mode_section


def test_render_preferences_page_theme_section_title_and_subtitle():
    output = ds.render_preferences_page()

    assert "<h2>Theme</h2>" in output
    assert "Select the accent color for the application interface." in output


def test_render_preferences_page_shows_font_controls():
    output = ds.render_preferences_page()

    assert "<h2>Font</h2>" in output
    for key, label, name in ds._FONT_CHOICES:
        assert f'data-font-choice="{key}"' in output
        assert label in output
        assert f"font-family: '{name}'," in output
    assert "loop-dashboard-font" in output


def test_render_preferences_page_roboto_is_the_default_font():
    output = ds.render_preferences_page()

    assert "localStorage.getItem('loop-dashboard-font') || 'roboto'" in output


def test_style_defines_font_family_stack_per_choice():
    assert "--font-family-stack: 'Roboto'," in ds._STYLE
    for key, label, name in ds._FONT_CHOICES:
        if key == "roboto":
            continue
        assert f':root[data-font="{key}"] {{ --font-family-stack: \'{name}\',' in ds._STYLE


def test_render_preferences_page_shows_auto_refresh_interval_controls():
    output = ds.render_preferences_page()

    for seconds in ("5", "11", "30", "60", "300"):
        assert f"data-refresh-choice=\"{seconds}\"" in output
    assert "loop-dashboard-refresh-interval" in output


def test_render_preferences_page_thirty_seconds_is_the_default_refresh_interval():
    output = ds.render_preferences_page()

    assert "localStorage.getItem('loop-dashboard-refresh-interval') || '30'" in output


def test_render_preferences_page_accent_swatches_show_a_layout_preview():
    """Swatches show a mini sidebar+content layout preview, not a plain
    color dot - so you can see how the page will actually look."""
    output = ds.render_preferences_page()

    assert "pref-swatch-preview-nav" in output
    assert "pref-swatch-preview-content" in output


def test_pref_swatch_preview_is_larger_than_a_plain_color_dot():
    """The layout preview inside each Theme swatch was too small to read
    as a mini sidebar+content layout - bumped up from 84x60 (same 1.4:1
    aspect ratio) so the nav/content split is actually legible."""
    assert ".pref-swatch-preview {" in ds._STYLE
    rule = ds._STYLE.split(".pref-swatch-preview {")[1].split("}")[0]
    assert "width: 140px" in rule
    assert "height: 100px" in rule


def test_render_preferences_page_nav_marks_active():
    output = ds.render_preferences_page()

    assert "<a href='/preferences' title='Preferences' class='active'>" in output


def test_render_markdown_defaults_to_real_gitlab_issue_url_prefixes(monkeypatch):
    monkeypatch.setattr(
        ds, "gitlab_issue_url_prefixes",
        lambda: {"brightleaf.web": "https://gitlab.example.com/acme/brightleaf/brightleaf.web"},
    )
    output = ds.render_markdown("brightleaf.web #42")
    assert "<a href=\"https://gitlab.example.com/acme/brightleaf/brightleaf.web/-/issues/42\"" in output


def test_gitlab_issue_url_prefixes_combines_loop_config_and_gitlab_config(tmp_path):
    loop_config_path = tmp_path / "projects.json"
    loop_config_path.write_text(json.dumps({
        "gitlab_instance": "acme",
        "projects": {
            "brightleaf.web": {"project_id": "acme/brightleaf/brightleaf.web"},
            "no-project-id": {},
        },
    }))
    gitlab_config_path = tmp_path / "gitlab_config.json"
    gitlab_config_path.write_text(json.dumps({
        "instances": {
            "acme": {"url": "https://gitlab.acme.com", "token": "glpat-should-never-appear"},
            "other": {"url": "https://gitlab.other.example.com"},
        },
    }))

    prefixes = ds.gitlab_issue_url_prefixes(loop_config_path, gitlab_config_path)

    assert prefixes == {"brightleaf.web": "https://gitlab.acme.com/acme/brightleaf/brightleaf.web"}


def test_gitlab_issue_url_prefixes_resolves_instance_per_project(tmp_path):
    loop_config_path = tmp_path / "projects.json"
    loop_config_path.write_text(json.dumps({
        "gitlab_instance": "acme",
        "projects": {
            "brightleaf.web": {"project_id": "acme/brightleaf/brightleaf.web"},
            "other-org-project": {"project_id": "other-org/some-project", "instance": "other"},
        },
    }))
    gitlab_config_path = tmp_path / "gitlab_config.json"
    gitlab_config_path.write_text(json.dumps({
        "instances": {
            "acme": {"url": "https://gitlab.acme.com"},
            "other": {"url": "https://gitlab.other.example.com"},
        },
    }))

    prefixes = ds.gitlab_issue_url_prefixes(loop_config_path, gitlab_config_path)

    assert prefixes == {
        "brightleaf.web": "https://gitlab.acme.com/acme/brightleaf/brightleaf.web",
        "other-org-project": "https://gitlab.other.example.com/other-org/some-project",
    }


def test_gitlab_issue_url_prefixes_returns_empty_when_loop_config_missing(tmp_path):
    prefixes = ds.gitlab_issue_url_prefixes(
        tmp_path / "does-not-exist.json", tmp_path / "also-missing.json")
    assert prefixes == {}


def test_gitlab_issue_url_prefixes_returns_empty_when_gitlab_config_missing(tmp_path):
    loop_config_path = tmp_path / "projects.json"
    loop_config_path.write_text(json.dumps({
        "gitlab_instance": "acme",
        "projects": {"brightleaf.web": {"project_id": "acme/brightleaf/brightleaf.web"}},
    }))

    prefixes = ds.gitlab_issue_url_prefixes(loop_config_path, tmp_path / "missing-gitlab-config.json")

    assert prefixes == {}


def test_gitlab_issue_url_prefixes_never_leaks_the_token(tmp_path):
    loop_config_path = tmp_path / "projects.json"
    loop_config_path.write_text(json.dumps({
        "gitlab_instance": "acme",
        "projects": {"brightleaf.web": {"project_id": "acme/brightleaf/brightleaf.web"}},
    }))
    gitlab_config_path = tmp_path / "gitlab_config.json"
    gitlab_config_path.write_text(json.dumps({
        "instances": {"acme": {"url": "https://gitlab.acme.com", "token": "glpat-super-secret"}},
    }))

    prefixes = ds.gitlab_issue_url_prefixes(loop_config_path, gitlab_config_path)

    assert "glpat-super-secret" not in json.dumps(prefixes)


def test_resolve_gitlab_issue_url_matches_a_tracked_alias():
    prefixes = {"harbor": "https://gitlab.acme.com/acme/harbor/harbor"}
    result = ds._resolve_gitlab_issue_url(
        "https://gitlab.acme.com/acme/harbor/harbor/-/issues/482", prefixes)
    assert result == ("harbor", 482)


def test_resolve_gitlab_issue_url_tolerates_a_trailing_slash():
    prefixes = {"harbor": "https://gitlab.acme.com/acme/harbor/harbor"}
    result = ds._resolve_gitlab_issue_url(
        "https://gitlab.acme.com/acme/harbor/harbor/-/issues/482/", prefixes)
    assert result == ("harbor", 482)


def test_resolve_gitlab_issue_url_tolerates_surrounding_whitespace():
    prefixes = {"harbor": "https://gitlab.acme.com/acme/harbor/harbor"}
    result = ds._resolve_gitlab_issue_url(
        "  https://gitlab.acme.com/acme/harbor/harbor/-/issues/482  ", prefixes)
    assert result == ("harbor", 482)


def test_resolve_gitlab_issue_url_rejects_untracked_project():
    prefixes = {"harbor": "https://gitlab.acme.com/acme/harbor/harbor"}
    result = ds._resolve_gitlab_issue_url(
        "https://gitlab.acme.com/acme/some-other-project/-/issues/1", prefixes)
    assert result is None


def test_resolve_gitlab_issue_url_rejects_a_merge_request_link():
    prefixes = {"harbor": "https://gitlab.acme.com/acme/harbor/harbor"}
    result = ds._resolve_gitlab_issue_url(
        "https://gitlab.acme.com/acme/harbor/harbor/-/merge_requests/9", prefixes)
    assert result is None


def test_resolve_gitlab_issue_url_rejects_a_different_gitlab_instance():
    prefixes = {"harbor": "https://gitlab.acme.com/acme/harbor/harbor"}
    result = ds._resolve_gitlab_issue_url(
        "https://gitlab.other.example.com/acme/harbor/harbor/-/issues/482", prefixes)
    assert result is None


def test_resolve_gitlab_issue_url_returns_none_for_empty_prefixes():
    result = ds._resolve_gitlab_issue_url(
        "https://gitlab.acme.com/acme/harbor/harbor/-/issues/482", {})
    assert result is None


def test_render_activity_page_renders_review_markdown_not_raw_text(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "LOOP_DIR", tmp_path)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path / "does-not-exist")
    ds.write_status("idle", status_path=status_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "daily-review.md").write_text("## Summary\n**All good.**")

    output = ds.render_activity_page()

    assert '<h2 id="summary">Summary</h2>' in output
    assert "<strong>All good.</strong>" in output
    assert "pre class" not in output


def test_dashboard_server_integration_history_route_renders_markdown(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "HISTORY_DIR", tmp_path)
    (tmp_path / "2026-08-01.md").write_text("# Review\n**bold content**")

    with _running_server() as port:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/history/2026-08-01.md", timeout=10) as response:
            body = response.read().decode("utf-8")
            assert '<h1 id="review">Review</h1>' in body
            assert "<strong>bold content</strong>" in body


def test_render_history_page_lists_history_files(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    monkeypatch.setattr(ds, "HISTORY_DIR", history_dir)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path / "does-not-exist")
    (history_dir / "2026-08-01.md").write_text("x")

    output = ds.render_history_page()

    assert "<h1>Run History</h1>" in output
    assert "<a href='/history/2026-08-01.md'>2026-08-01.md</a>" in output


def test_render_history_page_does_not_auto_refresh(tmp_path, monkeypatch):
    """Only Live GitLab, Topic Monitor, Activity, and Logs auto-refresh - a
    run history listing is a record of past runs, not live state."""
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    monkeypatch.setattr(ds, "HISTORY_DIR", history_dir)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path / "does-not-exist")

    output = ds.render_history_page()

    assert "auto-refreshes every 30s" not in output
    assert "location.reload();" not in output


def test_render_history_page_also_lists_topic_monitor_history(tmp_path, monkeypatch):
    """Both loops' run history live on this one page now - the GitLab loop's
    own archived reviews, and every configured topic's saved briefings,
    each linking to its own detail route."""
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(ds, "HISTORY_DIR", tmp_path / "does-not-exist")
    topic_history_dir = tmp_path / "topic-history"
    topic_history_dir.mkdir()
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", topic_history_dir)
    (topic_history_dir / "2026-08-22-ai-news.md").write_text("x")

    output = ds.render_history_page()

    assert "Topic Monitor" in output
    assert "<a href='/topic-monitor/history/2026-08-22-ai-news.md'>2026-08-22-ai-news.md</a>" in output


def test_render_history_page_shows_overview_and_tags_for_gitlab_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    monkeypatch.setattr(ds, "HISTORY_DIR", history_dir)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path / "does-not-exist")
    (history_dir / "2026-08-21.md").write_text(
        "# Daily Review — 2026-08-21\n\n## Summary\nFour issues checked, all no-ops.\n\n"
        "## MRs opened\n- fix #1\n"
    )

    output = ds.render_history_page()

    assert "Four issues checked, all no-ops." in output
    assert "1 MR" in output


def test_render_history_page_includes_delete_forms(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    monkeypatch.setattr(ds, "HISTORY_DIR", history_dir)
    topic_history_dir = tmp_path / "topic-history"
    topic_history_dir.mkdir()
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", topic_history_dir)
    (history_dir / "2026-08-21.md").write_text("## Summary\nx\n")
    (topic_history_dir / "2026-08-22-ai-news.md").write_text("x\n")

    output = ds.render_history_page()

    assert "action='/history/2026-08-21.md/delete'" in output
    assert "action='/topic-monitor/history/2026-08-22-ai-news.md/delete'" in output


def test_append_unified_log_writes_timestamped_header_and_body(tmp_path):
    log_path = tmp_path / "logs" / "loop-engineering.log"

    ds.append_unified_log("gitlab-loop", "run finished (exit 0)", body="All good.", log_path=log_path)

    content = log_path.read_text()
    assert re.search(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ---- gitlab-loop ---- run finished \(exit 0\) ----$",
                      content, re.MULTILINE)
    assert "All good." in content


def test_append_unified_log_without_body_writes_header_only(tmp_path):
    log_path = tmp_path / "logs" / "loop-engineering.log"

    ds.append_unified_log("chat-assistant", "turn started", log_path=log_path)

    content = log_path.read_text()
    assert "---- chat-assistant ---- turn started ----" in content
    assert content.count("\n") == 1


def test_append_unified_log_appends_across_multiple_calls(tmp_path):
    log_path = tmp_path / "logs" / "loop-engineering.log"

    ds.append_unified_log("chat-assistant", "turn started", log_path=log_path)
    ds.append_unified_log("chat-assistant", "reply", body="hello there", log_path=log_path)

    content = log_path.read_text()
    assert content.index("turn started") < content.index("hello there")


def test_append_unified_log_creates_missing_parent_directory(tmp_path):
    log_path = tmp_path / "does-not-exist-yet" / "loop-engineering.log"

    ds.append_unified_log("topic-monitor", "run started", log_path=log_path)

    assert log_path.exists()


def test_append_unified_log_is_silent_when_the_path_cannot_be_written(tmp_path):
    """A logging failure (disk full, logs/ unwritable) must never break the
    caller - in particular _run_chat_job must still finish and reply even
    if this fails. Simulated here by pointing at a path whose parent is a
    plain file, not a directory, so mkdir(parents=True) itself raises."""
    blocked = tmp_path / "blocked-file"
    blocked.write_text("x")
    log_path = blocked / "loop-engineering.log"

    ds.append_unified_log("chat-assistant", "turn started", log_path=log_path)  # must not raise


def test_read_unified_log_tail_returns_none_when_missing(tmp_path):
    assert ds.read_unified_log_tail(log_path=tmp_path / "does-not-exist.log") is None


def test_read_unified_log_tail_returns_last_n_lines(tmp_path):
    log_path = tmp_path / "loop-engineering.log"
    log_path.write_text("\n".join(f"line {i}" for i in range(10)) + "\n")

    tail = ds.read_unified_log_tail(lines=3, log_path=log_path)

    assert tail == "line 7\nline 8\nline 9"


def test_render_logs_page_shows_tail_content(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    log_path = tmp_path / "loop-engineering.log"
    log_path.write_text("[2026-08-24 10:00:00] ---- gitlab-loop ---- run started ----\nAll good.\n")
    monkeypatch.setattr(ds, "UNIFIED_LOG_PATH", log_path)

    output = ds.render_logs_page()

    assert "<h1>Logs</h1>" in output
    assert "gitlab-loop" in output
    assert "All good." in output


def test_render_logs_page_subtitle_names_the_selected_ai_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(ds, "UNIFIED_LOG_PATH", tmp_path / "does-not-exist.log")
    config_path = tmp_path / "ai_cli.json"
    config_path.write_text('{"cli": "codex"}')
    monkeypatch.setattr(ai_cli_config, "DEFAULT_CONFIG_PATH", config_path)

    output = ds.render_logs_page()

    assert "every Codex CLI invocation" in output


def test_render_logs_page_shows_placeholder_when_no_entries_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(ds, "UNIFIED_LOG_PATH", tmp_path / "does-not-exist.log")

    output = ds.render_logs_page()

    assert "No log entries yet." in output


def test_render_logs_page_auto_refreshes(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(ds, "UNIFIED_LOG_PATH", tmp_path / "does-not-exist.log")

    output = ds.render_logs_page()

    assert "auto-refreshes every 30s" in output
    assert "location.reload();" in output


def test_parse_unified_log_entries_splits_on_header_lines():
    text = (
        "[2026-08-24 10:00:00] ---- gitlab-loop ---- run started ----\n"
        "First body.\n"
        "[2026-08-24 10:05:00] ---- chat-assistant ---- reply ----\n"
        "Second body.\n"
        "More second body."
    )

    entries = ds._parse_unified_log_entries(text)

    assert len(entries) == 2
    assert entries[0] == {
        "timestamp": "2026-08-24 10:00:00", "source": "gitlab-loop",
        "detail": "run started", "body": "First body.",
    }
    assert entries[1] == {
        "timestamp": "2026-08-24 10:05:00", "source": "chat-assistant",
        "detail": "reply", "body": "Second body.\nMore second body.",
    }


def test_parse_unified_log_entries_keeps_text_before_first_header():
    text = "orphaned continuation line\n[2026-08-24 10:00:00] ---- gitlab-loop ---- run started ----\nBody."

    entries = ds._parse_unified_log_entries(text)

    assert entries[0] == {
        "timestamp": None, "source": None, "detail": None, "body": "orphaned continuation line",
    }
    assert entries[1]["source"] == "gitlab-loop"


def test_render_logs_page_shows_newest_entry_first(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    log_path = tmp_path / "loop-engineering.log"
    log_path.write_text(
        "[2026-08-24 10:00:00] ---- gitlab-loop ---- run started ----\n"
        "Older entry.\n"
        "[2026-08-24 10:05:00] ---- chat-assistant ---- reply ----\n"
        "Newer entry.\n"
    )
    monkeypatch.setattr(ds, "UNIFIED_LOG_PATH", log_path)

    output = ds.render_logs_page()

    assert output.index("Newer entry.") < output.index("Older entry.")


def test_render_logs_page_renders_each_entry_as_its_own_block(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    log_path = tmp_path / "loop-engineering.log"
    log_path.write_text(
        "[2026-08-24 10:00:00] ---- gitlab-loop ---- run started ----\n"
        "Older entry.\n"
        "[2026-08-24 10:05:00] ---- chat-assistant ---- reply ----\n"
        "Newer entry.\n"
    )
    monkeypatch.setattr(ds, "UNIFIED_LOG_PATH", log_path)

    output = ds.render_logs_page()

    assert output.count("class='log-entry'") == 2
    assert "gitlab-loop" in output and "chat-assistant" in output


def test_logs_nav_item_present_in_monitor_group_after_activity(tmp_path):
    sidebar = ds._sidebar_html("overview")

    assert ds._NAV_GROUP_OF["logs"] == "Monitor"
    assert sidebar.index("title='Activity'") < sidebar.index("title='Logs'") < sidebar.index("title='Run History'")


def test_dashboard_server_integration_logs_route():
    with _running_server() as port:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/logs", timeout=10) as response:
            assert response.status == 200
            assert "<h1>Logs</h1>" in response.read().decode("utf-8")


def test_run_chat_job_logs_turn_started_and_reply_to_unified_log(tmp_path, monkeypatch):
    log_path = tmp_path / "logs" / "loop-engineering.log"
    monkeypatch.setattr(ds, "UNIFIED_LOG_PATH", log_path)
    messages_path = tmp_path / "messages.json"
    messages_path.write_text("[]")
    lines = ['{"is_error":false,"result":"hello there","type":"result"}\n']
    monkeypatch.setattr(ds.subprocess, "Popen", lambda *a, **k: _FakeChatPopenProcess(lines))

    key = ds._chat_job_create()
    ds._run_chat_job(key, "hi", messages_path=messages_path)

    content = log_path.read_text()
    assert "chat-assistant ---- turn started (Claude Code)" in content
    assert "chat-assistant ---- reply (Claude Code)" in content
    assert "hello there" in content


def test_run_chat_job_logs_error_to_unified_log_on_failed_result(tmp_path, monkeypatch):
    log_path = tmp_path / "logs" / "loop-engineering.log"
    monkeypatch.setattr(ds, "UNIFIED_LOG_PATH", log_path)
    messages_path = tmp_path / "messages.json"
    messages_path.write_text("[]")
    lines = ['{"is_error":true,"result":"Not logged in","type":"result"}\n']
    monkeypatch.setattr(ds.subprocess, "Popen", lambda *a, **k: _FakeChatPopenProcess(lines))

    key = ds._chat_job_create()
    ds._run_chat_job(key, "hi", messages_path=messages_path)

    content = log_path.read_text()
    assert "chat-assistant ---- error (Claude Code)" in content
    assert "Not logged in" in content


def test_delete_history_file_removes_file(tmp_path):
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    (history_dir / "2026-08-21.md").write_text("x")

    ok, message = ds.delete_history_file("2026-08-21.md", history_dir)

    assert ok, message
    assert not (history_dir / "2026-08-21.md").exists()


def test_delete_history_file_missing_returns_false(tmp_path):
    history_dir = tmp_path / "history"
    history_dir.mkdir()

    ok, message = ds.delete_history_file("does-not-exist.md", history_dir)

    assert not ok
    assert "not found" in message


def test_delete_history_file_rejects_non_md_and_path_traversal(tmp_path):
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")

    ok, message = ds.delete_history_file("../outside.txt", history_dir)

    assert not ok
    assert outside.exists()
    assert outside.read_text() == "keep me"


def test_history_delete_route_success(monkeypatch, tmp_path):
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    (history_dir / "2026-08-21.md").write_text("x")
    monkeypatch.setattr(ds, "HISTORY_DIR", history_dir)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/daemons")
        status, headers, _body = _post(port, "/history/2026-08-21.md/delete", {"csrf_token": token})
        assert status == 303
        parsed = _flash_from_location(headers.get("Location"), prefix="/history?")
        assert parsed["ok"] == ["1"]
    assert not (history_dir / "2026-08-21.md").exists()


def test_history_delete_route_requires_csrf(monkeypatch, tmp_path):
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    (history_dir / "2026-08-21.md").write_text("x")
    monkeypatch.setattr(ds, "HISTORY_DIR", history_dir)

    with _running_server() as port:
        status, _headers, _body = _post(port, "/history/2026-08-21.md/delete", {"csrf_token": ""})
        assert status == 403
    assert (history_dir / "2026-08-21.md").exists()


def test_topic_monitor_history_delete_route_success(monkeypatch, tmp_path):
    topic_history_dir = tmp_path / "topic-history"
    topic_history_dir.mkdir()
    (topic_history_dir / "2026-08-22-ai-news.md").write_text("x")
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", topic_history_dir)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/daemons")
        status, headers, _body = _post(port, "/topic-monitor/history/2026-08-22-ai-news.md/delete", {"csrf_token": token})
        assert status == 303
        parsed = _flash_from_location(headers.get("Location"), prefix="/history?")
        assert parsed["ok"] == ["1"]
    assert not (topic_history_dir / "2026-08-22-ai-news.md").exists()


def test_topic_monitor_history_delete_route_requires_csrf(monkeypatch, tmp_path):
    topic_history_dir = tmp_path / "topic-history"
    topic_history_dir.mkdir()
    (topic_history_dir / "2026-08-22-ai-news.md").write_text("x")
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", topic_history_dir)

    with _running_server() as port:
        status, _headers, _body = _post(port, "/topic-monitor/history/2026-08-22-ai-news.md/delete", {"csrf_token": ""})
        assert status == 403
    assert (topic_history_dir / "2026-08-22-ai-news.md").exists()


def test_md_spinner_animation_ignores_reduced_motion_preference():
    """Unlike the purely decorative progress-bar animation, the spinner's
    motion is the only signal a loading page is still working - it must
    keep spinning even under prefers-reduced-motion, or it just looks
    frozen/broken instead of calmer."""
    reduced_motion_block = ds._STYLE.split("@media (prefers-reduced-motion: no-preference) {")[1]

    assert "md-spinner" not in reduced_motion_block
    assert ".md-spinner::before { animation: md-spin-cw" in ds._STYLE
    assert ".md-spinner::after { animation: md-spin-ccw" in ds._STYLE


def test_md_spinner_pill_sizes_via_em_to_fit_either_pill_size():
    """One inline spinner variant, sized in em rather than a fixed px like
    .md-spinner-sm, so it automatically matches whichever pill it's
    placed in - the small topbar/per-topic .pill (0.75rem text) and the
    larger .pill-lg hero badge (0.95rem text) - without needing a second,
    separate size variant for each."""
    assert ".md-spinner.md-spinner-pill {" in ds._STYLE
    rule = ds._STYLE.split(".md-spinner.md-spinner-pill {")[1].split("}")[0]
    assert "em" in rule
    assert "px" not in rule


def test_render_gitlab_page_does_not_fetch_live_state(monkeypatch, tmp_path):
    """The page itself must render instantly - fetching live GitLab state
    (a subprocess + network round trip per configured project) happens only
    when the browser requests /gitlab/live, never while rendering the page
    shell. See render_gitlab_live_fragment for the part that actually calls
    get_live_gitlab_state."""
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")

    def must_not_be_called(*a, **k):
        raise AssertionError("render_gitlab_page must not fetch live GitLab state itself")

    monkeypatch.setattr(ds, "get_live_gitlab_state", must_not_be_called)

    output = ds.render_gitlab_page()

    assert "<h1>Live GitLab</h1>" in output
    assert "data-lazy-load='/gitlab/live'" in output
    assert "md-spinner" in output


def test_render_gitlab_page_auto_refreshes(monkeypatch, tmp_path):
    """Live GitLab is one of the three pages (with Topic Monitor and
    Activity) whose data changes out from under a reader while they watch
    it, so it opts into _render_shell's auto-refresh explicitly."""
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")

    output = ds.render_gitlab_page()

    assert "auto-refreshes every 30s" in output
    assert "location.reload();" in output


def test_render_gitlab_page_loading_text_has_animated_dots():
    """A static "…" character read as flat/dead. Three separately-animated
    dot spans, staggered, give the classic "still working" pulsing-dots
    look instead - purely decorative (unlike the spinner's motion, which
    stays unconditional), so it can respect prefers-reduced-motion."""
    output = ds.render_gitlab_page()

    loading_text = output.split("class=\"loading-text\">")[1].split("</p>")[0]
    assert "class=\"loading-dots\">" in loading_text
    dots_html = loading_text.split("class=\"loading-dots\">")[1]
    assert dots_html.count("<span>") == 3
    assert "loading-dots" in ds._STYLE
    assert "@keyframes loading-dots-fade" in ds._STYLE


def test_render_gitlab_live_fragment_shows_configured_project_data(monkeypatch):
    monkeypatch.setattr(ds, "get_live_gitlab_state", lambda *a, **k: {
        "myproj": {"issues": [{"iid": 1, "title": "Fix bug", "web_url": "http://x/1"}], "mrs": []},
    })

    output = ds.render_gitlab_live_fragment()

    assert "myproj" in output
    assert "Fix bug" in output


def test_render_gitlab_live_fragment_shows_empty_state_with_settings_link_when_no_projects(monkeypatch):
    monkeypatch.setattr(ds, "get_live_gitlab_state", lambda *a, **k: {})

    output = ds.render_gitlab_live_fragment()

    assert "class='empty-state'" in output
    assert "<span class='material-symbols-outlined' aria-hidden='true'>folder_off</span>" in output
    assert "<a class='btn btn-primary empty-state-action' href='/settings'>" in output
    assert "<p>(no projects configured)</p>" not in output


def test_render_gitlab_live_fragment_shows_error_notice_instead_of_fake_issue(monkeypatch):
    monkeypatch.setattr(ds, "get_live_gitlab_state", lambda *a, **k: {
        "myproj": {
            "issues": [], "issues_error": "Error: Instance 'x' not found",
            "mrs": [], "mrs_error": "Error: Instance 'x' not found",
        },
    })

    output = ds.render_gitlab_live_fragment()

    assert "Couldn't check: Error: Instance &#x27;x&#x27; not found" in output
    assert "class='inline-error'" in output
    assert "(error:" not in output
    assert "Issues <span class='badge-count'>0</span>" in output


def test_render_gitlab_live_fragment_shows_assignee_updated_time_and_labels(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(ds, "get_live_gitlab_state", lambda *a, **k: {
        "myproj": {
            "issues": [{
                "iid": 1, "title": "Fix bug", "web_url": "http://x/1",
                "assignees": [{"name": "Berin Zhou", "username": "berin"}],
                "updated_at": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "labels": ["Status: In Progress"],
            }],
            "mrs": [],
        },
    })

    output = ds.render_gitlab_live_fragment()

    assert "class='gitlab-item'" in output
    assert "Berin Zhou &middot; 2h ago" in output
    assert "class='gitlab-item-row'" in output
    assert "<span class='pill pill-grey'>Status: In Progress</span>" in output


def test_render_shell_wires_up_lazy_load_fetch():
    output = ds.render_overview_page()

    assert "data-lazy-load" in output
    assert "fetch(" in output


def test_render_shell_wires_up_tab_switching():
    page = ds._render_shell("Test", "overview", "<span>badge</span>", "<p>body</p>")
    head, _, _ = page.partition("<body>")

    assert "data-tab-target" in head
    assert "data-tab-panel" in head
    assert "is-active" in head


def test_relative_time_buckets():
    now = datetime.now(timezone.utc)
    assert ds._relative_time((now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")) == "just now"
    assert ds._relative_time((now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")) == "5m ago"
    assert ds._relative_time((now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")) == "3h ago"
    assert ds._relative_time((now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")) == "2d ago"


def test_relative_time_handles_missing_or_invalid_input():
    assert ds._relative_time("") == ""
    assert ds._relative_time("not-a-timestamp") == "not-a-timestamp"


def test_get_project_memory_merges_legacy_and_task_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "get_project_learnings", lambda *a, **k: {
        "myproj": [{"lesson": "An old lesson."}],
    })
    monkeypatch.setattr(
        ds.memory_store, "list_task_memories",
        lambda alias, root=None: [{"body": "A new lesson.", "issue_iid": 1, "tags": []}],
    )

    memory = ds.get_project_memory()

    assert memory["myproj"]["legacy"] == [{"lesson": "An old lesson."}]
    assert memory["myproj"]["tasks"] == [{"body": "A new lesson.", "issue_iid": 1, "tags": []}]


def test_render_memory_page_shows_task_memory(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(ds.learning.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")
    monkeypatch.setattr(ds, "get_project_memory", lambda *a, **k: {
        "myproj": {"legacy": [], "tasks": [{"body": "Always run tests.", "issue_iid": 1, "tags": []}]},
    })
    monkeypatch.setattr(ds, "gitlab_issue_url_prefixes", lambda *a, **k: {})

    output = ds.render_memory_page()

    assert "<h1>Project Memory</h1>" in output
    assert "Always run tests." in output


def test_render_memory_page_shows_empty_state_with_settings_link_when_no_projects(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(ds.learning.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")
    monkeypatch.setattr(ds, "get_project_memory", lambda *a, **k: {})

    output = ds.render_memory_page()

    assert "class='empty-state'" in output
    assert "<span class='material-symbols-outlined' aria-hidden='true'>folder_off</span>" in output
    assert "<a class='btn btn-primary empty-state-action' href='/settings'>" in output


def test_render_memory_page_renders_markdown_and_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(ds.learning.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")
    monkeypatch.setattr(ds, "get_project_memory", lambda *a, **k: {
        "myproj": {"legacy": [], "tasks": [{
            "body": "Run `bundle exec rspec` before pushing.",
            "issue_iid": 42,
            "tags": ["flaky-test"],
        }]},
    })
    monkeypatch.setattr(ds, "gitlab_issue_url_prefixes", lambda *a, **k: {})

    output = ds.render_memory_page()

    assert "<code>bundle exec rspec</code>" in output
    assert "class='learning-item'" in output
    assert "<span class='pill pill-grey'>#42</span>" in output
    assert "<span class='pill pill-grey'>flaky-test</span>" in output


def test_render_memory_page_shows_task_description_as_subtitle(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(ds.learning.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")
    monkeypatch.setattr(ds, "get_project_memory", lambda *a, **k: {
        "myproj": {"legacy": [], "tasks": [{
            "body": "Always run tests.",
            "description": "Flaky RSpec run under parallel load",
            "issue_iid": 42,
            "tags": [],
        }]},
    })
    monkeypatch.setattr(ds, "gitlab_issue_url_prefixes", lambda *a, **k: {})

    output = ds.render_memory_page()

    assert "Flaky RSpec run under parallel load" in output


def test_render_memory_page_links_issue_number_to_the_real_gitlab_issue(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(ds.learning.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")
    monkeypatch.setattr(ds, "get_project_memory", lambda *a, **k: {
        "myproj": {"legacy": [], "tasks": [{
            "body": "Always run tests.",
            "issue_iid": 42,
            "tags": ["flaky-test"],
        }]},
    })
    monkeypatch.setattr(
        ds, "gitlab_issue_url_prefixes",
        lambda *a, **k: {"myproj": "https://gitlab.example.com/mygroup/myproj"},
    )

    output = ds.render_memory_page()

    assert (
        "<a class='pill pill-link' href='https://gitlab.example.com/mygroup/myproj/-/issues/42' "
        "rel='noopener' target='_blank'>" in output
    )


def test_render_memory_page_shows_legacy_learnings_in_their_own_section(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(ds.learning.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")
    monkeypatch.setattr(ds, "get_project_memory", lambda *a, **k: {
        "myproj": {
            "legacy": [{"lesson": "An old lesson from before the file-based format."}],
            "tasks": [],
        },
    })
    monkeypatch.setattr(ds, "gitlab_issue_url_prefixes", lambda *a, **k: {})

    output = ds.render_memory_page()

    assert "<h4>Legacy learnings</h4>" in output
    assert "An old lesson from before the file-based format." in output


def test_render_topic_monitor_page_shows_no_topics_message_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [])

    output = ds.render_topic_monitor_page()

    assert "No topics configured" in output


def test_render_topic_monitor_page_shows_empty_state_with_topic_settings_link_when_no_topics(monkeypatch):
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [])

    output = ds.render_topic_monitor_page()

    assert "class='empty-state'" in output
    assert "<span class='material-symbols-outlined' aria-hidden='true'>folder_off</span>" in output
    assert "<a class='btn btn-primary empty-state-action' href='/topic-monitor/settings'>" in output


def test_render_topic_monitor_page_auto_refreshes(monkeypatch, tmp_path):
    """Topic Monitor is one of the three pages (with Live GitLab and
    Activity) whose data changes out from under a reader while they watch
    it, so it opts into _render_shell's auto-refresh explicitly."""
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [])

    output = ds.render_topic_monitor_page()

    assert "auto-refreshes every 30s" in output
    assert "location.reload();" in output


def test_render_topic_monitor_page_lists_topic_status(monkeypatch):
    """Status only - saved briefings moved to the Run History page."""
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [
        {"name": "ai-news", "label": "AI news", "brief": "x", "slack_bundle": None},
    ])
    monkeypatch.setattr(ds, "read_topic_status", lambda *a, **k: {
        "topics": {"ai-news": {"state": "idle", "updated_at": "2026-08-22T09:00:00+00:00"}}
    })

    output = ds.render_topic_monitor_page()

    assert "AI news" in output
    assert "/topic-monitor/history/" not in output


def test_render_topic_monitor_page_shows_last_run_time(monkeypatch):
    """The spec asks for "idle/running/last-run-time per topic": without the
    timestamp a topic that ran an hour ago and one that ran a week ago look
    identical."""
    updated_at = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [
        {"name": "ai-news", "label": "AI news", "brief": "x", "slack_bundle": None},
    ])
    monkeypatch.setattr(ds, "read_topic_status", lambda *a, **k: {
        "topics": {"ai-news": {"state": "idle", "updated_at": updated_at}}
    })

    output = ds.render_topic_monitor_page()

    assert "3h ago" in output


def test_render_topic_monitor_page_shows_current_step_while_running(monkeypatch):
    """The full status entry reaches _status_badge_markup, so the badge shows
    what the loop is doing (current_step) rather than a bare "Running"."""
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [
        {"name": "ai-news", "label": "AI news", "brief": "x", "slack_bundle": None},
    ])
    monkeypatch.setattr(ds, "read_topic_status", lambda *a, **k: {
        "topics": {"ai-news": {
            "state": "running",
            "current_step": "researching",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }}
    })

    output = ds.render_topic_monitor_page()

    assert "Researching" in output


def test_render_topic_monitor_page_shows_run_now_button_when_idle(monkeypatch):
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [
        {"name": "ai-news", "label": "AI news", "brief": "x", "slack_bundle": None},
    ])
    monkeypatch.setattr(ds, "read_topic_status", lambda *a, **k: {
        "topics": {"ai-news": {"state": "idle", "updated_at": datetime.now(timezone.utc).isoformat()}}
    })

    output = ds.render_topic_monitor_page()

    assert "action='/topic-monitor/run-now'" in output
    assert "Run now" in output
    assert "class='btn btn-primary'" in output


def test_render_topic_monitor_page_hides_run_now_button_when_a_topic_is_running(monkeypatch):
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [
        {"name": "ai-news", "label": "AI news", "brief": "x", "slack_bundle": None},
    ])
    monkeypatch.setattr(ds, "read_topic_status", lambda *a, **k: {
        "topics": {"ai-news": {"state": "running", "current_step": "researching"}}
    })

    output = ds.render_topic_monitor_page()

    assert "action='/topic-monitor/run-now'" not in output


def test_render_topic_monitor_page_hides_run_now_button_when_no_topics_configured(monkeypatch):
    """Previously this button stayed enabled with zero topics configured
    (any_topic_running is vacuously False over an empty dict) - clicking it
    would have had nothing to actually research."""
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [])
    monkeypatch.setattr(ds, "read_topic_status", lambda *a, **k: {"topics": {}})

    output = ds.render_topic_monitor_page()

    assert "action='/topic-monitor/run-now'" not in output


def test_render_topic_monitor_page_does_not_include_settings_section(monkeypatch):
    """Topic settings (edit/add/delete) moved to their own page
    (render_topic_settings_page, /topic-monitor/settings) - this page only
    shows each topic's live status now."""
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [
        {"name": "ai-news", "label": "AI news", "brief": "Major AI news.", "slack_bundle": None},
    ])
    monkeypatch.setattr(ds, "read_topic_status", lambda *a, **k: {
        "topics": {"ai-news": {"state": "idle", "updated_at": "2026-08-22T09:00:00+00:00"}}
    })

    output = ds.render_topic_monitor_page()

    assert "<h2>Topics</h2>" in output
    assert "<h2>Topic Settings</h2>" not in output
    assert "action='/topic-monitor/topics'" not in output


def test_render_topic_monitor_page_shows_latest_data_overview_and_tags(monkeypatch, tmp_path):
    """The Latest Data section, added after Topics, surfaces each topic's
    most recent saved briefing at a glance - same overview/tags building
    blocks the Run History page already uses for each entry."""
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [
        {"name": "ai-news", "label": "AI news", "brief": "x", "slack_bundle": None},
    ])
    monkeypatch.setattr(ds, "read_topic_status", lambda *a, **k: {"topics": {}})
    topic_history_dir = tmp_path / "topic-history"
    topic_history_dir.mkdir()
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", topic_history_dir)
    (topic_history_dir / "2026-08-22-ai-news.md").write_text(
        "# AI news - 2026-08-22\n\nA new model shipped today with major gains.\n"
    )

    output = ds.render_topic_monitor_page()

    assert "<h2>Latest Data</h2>" in output
    assert "A new model shipped today with major gains." in output
    assert "ai-news" in output


def test_render_topic_monitor_page_latest_data_shows_no_data_yet_without_history(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [
        {"name": "ai-news", "label": "AI news", "brief": "x", "slack_bundle": None},
    ])
    monkeypatch.setattr(ds, "read_topic_status", lambda *a, **k: {"topics": {}})
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", tmp_path / "does-not-exist")

    output = ds.render_topic_monitor_page()

    assert "<h2>Latest Data</h2>" in output
    assert "no data yet" in output


def test_render_topic_monitor_page_latest_data_is_expandable_and_collapsed_by_default(monkeypatch, tmp_path):
    """Clicking an item reveals the full briefing inline (no navigation to
    the Run History detail page - this page never links out there, see
    test_render_topic_monitor_page_lists_topic_status) via the same
    onclick-toggles-is-expanded convention the Skills page uses."""
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [
        {"name": "ai-news", "label": "AI news", "brief": "x", "slack_bundle": None},
    ])
    monkeypatch.setattr(ds, "read_topic_status", lambda *a, **k: {"topics": {}})
    topic_history_dir = tmp_path / "topic-history"
    topic_history_dir.mkdir()
    monkeypatch.setattr(ds, "TOPIC_MONITOR_HISTORY_DIR", topic_history_dir)
    (topic_history_dir / "2026-08-22-ai-news.md").write_text(
        "# AI news\n\nOverview paragraph.\n\n## Details\nThe full body content goes here.\n"
    )

    output = ds.render_topic_monitor_page()

    assert "topic-latest-summary" in output
    assert "aria-expanded='false'" in output
    assert "aria-expanded='true'" not in output
    assert "The full body content goes here." in output
    assert "/topic-monitor/history/" not in output


def test_render_topic_monitor_page_omits_latest_data_section_when_no_topics_configured(monkeypatch):
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [])

    output = ds.render_topic_monitor_page()

    assert "<h2>Latest Data</h2>" not in output


def test_render_topic_settings_page_includes_edit_and_delete_forms_for_each_topic(monkeypatch):
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [
        {"name": "ai-news", "label": "AI news", "brief": "Major AI news.", "slack_bundle": None},
    ])
    monkeypatch.setattr(ds, "read_topic_status", lambda *a, **k: {"topics": {}})
    monkeypatch.setattr(ds, "read_gitlab_config", lambda *a, **k: {"bundles": {}})

    output = ds.render_topic_settings_page()

    assert "<h2>Topic Settings</h2>" in output
    assert "action='/topic-monitor/topics'" in output
    assert "value='ai-news'" in output
    assert "value='AI news'" in output
    assert "Major AI news." in output
    assert "action='/topic-monitor/topics/ai-news/delete'" in output


def test_render_topic_settings_page_includes_add_topic_form(monkeypatch):
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [])
    monkeypatch.setattr(ds, "read_topic_status", lambda *a, **k: {"topics": {}})
    monkeypatch.setattr(ds, "read_gitlab_config", lambda *a, **k: {"bundles": {}})

    output = ds.render_topic_settings_page()

    assert output.count("action='/topic-monitor/topics'") == 1
    assert "placeholder='topic name'" in output
    assert "Add topic" in output


def test_topic_monitor_topics_route_add_success(monkeypatch, tmp_path):
    topics_path = tmp_path / "topics.json"
    monkeypatch.setattr(topic_config, "DEFAULT_CONFIG_PATH", topics_path)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/daemons")
        status, _headers, _body = _post(port, "/topic-monitor/topics", {
            "name": "ai-news", "label": "AI news", "brief": "Major AI news.", "slack_bundle": "", "csrf_token": token,
        })
        assert status == 303
    assert topic_config.get_topic("ai-news", topics_path)["label"] == "AI news"


def test_topic_monitor_topics_route_edit_success(monkeypatch, tmp_path):
    topics_path = tmp_path / "topics.json"
    topic_config.upsert_topic("ai-news", "AI news", "Old brief.", "", topics_path)
    monkeypatch.setattr(topic_config, "DEFAULT_CONFIG_PATH", topics_path)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/daemons")
        status, _headers, _body = _post(port, "/topic-monitor/topics", {
            "name": "ai-news", "label": "AI news", "brief": "New brief.", "slack_bundle": "", "csrf_token": token,
        })
        assert status == 303
    assert topic_config.get_topic("ai-news", topics_path)["brief"] == "New brief."


def test_topic_monitor_topics_route_requires_csrf(monkeypatch, tmp_path):
    topics_path = tmp_path / "topics.json"
    monkeypatch.setattr(topic_config, "DEFAULT_CONFIG_PATH", topics_path)

    with _running_server() as port:
        status, _headers, _body = _post(port, "/topic-monitor/topics", {
            "name": "ai-news", "label": "AI news", "brief": "x", "csrf_token": "",
        })
        assert status == 403
    assert not topics_path.exists()


def test_topic_monitor_topics_delete_route_success(monkeypatch, tmp_path):
    topics_path = tmp_path / "topics.json"
    topic_config.upsert_topic("ai-news", "AI news", "Brief.", "", topics_path)
    monkeypatch.setattr(topic_config, "DEFAULT_CONFIG_PATH", topics_path)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/daemons")
        status, _headers, _body = _post(port, "/topic-monitor/topics/ai-news/delete", {"csrf_token": token})
        assert status == 303
    assert topic_config.list_names(topics_path) == []


def test_topic_monitor_topics_delete_route_requires_csrf(monkeypatch, tmp_path):
    topics_path = tmp_path / "topics.json"
    topic_config.upsert_topic("ai-news", "AI news", "Brief.", "", topics_path)
    monkeypatch.setattr(topic_config, "DEFAULT_CONFIG_PATH", topics_path)

    with _running_server() as port:
        status, _headers, _body = _post(port, "/topic-monitor/topics/ai-news/delete", {"csrf_token": ""})
        assert status == 403
    assert topic_config.list_names(topics_path) == ["ai-news"]


def test_trigger_topic_monitor_run_refuses_when_a_topic_is_running(tmp_path):
    status_path = tmp_path / "status.json"
    ds.write_topic_status("ai-news", "running", status_path=status_path)

    ok, message = ds.trigger_topic_monitor_run(status_path=status_path, run_loop_path=tmp_path / "run-topic-monitor-loop.sh")

    assert not ok
    assert "already in progress" in message


def test_trigger_topic_monitor_run_refuses_when_script_missing(tmp_path):
    status_path = tmp_path / "status.json"
    ds.write_topic_status("ai-news", "idle", status_path=status_path)

    ok, message = ds.trigger_topic_monitor_run(status_path=status_path, run_loop_path=tmp_path / "does-not-exist.sh")

    assert not ok
    assert "not found" in message


def test_trigger_topic_monitor_run_launches_the_script(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    ds.write_topic_status("ai-news", "idle", status_path=status_path)
    run_loop_path = tmp_path / "run-topic-monitor-loop.sh"
    run_loop_path.write_text("#!/bin/bash\ntrue\n")
    run_loop_path.chmod(0o755)

    captured = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    ok, message = ds.trigger_topic_monitor_run(status_path=status_path, run_loop_path=run_loop_path)

    assert ok, message
    assert captured["args"] == ["bash", str(run_loop_path)]
    assert captured["kwargs"]["start_new_session"] is True


def test_topic_monitor_run_now_route_launches_when_idle(monkeypatch, tmp_path):
    status_path = tmp_path / "status.json"
    ds.write_topic_status("ai-news", "idle", status_path=status_path)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "RUN_TOPIC_MONITOR_LOOP_SH", tmp_path / "run-topic-monitor-loop.sh")
    (tmp_path / "run-topic-monitor-loop.sh").write_text("#!/bin/bash\ntrue\n")

    captured = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/daemons")
        status, headers, _body = _post(port, "/topic-monitor/run-now", {"csrf_token": token})
        assert status == 303
        flash_query = _flash_from_location(headers["Location"], prefix="/topic-monitor?")
        assert flash_query["ok"] == ["1"]
    assert captured["args"] == ["bash", str(tmp_path / "run-topic-monitor-loop.sh")]


def test_topic_monitor_run_now_route_refuses_when_a_topic_is_running(monkeypatch, tmp_path):
    status_path = tmp_path / "status.json"
    ds.write_topic_status("ai-news", "running", status_path=status_path)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_STATUS_PATH", status_path)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/daemons")
        status, headers, _body = _post(port, "/topic-monitor/run-now", {"csrf_token": token})
        assert status == 303
        flash_query = _flash_from_location(headers["Location"], prefix="/topic-monitor?")
        assert flash_query["ok"] == ["0"]


def test_topic_monitor_run_now_route_requires_csrf(monkeypatch, tmp_path):
    status_path = tmp_path / "status.json"
    ds.write_topic_status("ai-news", "idle", status_path=status_path)
    monkeypatch.setattr(ds, "TOPIC_MONITOR_STATUS_PATH", status_path)

    with _running_server() as port:
        status, _headers, _body = _post(port, "/topic-monitor/run-now", {"csrf_token": ""})
        assert status == 403


def test_render_topic_monitor_page_omits_timestamp_for_a_never_run_topic(monkeypatch):
    """A topic with no status entry at all has no updated_at to render - that
    must omit the timestamp, not crash or print an empty "  ago"."""
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [
        {"name": "ai-news", "label": "AI news", "brief": "x", "slack_bundle": None},
    ])
    monkeypatch.setattr(ds, "read_topic_status", lambda *a, **k: {"topics": {}})
    monkeypatch.setattr(ds, "list_topic_history", lambda name, history_dir=None: [])

    output = ds.render_topic_monitor_page()

    assert "AI news" in output
    assert "Never Run" in output
    assert "ago" not in output.split("<h1>Topic Monitor</h1>", 1)[1]


def test_render_daemons_page_shows_daemons_table(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(ds, "get_daemons_status", lambda *a, **k: [
        {"file": "com.example.on.plist", "label": "com.example.on", "loaded": True,
         "pid": "123", "program_arguments": ["/bin/true"], "run_at_load": True,
         "keep_alive": True, "schedule": None},
    ])

    output = ds.render_daemons_page()

    assert "<h1>Launchd Daemons</h1>" in output
    assert "com.example.on" in output


def test_render_daemons_page_flash_message_is_html_escaped():
    output = ds.render_daemons_page(flash="<script>alert(1)</script>", flash_ok=False)

    assert "<script>alert(1)</script>" not in output
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in output
    assert "<div class='flash flash-danger'>" in output


def test_render_daemons_page_no_flash_by_default():
    output = ds.render_daemons_page()

    assert "<div class='flash" not in output


def test_nav_active_class_matches_current_page(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(ds, "HISTORY_DIR", tmp_path)
    monkeypatch.setattr(ds.learning.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")
    monkeypatch.setattr(ds, "get_live_gitlab_state", lambda *a, **k: {})
    monkeypatch.setattr(ds, "get_project_memory", lambda *a, **k: {})
    monkeypatch.setattr(ds, "get_daemons_status", lambda *a, **k: [])
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", tmp_path / "does-not-exist-gitlab.json")
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "does-not-exist-slack.json")
    monkeypatch.setattr(loop_config, "DEFAULT_CONFIG_PATH", tmp_path / "does-not-exist-projects.json")

    assert "<a href='/' title='Dashboard' class='active'>" in ds.render_overview_page()
    assert "<a href='/history' title='Run History' class='active'>" in ds.render_history_page()
    assert "<a href='/gitlab' title='Live GitLab' class='active'>" in ds.render_gitlab_page()
    assert "<a href='/memory' title='Memory' class='active'>" in ds.render_memory_page()
    assert "<a href='/daemons' title='Daemons' class='active'>" in ds.render_daemons_page()
    assert "<a href='/settings' title='GitLab' class='active'>" in ds.render_settings_page()
    assert "<a href='/notifications' title='Notifications' class='active'>" in ds.render_slack_page()


def test_read_gitlab_config_missing_file_returns_empty_dict(tmp_path):
    assert ds.read_gitlab_config(tmp_path / "does-not-exist.json") == {}


def test_read_gitlab_config_malformed_json_returns_empty_dict(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("not valid json {{{")
    assert ds.read_gitlab_config(path) == {}


def test_read_gitlab_config_returns_parsed_dict(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"default": "acme", "instances": {}, "projects": {}}')
    assert ds.read_gitlab_config(path) == {"default": "acme", "instances": {}, "projects": {}}


def test_write_gitlab_config_roundtrips(tmp_path):
    path = tmp_path / "config.json"
    config = {"default": "acme", "instances": {"acme": {"url": "https://gitlab.acme.com", "token": "abc123"}}, "projects": {}}
    ds.write_gitlab_config(config, path)
    assert ds.read_gitlab_config(path) == config


def test_write_gitlab_config_sets_file_mode_0600(tmp_path):
    path = tmp_path / "config.json"
    ds.write_gitlab_config({"default": "x"}, path)
    assert (path.stat().st_mode & 0o777) == 0o600


def test_write_gitlab_config_creates_missing_parent_dir(tmp_path):
    path = tmp_path / "nested" / "does" / "not" / "exist" / "config.json"
    ds.write_gitlab_config({"default": "x"}, path)
    assert path.exists()
    assert ds.read_gitlab_config(path) == {"default": "x"}


def test_read_slack_config_missing_file_returns_empty_dict(tmp_path):
    assert ds.read_slack_config(tmp_path / "does-not-exist.json") == {}


def test_write_slack_config_roundtrips(tmp_path):
    path = tmp_path / "config.json"
    ds.write_slack_config({"webhook_url": "https://hooks.slack.com/services/x"}, path)
    assert ds.read_slack_config(path) == {"webhook_url": "https://hooks.slack.com/services/x"}


def test_mask_secret_long_token_shows_last_four():
    assert ds._mask_secret("glpat-abcdEFGH1234") == "••••1234"


def test_mask_secret_short_secret_shows_dots_only():
    assert ds._mask_secret("abc") == "••••"


def test_mask_secret_empty_string_shows_dots_only():
    assert ds._mask_secret("") == "••••"


def test_render_settings_page_masks_gitlab_token(monkeypatch, tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    gitlab_path.write_text(json.dumps({
        "default": "acme",
        "instances": {"acme": {"url": "https://gitlab.acme.com", "token": "glpat-supersecret1234"}},
        "projects": {},
    }))
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", gitlab_path)
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "slack.json")
    monkeypatch.setattr(loop_config, "DEFAULT_CONFIG_PATH", tmp_path / "does-not-exist-projects.json")

    output = ds.render_settings_page()

    assert "glpat-supersecret1234" not in output
    assert "••••1234" in output
    assert "https://gitlab.acme.com" in output  # URL itself is not a secret


def test_render_slack_page_masks_webhook(monkeypatch, tmp_path):
    slack_path = tmp_path / "slack.json"
    slack_path.write_text(json.dumps({"webhook_url": "https://hooks.slack.com/services/T00/B00/xyzSECRET"}))
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", slack_path)

    output = ds.render_slack_page()

    assert "xyzSECRET" not in output
    assert "https://hooks.slack.com" not in output
    assert "••••CRET" in output


def test_render_slack_page_labels_the_main_webhook_as_default(monkeypatch, tmp_path):
    """Distinguishes it from the per-bundle webhook overrides on the
    GitLab page's Access bundles section - both used to just say "Webhook"."""
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "does-not-exist-slack.json")

    output = ds.render_slack_page()

    assert "<strong>Default webhook:</strong>" in output


def test_render_slack_page_empty_config_shows_placeholder(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "does-not-exist-slack.json")

    output = ds.render_slack_page()

    assert "(not set)" in output


def test_render_settings_page_shows_default_badge(monkeypatch, tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    gitlab_path.write_text(json.dumps({
        "default": "acme",
        "instances": {
            "acme": {"url": "https://gitlab.acme.com", "token": "tok1"},
            "other": {"url": "https://gitlab.other.com", "token": "tok2"},
        },
        "projects": {},
    }))
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", gitlab_path)
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "slack.json")
    monkeypatch.setattr(loop_config, "DEFAULT_CONFIG_PATH", tmp_path / "does-not-exist-projects.json")

    output = ds.render_settings_page()

    acme_row = output.split("<td>acme")[1].split("</tr>")[0]
    other_row = output.split("<td>other")[1].split("</tr>")[0]
    assert "pill-blue" in acme_row
    assert "pill-blue" not in other_row


def test_render_settings_page_empty_config_shows_placeholders(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", tmp_path / "does-not-exist-gitlab.json")
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "does-not-exist-slack.json")
    monkeypatch.setattr(loop_config, "DEFAULT_CONFIG_PATH", tmp_path / "does-not-exist-projects.json")

    output = ds.render_settings_page()

    assert "(no GitLab instances configured)" in output
    assert "(no project aliases configured)" in output
    assert "(no tracked projects configured)" in output


def test_render_settings_page_flash_success(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", tmp_path / "gitlab.json")
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "slack.json")
    monkeypatch.setattr(loop_config, "DEFAULT_CONFIG_PATH", tmp_path / "does-not-exist-projects.json")

    output = ds.render_settings_page(flash="Added instance acme", flash_ok=True)

    assert "<div class='flash flash-success'>Added instance acme</div>" in output


def test_render_settings_page_flash_danger(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", tmp_path / "gitlab.json")
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "slack.json")
    monkeypatch.setattr(loop_config, "DEFAULT_CONFIG_PATH", tmp_path / "does-not-exist-projects.json")

    output = ds.render_settings_page(flash="Unknown instance: bogus", flash_ok=False)

    assert "<div class='flash flash-danger'>Unknown instance: bogus</div>" in output


def test_dashboard_server_integration_settings_route(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", tmp_path / "gitlab.json")
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "slack.json")
    monkeypatch.setattr(loop_config, "DEFAULT_CONFIG_PATH", tmp_path / "does-not-exist-projects.json")

    with _running_server() as port:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/settings", timeout=10) as response:
            assert response.status == 200
            body = response.read().decode("utf-8")
            assert "<h1>GitLab</h1>" in body


def test_dashboard_server_integration_slack_route(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "slack.json")

    with _running_server() as port:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/notifications", timeout=10) as response:
            assert response.status == 200
            body = response.read().decode("utf-8")
            assert "<h1>Notifications</h1>" in body


def test_set_default_gitlab_instance_success(tmp_path):
    path = tmp_path / "config.json"
    ds.write_gitlab_config({"default": "a", "instances": {"a": {}, "b": {}}, "projects": {}}, path)
    ok, message = ds.set_default_gitlab_instance("b", path)
    assert ok is True
    assert ds.read_gitlab_config(path)["default"] == "b"


def test_set_default_gitlab_instance_unknown_instance_rejected(tmp_path):
    path = tmp_path / "config.json"
    ds.write_gitlab_config({"default": "a", "instances": {"a": {}}, "projects": {}}, path)
    ok, message = ds.set_default_gitlab_instance("bogus", path)
    assert ok is False
    assert ds.read_gitlab_config(path)["default"] == "a"


def test_upsert_gitlab_instance_creates_new(tmp_path):
    path = tmp_path / "config.json"
    ds.write_gitlab_config({"default": "", "instances": {}, "projects": {}}, path)
    ok, message = ds.upsert_gitlab_instance("acme", "https://gitlab.acme.com", "tok123", path)
    assert ok is True
    saved = ds.read_gitlab_config(path)["instances"]["acme"]
    assert saved == {"url": "https://gitlab.acme.com", "token": "tok123"}


def test_upsert_gitlab_instance_new_without_token_rejected(tmp_path):
    path = tmp_path / "config.json"
    ds.write_gitlab_config({"default": "", "instances": {}, "projects": {}}, path)
    ok, message = ds.upsert_gitlab_instance("acme", "https://gitlab.acme.com", "", path)
    assert ok is False
    assert "acme" not in ds.read_gitlab_config(path)["instances"]


def test_upsert_gitlab_instance_edit_blank_token_keeps_existing(tmp_path):
    path = tmp_path / "config.json"
    ds.write_gitlab_config({"default": "", "instances": {"acme": {"url": "https://old.example.com", "token": "original"}}, "projects": {}}, path)
    ok, message = ds.upsert_gitlab_instance("acme", "https://new.example.com", "", path)
    assert ok is True
    saved = ds.read_gitlab_config(path)["instances"]["acme"]
    assert saved == {"url": "https://new.example.com", "token": "original"}


def test_upsert_gitlab_instance_edit_new_token_replaces(tmp_path):
    path = tmp_path / "config.json"
    ds.write_gitlab_config({"default": "", "instances": {"acme": {"url": "https://old.example.com", "token": "original"}}, "projects": {}}, path)
    ok, message = ds.upsert_gitlab_instance("acme", "https://old.example.com", "replaced", path)
    assert ok is True
    assert ds.read_gitlab_config(path)["instances"]["acme"]["token"] == "replaced"


def test_upsert_gitlab_instance_blank_url_rejected(tmp_path):
    path = tmp_path / "config.json"
    ds.write_gitlab_config({"default": "", "instances": {"acme": {"url": "https://old.example.com", "token": "tok"}}, "projects": {}}, path)
    ok, message = ds.upsert_gitlab_instance("acme", "", "newtok", path)
    assert ok is False
    assert ds.read_gitlab_config(path)["instances"]["acme"]["url"] == "https://old.example.com"


def test_delete_gitlab_instance_success(tmp_path):
    path = tmp_path / "config.json"
    ds.write_gitlab_config({"default": "a", "instances": {"a": {}, "b": {}}, "projects": {}}, path)
    ok, message = ds.delete_gitlab_instance("b", path)
    assert ok is True
    assert "b" not in ds.read_gitlab_config(path)["instances"]


def test_delete_gitlab_instance_blocks_default(tmp_path):
    path = tmp_path / "config.json"
    ds.write_gitlab_config({"default": "a", "instances": {"a": {}}, "projects": {}}, path)
    ok, message = ds.delete_gitlab_instance("a", path)
    assert ok is False
    assert "a" in ds.read_gitlab_config(path)["instances"]


def test_delete_gitlab_instance_blocks_when_referenced_by_project(tmp_path):
    path = tmp_path / "config.json"
    ds.write_gitlab_config({
        "default": "a",
        "instances": {"a": {}, "b": {}},
        "projects": {"proj1": {"project_id": "x/y", "instance": "b"}},
    }, path)
    ok, message = ds.delete_gitlab_instance("b", path)
    assert ok is False
    assert "proj1" in message
    assert "b" in ds.read_gitlab_config(path)["instances"]


def test_delete_gitlab_instance_blocks_when_referenced_by_bundle(tmp_path):
    path = tmp_path / "config.json"
    ds.write_gitlab_config({
        "default": "a",
        "instances": {"a": {}, "b": {}},
        "bundles": {"vertex-limited": {"instance": "b", "token": "tok"}},
        "projects": {},
    }, path)
    ok, message = ds.delete_gitlab_instance("b", path)
    assert ok is False
    assert "vertex-limited" in message
    assert "b" in ds.read_gitlab_config(path)["instances"]


def test_delete_gitlab_instance_blocks_when_referenced_by_project_and_bundle(tmp_path):
    path = tmp_path / "config.json"
    ds.write_gitlab_config({
        "default": "a",
        "instances": {"a": {}, "b": {}},
        "bundles": {"vertex-limited": {"instance": "b", "token": "tok"}},
        "projects": {"proj1": {"project_id": "x/y", "instance": "b"}},
    }, path)
    ok, message = ds.delete_gitlab_instance("b", path)
    assert ok is False
    assert "proj1" in message
    assert "vertex-limited" in message


def test_upsert_gitlab_project_success(tmp_path):
    path = tmp_path / "config.json"
    ds.write_gitlab_config({"default": "a", "instances": {"a": {}}, "projects": {}}, path)
    ok, message = ds.upsert_gitlab_project("myproj", "ns/myproj", "a", config_path=path)
    assert ok is True
    assert ds.read_gitlab_config(path)["projects"]["myproj"] == {"project_id": "ns/myproj", "instance": "a"}


def test_upsert_gitlab_project_unknown_instance_rejected(tmp_path):
    path = tmp_path / "config.json"
    ds.write_gitlab_config({"default": "a", "instances": {"a": {}}, "projects": {}}, path)
    ok, message = ds.upsert_gitlab_project("myproj", "ns/myproj", "bogus", config_path=path)
    assert ok is False
    assert "myproj" not in ds.read_gitlab_config(path)["projects"]


def test_delete_gitlab_project_success(tmp_path):
    path = tmp_path / "config.json"
    ds.write_gitlab_config({"default": "a", "instances": {"a": {}}, "projects": {"myproj": {"project_id": "ns/myproj", "instance": "a"}}}, path)
    ok, message = ds.delete_gitlab_project("myproj", path)
    assert ok is True
    assert "myproj" not in ds.read_gitlab_config(path)["projects"]


def test_update_slack_webhook_success(tmp_path):
    path = tmp_path / "config.json"
    ok, message = ds.update_slack_webhook("https://hooks.slack.com/services/new", path)
    assert ok is True
    assert ds.read_slack_config(path)["webhook_url"] == "https://hooks.slack.com/services/new"


def test_update_slack_webhook_blank_rejected(tmp_path):
    path = tmp_path / "config.json"
    ds.write_slack_config({"webhook_url": "https://hooks.slack.com/services/original"}, path)
    ok, message = ds.update_slack_webhook("", path)
    assert ok is False
    assert ds.read_slack_config(path)["webhook_url"] == "https://hooks.slack.com/services/original"


def test_read_custom_instructions_returns_empty_string_when_missing(tmp_path):
    assert ds.read_custom_instructions(tmp_path / "does-not-exist.md") == ""


def test_write_then_read_custom_instructions_round_trips(tmp_path):
    path = tmp_path / "nested" / "instructions.md"

    ok, message = ds.write_custom_instructions("Always run tests before committing.", path)

    assert ok is True
    assert ds.read_custom_instructions(path) == "Always run tests before committing."


def test_write_custom_instructions_allows_clearing_to_blank(tmp_path):
    path = tmp_path / "instructions.md"
    ds.write_custom_instructions("some text", path)

    ok, message = ds.write_custom_instructions("", path)

    assert ok is True
    assert ds.read_custom_instructions(path) == ""


def test_render_instructions_page_shows_subtitle_and_current_text(monkeypatch, tmp_path):
    path = tmp_path / "instructions.md"
    path.write_text("Prefer tabs over spaces.")
    monkeypatch.setattr(ds, "CUSTOM_INSTRUCTIONS_PATH", path)
    monkeypatch.setattr(ai_cli_config, "DEFAULT_CONFIG_PATH", tmp_path / "does-not-exist.json")

    output = ds.render_instructions_page()

    assert "<h1>Instructions</h1>" in output
    assert "Include specific instructions in Claude Code's system prompt" in output
    assert "Prefer tabs over spaces." in output
    assert "<textarea" in output


def test_render_instructions_page_subtitle_names_the_selected_ai_cli(monkeypatch, tmp_path):
    config_path = tmp_path / "ai_cli.json"
    config_path.write_text('{"cli": "codex"}')
    monkeypatch.setattr(ai_cli_config, "DEFAULT_CONFIG_PATH", config_path)

    output = ds.render_instructions_page()

    assert "Include specific instructions in Codex CLI's system prompt" in output


def test_render_instructions_page_textarea_is_large():
    output = ds.render_instructions_page()

    assert "rows='24'" in output


def test_render_instructions_page_escapes_saved_text(monkeypatch, tmp_path):
    path = tmp_path / "instructions.md"
    path.write_text("<script>alert(1)</script>")
    monkeypatch.setattr(ds, "CUSTOM_INSTRUCTIONS_PATH", path)

    output = ds.render_instructions_page()

    assert "<script>alert(1)</script>" not in output
    assert "&lt;script&gt;" in output


def test_instructions_route_saves_and_redirects(tmp_path, monkeypatch):
    path = tmp_path / "instructions.md"
    monkeypatch.setattr(ds, "CUSTOM_INSTRUCTIONS_PATH", path)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/instructions")
        status, headers, _body = _post(port, "/instructions", {
            "instructions": "Always write tests first.", "csrf_token": token,
        })
        assert status == 303
        assert headers.get("Location", "").startswith("/instructions?")

    assert ds.read_custom_instructions(path) == "Always write tests first."


def test_instructions_route_requires_csrf(tmp_path, monkeypatch):
    path = tmp_path / "instructions.md"
    monkeypatch.setattr(ds, "CUSTOM_INSTRUCTIONS_PATH", path)

    with _running_server() as port:
        status, _headers, _body = _post(port, "/instructions", {"instructions": "sneaky"})
        assert status == 403

    assert ds.read_custom_instructions(path) == ""


def test_settings_route_set_default_requires_csrf(monkeypatch, tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({"default": "a", "instances": {"a": {}, "b": {}}, "projects": {}}, gitlab_path)
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", gitlab_path)
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "slack.json")

    with _running_server() as port:
        status, _headers, _body = _post(port, "/settings/gitlab/default", {"instance": "b", "csrf_token": ""})
        assert status == 403
    assert ds.read_gitlab_config(gitlab_path)["default"] == "a"


def test_settings_route_set_default_success(monkeypatch, tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({"default": "a", "instances": {"a": {}, "b": {}}, "projects": {}}, gitlab_path)
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", gitlab_path)
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "slack.json")

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/settings")
        status, headers, _body = _post(port, "/settings/gitlab/default", {"instance": "b", "csrf_token": token})
        assert status == 303
        flash_query = _flash_from_location(headers["Location"], prefix="/settings?")
        assert flash_query["ok"] == ["1"]
    assert ds.read_gitlab_config(gitlab_path)["default"] == "b"


def test_settings_route_add_instance_success(monkeypatch, tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({"default": "", "instances": {}, "projects": {}}, gitlab_path)
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", gitlab_path)
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "slack.json")

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/settings")
        status, _headers, _body = _post(port, "/settings/gitlab/instances", {
            "alias": "acme", "url": "https://gitlab.acme.com", "token": "newtok", "csrf_token": token,
        })
        assert status == 303
    assert ds.read_gitlab_config(gitlab_path)["instances"]["acme"]["url"] == "https://gitlab.acme.com"


def test_settings_route_delete_instance_success(monkeypatch, tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({"default": "a", "instances": {"a": {}, "b": {}}, "projects": {}}, gitlab_path)
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", gitlab_path)
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "slack.json")

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/settings")
        status, _headers, _body = _post(port, "/settings/gitlab/instances/b/delete", {"csrf_token": token})
        assert status == 303
    assert "b" not in ds.read_gitlab_config(gitlab_path)["instances"]


def test_settings_route_add_project_success(monkeypatch, tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({"default": "a", "instances": {"a": {}}, "projects": {}}, gitlab_path)
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", gitlab_path)
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "slack.json")

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/settings")
        status, _headers, _body = _post(port, "/settings/gitlab/projects", {
            "alias": "myproj", "project_id": "ns/myproj", "instance": "a", "csrf_token": token,
        })
        assert status == 303
    assert ds.read_gitlab_config(gitlab_path)["projects"]["myproj"]["project_id"] == "ns/myproj"


def test_settings_route_delete_project_success(monkeypatch, tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({"default": "a", "instances": {"a": {}}, "projects": {"myproj": {"project_id": "ns/myproj", "instance": "a"}}}, gitlab_path)
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", gitlab_path)
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "slack.json")

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/settings")
        status, _headers, _body = _post(port, "/settings/gitlab/projects/myproj/delete", {"csrf_token": token})
        assert status == 303
    assert "myproj" not in ds.read_gitlab_config(gitlab_path)["projects"]


def test_slack_route_update_webhook_success(monkeypatch, tmp_path):
    slack_path = tmp_path / "slack.json"
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", slack_path)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/notifications")
        status, _headers, _body = _post(port, "/notifications/webhook", {
            "webhook_url": "https://hooks.slack.com/services/new", "csrf_token": token,
        })
        assert status == 303
    assert ds.read_slack_config(slack_path)["webhook_url"] == "https://hooks.slack.com/services/new"


def test_slack_route_update_webhook_blank_rejected(monkeypatch, tmp_path):
    slack_path = tmp_path / "slack.json"
    ds.write_slack_config({"webhook_url": "https://hooks.slack.com/services/original"}, slack_path)
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", slack_path)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/notifications")
        status, headers, _body = _post(port, "/notifications/webhook", {"webhook_url": "", "csrf_token": token})
        assert status == 303
        flash_query = _flash_from_location(headers["Location"], prefix="/notifications?")
        assert flash_query["ok"] == ["0"]
    assert ds.read_slack_config(slack_path)["webhook_url"] == "https://hooks.slack.com/services/original"


def test_ai_cli_nav_item_present_with_icon():
    matching = [item for item in ds._NAV_ITEMS if item[0] == "ai_cli"]
    assert len(matching) == 1
    key, href, label, icon = matching[0]
    assert href == "/ai-cli"
    assert label == "AI CLI"
    assert icon == "<span class='material-symbols-outlined' aria-hidden='true'>smart_toy</span>"


def test_ai_cli_material_symbol_name_is_registered():
    assert "smart_toy" in ds._MATERIAL_SYMBOLS_ICON_NAMES.split(",")


def test_render_ai_cli_page_shows_current_selection(monkeypatch, tmp_path):
    config_path = tmp_path / "ai_cli.json"
    config_path.write_text('{"cli": "codex"}')
    monkeypatch.setattr(ai_cli_config, "DEFAULT_CONFIG_PATH", config_path)

    output = ds.render_ai_cli_page()

    assert "<h1>AI CLI</h1>" in output
    # "codex" alone would be true regardless of which CLI is actually
    # selected (both option values always appear in the rendered
    # dropdown) - assert on the selected <option> marker instead, which
    # only appears when codex is actually the current selection.
    assert "<option value='codex' selected>" in output


def test_render_ai_cli_page_defaults_to_claude_when_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_cli_config, "DEFAULT_CONFIG_PATH", tmp_path / "does-not-exist.json")

    output = ds.render_ai_cli_page()

    assert "claude" in output


def test_render_ai_cli_page_flash_success(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_cli_config, "DEFAULT_CONFIG_PATH", tmp_path / "ai_cli.json")

    output = ds.render_ai_cli_page(flash="Switched to codex", flash_ok=True)

    assert "<div class='flash flash-success'>Switched to codex</div>" in output


def test_render_ai_cli_page_closed_dropdown_trigger_shows_availability_label(monkeypatch, tmp_path):
    # The closed-dropdown trigger (_custom_select's own
    # <span class='custom-select-value'>) must carry the same
    # availability-annotated label as the option/menu-item text - it's
    # the only part of the control visible before the user opens the
    # dropdown, so this is where the "not found on PATH" warning
    # actually needs to be seen.
    config_path = tmp_path / "ai_cli.json"
    config_path.write_text('{"cli": "codex"}')
    monkeypatch.setattr(ai_cli_config, "DEFAULT_CONFIG_PATH", config_path)
    monkeypatch.setattr(ds, "_cli_available", lambda name: True)

    output = ds.render_ai_cli_page()

    assert "custom-select-value'>Codex CLI (installed)</span>" in output


def test_dashboard_server_integration_ai_cli_route(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_cli_config, "DEFAULT_CONFIG_PATH", tmp_path / "ai_cli.json")

    with _running_server() as port:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ai-cli", timeout=10) as response:
            assert response.status == 200
            body = response.read().decode("utf-8")
            assert "<h1>AI CLI</h1>" in body


def test_do_post_ai_cli_without_csrf_token_is_forbidden_and_mutates_nothing(monkeypatch, tmp_path):
    config_path = tmp_path / "ai_cli.json"
    monkeypatch.setattr(ai_cli_config, "DEFAULT_CONFIG_PATH", config_path)

    with _running_server() as port:
        status, _headers, _body = _post(port, "/ai-cli", {"csrf_token": "", "cli": "codex"})
        assert status == 403
        assert not config_path.exists()


def test_do_post_ai_cli_with_valid_csrf_token_switches_cli(monkeypatch, tmp_path):
    config_path = tmp_path / "ai_cli.json"
    monkeypatch.setattr(ai_cli_config, "DEFAULT_CONFIG_PATH", config_path)

    with _running_server() as port:
        token = _fetch_csrf_token(port, path="/ai-cli")
        status, headers, _body = _post(port, "/ai-cli", {"csrf_token": token, "cli": "codex"})
        assert status == 303
        assert headers["Location"].startswith("/ai-cli?")
        assert ai_cli_config.get_selected_cli(config_path) == "codex"


def test_upsert_gitlab_instance_preserves_unknown_fields_on_edit(tmp_path):
    path = tmp_path / "config.json"
    ds.write_gitlab_config({"default": "", "instances": {"acme": {"url": "https://old.example.com", "token": "tok", "description": "team instance"}}, "projects": {}}, path)
    ok, message = ds.upsert_gitlab_instance("acme", "https://new.example.com", "", path)
    assert ok is True
    assert ds.read_gitlab_config(path)["instances"]["acme"]["description"] == "team instance"


def test_upsert_gitlab_project_preserves_unknown_fields_on_edit(tmp_path):
    path = tmp_path / "config.json"
    ds.write_gitlab_config({"default": "a", "instances": {"a": {}}, "projects": {"myproj": {"project_id": "ns/old", "instance": "a", "description": "the main app"}}}, path)
    ok, message = ds.upsert_gitlab_project("myproj", "ns/new", "a", config_path=path)
    assert ok is True
    assert ds.read_gitlab_config(path)["projects"]["myproj"]["description"] == "the main app"


def test_settings_route_delete_instance_with_space_in_alias(monkeypatch, tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({"default": "a", "instances": {"a": {}, "my inst": {}}, "projects": {}}, gitlab_path)
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", gitlab_path)
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "slack.json")

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/settings")
        # Simulate what a browser actually sends: the alias percent-encoded in the URL path.
        status, _headers, _body = _post(port, "/settings/gitlab/instances/my%20inst/delete", {"csrf_token": token})
        assert status == 303
    assert "my inst" not in ds.read_gitlab_config(gitlab_path)["instances"]


@pytest.mark.parametrize("path,fields", [
    ("/settings/gitlab/default", {"instance": "a"}),
    ("/settings/gitlab/instances", {"alias": "x", "url": "https://x.example.com", "token": "tok"}),
    ("/settings/gitlab/instances/a/delete", {}),
    ("/settings/gitlab/projects", {"alias": "x", "project_id": "ns/x", "instance": "a"}),
    ("/settings/gitlab/projects/x/delete", {}),
    ("/notifications/webhook", {"webhook_url": "https://hooks.slack.com/services/x"}),
    ("/settings/loop-config", {"assignee_username": "encore", "worktree_root": "/tmp/wt", "gitlab_instance": "a"}),
    ("/settings/loop-projects", {"alias": "x", "project_id": "ns/x"}),
    ("/settings/loop-projects/x/delete", {}),
])
def test_settings_routes_all_require_csrf(monkeypatch, tmp_path, path, fields):
    gitlab_path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({"default": "a", "instances": {"a": {}}, "projects": {"x": {"project_id": "ns/x", "instance": "a"}}}, gitlab_path)
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", gitlab_path)
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "slack.json")

    with _running_server() as port:
        status, _headers, _body = _post(port, path, {**fields, "csrf_token": ""})
        assert status == 403


def test_read_messages_missing_file_returns_empty_list(tmp_path):
    assert ds.read_messages(tmp_path / "does-not-exist.json") == []


def test_read_messages_malformed_json_returns_empty_list(tmp_path):
    path = tmp_path / "messages.json"
    path.write_text("not valid json {{{")
    assert ds.read_messages(path) == []


def test_read_messages_non_list_json_returns_empty_list(tmp_path):
    path = tmp_path / "messages.json"
    path.write_text('{"not": "a list"}')
    assert ds.read_messages(path) == []


def test_read_messages_drops_non_dict_elements(tmp_path):
    path = tmp_path / "messages.json"
    path.write_text('[1, {"from": "user", "text": "ok", "timestamp": "t"}, "x"]')
    messages = ds.read_messages(path)
    assert messages == [{"from": "user", "text": "ok", "timestamp": "t"}]


def test_append_message_user_sets_seen_by_loop_false(tmp_path):
    path = tmp_path / "messages.json"
    ds.append_message("user", "please hold off on brightleaf.web today", path)

    messages = ds.read_messages(path)
    assert len(messages) == 1
    assert messages[0]["from"] == "user"
    assert messages[0]["text"] == "please hold off on brightleaf.web today"
    assert messages[0]["seen_by_loop"] is False
    assert "timestamp" in messages[0]


def test_append_message_loop_has_no_seen_by_loop_field(tmp_path):
    path = tmp_path / "messages.json"
    ds.append_message("loop", "understood, skipping brightleaf.web", path)

    messages = ds.read_messages(path)
    assert len(messages) == 1
    assert messages[0]["from"] == "loop"
    assert "seen_by_loop" not in messages[0]


def test_append_message_preserves_order(tmp_path):
    path = tmp_path / "messages.json"
    ds.append_message("user", "first", path)
    ds.append_message("loop", "second", path)
    ds.append_message("user", "third", path)

    messages = ds.read_messages(path)
    assert [m["text"] for m in messages] == ["first", "second", "third"]


def test_pop_unseen_user_messages_returns_and_marks_seen(tmp_path):
    path = tmp_path / "messages.json"
    ds.append_message("user", "first message", path)
    ds.append_message("loop", "a loop reply, never unseen-user", path)
    ds.append_message("user", "second message", path)

    unseen = ds.pop_unseen_user_messages(path)
    assert [m["text"] for m in unseen] == ["first message", "second message"]

    # Second call returns nothing - already marked seen.
    assert ds.pop_unseen_user_messages(path) == []

    # The underlying file reflects the seen state, not just the return value.
    all_messages = ds.read_messages(path)
    user_messages = [m for m in all_messages if m["from"] == "user"]
    assert all(m["seen_by_loop"] is True for m in user_messages)


def test_pop_unseen_user_messages_ignores_loop_messages(tmp_path):
    path = tmp_path / "messages.json"
    ds.append_message("loop", "only a loop message", path)

    assert ds.pop_unseen_user_messages(path) == []


def test_pop_unseen_user_messages_empty_file_returns_empty_list(tmp_path):
    path = tmp_path / "does-not-exist.json"
    assert ds.pop_unseen_user_messages(path) == []


def test_append_message_blocks_while_an_external_holder_has_the_lock(tmp_path):
    """Fix 8: append_message and pop_unseen_user_messages can run in
    genuinely different OS processes (this dashboard's own background
    chat threads vs. the separately-scheduled GitLab loop's own `python3
    dashboard_server.py read-messages` invocation), so an in-process
    threading.Lock would not protect their read-modify-write cycle from
    each other - only real, cross-process fcntl.flock does. This proves
    the lock is actually acquired for real: a thread calling
    append_message must not complete while an external holder of the same
    <path>.lock file's exclusive flock is still holding it, and must
    complete promptly once that holder releases it."""
    messages_path = tmp_path / "messages.json"
    messages_path.write_text("[]")
    lock_path = tmp_path / "messages.json.lock"

    holder = open(lock_path, "a+")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        finished = {"value": False}

        def worker():
            ds.append_message("user", "hello", messages_path)
            finished["value"] = True

        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.3)
        assert finished["value"] is False, "append_message must block while the lock is held elsewhere"
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    t.join(timeout=5)
    assert finished["value"] is True
    saved = json.loads(messages_path.read_text())
    assert saved[-1]["text"] == "hello"


def test_pop_unseen_user_messages_blocks_while_an_external_holder_has_the_lock(tmp_path):
    """Same cross-process locking guarantee as append_message, proven the
    same way, for the other writer of outputs/messages.json."""
    messages_path = tmp_path / "messages.json"
    ds.append_message("user", "pending question", messages_path)
    lock_path = tmp_path / "messages.json.lock"

    holder = open(lock_path, "a+")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        result = {"value": None}

        def worker():
            result["value"] = ds.pop_unseen_user_messages(messages_path)

        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.3)
        assert result["value"] is None, "pop_unseen_user_messages must block while the lock is held elsewhere"
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    t.join(timeout=5)
    assert result["value"] is not None and len(result["value"]) == 1
    assert result["value"][0]["text"] == "pending question"


def test_write_status_cli_records_current_issue_and_step(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(sys, "argv", [
        "dashboard_server.py", "write-status", "running",
        "--current-issue", "brightleaf.web #1206", "--current-step", "verifying",
    ])

    ds.main()

    written = ds.read_status(status_path)
    assert written["state"] == "running"
    assert written["current_issue"] == "brightleaf.web #1206"
    assert written["current_step"] == "verifying"


def test_write_status_cli_idle_clears_progress_fields(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    ds.write_status("running", status_path, current_issue="brightleaf.web #1206", current_step="verifying")
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(sys, "argv", ["dashboard_server.py", "write-status", "idle", "--exit-code", "0"])

    ds.main()

    written = ds.read_status(status_path)
    assert written["state"] == "idle"
    assert "current_issue" not in written
    assert "current_step" not in written


def test_read_messages_cli_prints_unseen_user_messages_as_json(tmp_path, monkeypatch, capsys):
    messages_path = tmp_path / "messages.json"
    ds.append_message("user", "please hold off on brightleaf.web today", messages_path)
    monkeypatch.setattr(ds, "MESSAGES_PATH", messages_path)
    monkeypatch.setattr(sys, "argv", ["dashboard_server.py", "read-messages"])

    ds.main()

    output = json.loads(capsys.readouterr().out)
    assert len(output) == 1
    assert output[0]["text"] == "please hold off on brightleaf.web today"

    # A second CLI call returns nothing new - already marked seen.
    ds.main()
    assert json.loads(capsys.readouterr().out) == []


def test_add_message_cli_appends_a_loop_message(tmp_path, monkeypatch):
    messages_path = tmp_path / "messages.json"
    monkeypatch.setattr(ds, "MESSAGES_PATH", messages_path)
    monkeypatch.setattr(sys, "argv", ["dashboard_server.py", "add-message", "loop", "understood, skipping brightleaf.web"])

    ds.main()

    messages = ds.read_messages(messages_path)
    assert len(messages) == 1
    assert messages[0]["from"] == "loop"
    assert messages[0]["text"] == "understood, skipping brightleaf.web"


def test_add_message_cli_rejects_invalid_from(tmp_path, monkeypatch, capsys):
    messages_path = tmp_path / "messages.json"
    monkeypatch.setattr(ds, "MESSAGES_PATH", messages_path)
    monkeypatch.setattr(sys, "argv", ["dashboard_server.py", "add-message", "bot", "hi"])

    with pytest.raises(SystemExit):
        ds.main()

    assert ds.read_messages(messages_path) == []


def test_chat_tool_status_combines_gitlab_and_topic_monitor_state(tmp_path):
    """_chat_tool_status takes every path as an injectable, None-default
    parameter (this project's own DI convention - see CLAUDE.md) rather
    than reaching for module globals with no way to redirect them. Passing
    tmp_path fixtures for ALL four paths (including the topics config,
    which a prior version of this test had no way to redirect at all - it
    silently read this machine's real ~/.loop-engineering/topics.json)
    proves the function never touches real config outside a test."""
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({"state": "idle"}))
    topic_status_path = tmp_path / "topic-status.json"
    topic_status_path.write_text(json.dumps({"topics": {}}))
    projects_config_path = tmp_path / "missing-projects.json"
    topics_config_path = tmp_path / "missing-topics.json"

    result = ds._chat_tool_status(
        status_path=status_path,
        topic_status_path=topic_status_path,
        projects_config_path=projects_config_path,
        topics_config_path=topics_config_path,
    )
    assert result["gitlab_loop"]["state"] == "idle"
    assert result["topic_monitor"]["topics"] == {}
    assert result["configured_topics"] == []
    assert result["tracked_projects"] == []


def test_chat_tool_status_defaults_to_module_constants_when_no_paths_given():
    """The `chat-tool status` CLI action calls _chat_tool_status() with no
    arguments at all - this confirms that still works and resolves to the
    real module-level constants (per this file's None-default DI
    convention), not that it crashes now that the parameters exist."""
    result = ds._chat_tool_status()
    assert "gitlab_loop" in result and "topic_monitor" in result
    assert "configured_topics" in result and "tracked_projects" in result


def test_chat_tool_history_list_and_read(tmp_path):
    (tmp_path / "2026-08-20.md").write_text("# Review\ncontent")
    assert ds._chat_tool_history_list(history_dir=tmp_path) == ["2026-08-20.md"]
    result = ds._chat_tool_history_read("2026-08-20.md", history_dir=tmp_path)
    assert result == {"content": "# Review\ncontent"}


def test_chat_tool_history_read_missing_file_returns_error(tmp_path):
    result = ds._chat_tool_history_read("missing.md", history_dir=tmp_path)
    assert "error" in result


def test_chat_tool_progress_reads_file(tmp_path):
    progress_path = tmp_path / "PROGRESS.md"
    progress_path.write_text("# Progress\nlast run: today")
    result = ds._chat_tool_progress(progress_path=progress_path)
    assert result == {"content": "# Progress\nlast run: today"}


def test_chat_tool_progress_missing_file_returns_error(tmp_path):
    result = ds._chat_tool_progress(progress_path=tmp_path / "missing.md")
    assert "error" in result


def test_dispatch_chat_tool_status_prints_json(capsys):
    ds._dispatch_chat_tool("status", [])
    output = json.loads(capsys.readouterr().out)
    assert "gitlab_loop" in output


def test_dispatch_chat_tool_unknown_action_exits_1(capsys):
    with pytest.raises(SystemExit) as exc_info:
        ds._dispatch_chat_tool("not-a-real-action", [])
    assert exc_info.value.code == 1
    assert "Unknown chat-tool action" in capsys.readouterr().err


def test_dispatch_chat_tool_history_read_without_name_exits_1(capsys):
    with pytest.raises(SystemExit) as exc_info:
        ds._dispatch_chat_tool("history-read", [])
    assert exc_info.value.code == 1


def test_chat_tool_daemon_enable_and_disable(tmp_path, monkeypatch):
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    (launchd_dir / "com.hermes.test.plist").write_text("<plist></plist>")
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()

    def fake_runner(args, **kwargs):
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(ds, "_resolve_runner", lambda runner: fake_runner)
    enable_result = ds._chat_tool_daemon_enable(
        "com.hermes.test.plist", launchd_dir=launchd_dir,
    )
    assert "ok" in enable_result and "message" in enable_result

    # Was previously untested by this test despite its name promising
    # both enable and disable - mirrors the enable assertion above.
    disable_result = ds._chat_tool_daemon_disable(
        "com.hermes.test.plist", launchd_dir=launchd_dir,
    )
    assert "ok" in disable_result and "message" in disable_result


def test_chat_tool_daemon_disable_refuses_the_dashboards_own_plist(tmp_path):
    """Fix 6: disable_daemon's `launchctl unload -w` persists the disabled
    state, so a chat message that disabled the dashboard's own daemon
    would kill the very process serving that reply, with no way to
    re-enable it from a now-dead dashboard UI. This must be refused before
    disable_daemon is ever called, regardless of whether the plist exists
    on disk."""
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    (launchd_dir / ds.DASHBOARD_DAEMON_PLIST).write_text("<plist></plist>")

    result = ds._chat_tool_daemon_disable(ds.DASHBOARD_DAEMON_PLIST, launchd_dir=launchd_dir)

    assert result["ok"] is False
    assert "dashboard" in result["message"].lower()


def test_chat_tool_daemon_disable_refuses_dashboard_plist_via_path_traversal(tmp_path):
    """Same refusal must hold even if the filename arrives with a path
    prefix - _chat_tool_daemon_disable takes Path(filename).name before
    comparing, matching the same discipline disable_daemon itself already
    uses for path traversal."""
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()

    result = ds._chat_tool_daemon_disable("../../" + ds.DASHBOARD_DAEMON_PLIST, launchd_dir=launchd_dir)

    assert result["ok"] is False
    assert "dashboard" in result["message"].lower()


def test_chat_tool_daemon_disable_refuses_dashboard_plist_case_variant(tmp_path):
    """The refusal check used a case-SENSITIVE `==` against
    DASHBOARD_DAEMON_PLIST. This repo lives on a case-insensitive
    filesystem (macOS APFS), so a differently-cased filename like
    "COM.HERMES.LOOP-ENGINEERING-DASHBOARD.plist" would walk straight past
    that comparison and still reach disable_daemon, which resolves the
    file on disk case-insensitively too and would disable the real
    dashboard daemon. The refusal must fire for any case variant of the
    real plist name, not just the exact-case original."""
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    (launchd_dir / ds.DASHBOARD_DAEMON_PLIST).write_text("<plist></plist>")

    case_variant = ds.DASHBOARD_DAEMON_PLIST.upper()
    assert case_variant != ds.DASHBOARD_DAEMON_PLIST  # sanity: actually a different string

    result = ds._chat_tool_daemon_disable(case_variant, launchd_dir=launchd_dir)

    assert result["ok"] is False
    assert "dashboard" in result["message"].lower()


def test_chat_tool_run_now_gitlab_refuses_when_already_running(tmp_path):
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({"state": "running"}))
    result = ds._chat_tool_run_now("gitlab", status_path=status_path, run_loop_path=tmp_path / "run-loop.sh")
    assert result == {"ok": False, "message": "A run is already in progress"}


def test_chat_tool_run_now_topic_monitor_refuses_when_already_running(tmp_path):
    status_path = tmp_path / "topic-status.json"
    status_path.write_text(json.dumps({"topics": {"ai": {"state": "running"}}}))
    result = ds._chat_tool_run_now(
        "topic-monitor", topic_status_path=status_path, topic_run_loop_path=tmp_path / "run-topic-monitor-loop.sh",
    )
    assert result == {"ok": False, "message": "A run is already in progress"}


def test_chat_tool_run_now_unknown_kind_returns_error():
    result = ds._chat_tool_run_now("not-a-real-kind")
    assert "error" in result


def test_chat_tool_run_issue_refuses_when_already_running(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({"state": "running"}))
    loop_config_path = tmp_path / "projects.json"
    loop_config_path.write_text(json.dumps({
        "gitlab_instance": "acme",
        "projects": {"harbor": {"project_id": "acme/harbor/harbor"}},
    }))
    gitlab_config_path = tmp_path / "gitlab_config.json"
    gitlab_config_path.write_text(json.dumps({
        "instances": {"acme": {"url": "https://gitlab.acme.com"}},
    }))
    popen_called = {"value": False}

    def fake_popen(*args, **kwargs):
        popen_called["value"] = True
        raise AssertionError("Popen should not be called")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = ds._chat_tool_run_issue(
        "https://gitlab.acme.com/acme/harbor/harbor/-/issues/482",
        status_path=status_path, run_loop_path=tmp_path / "run-loop.sh",
        loop_config_path=loop_config_path, gitlab_config_path=gitlab_config_path,
    )

    assert result == {"ok": False, "message": "A run is already in progress"}
    assert popen_called["value"] is False


def test_chat_tool_run_issue_refuses_unmatched_url(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    ds.write_status("idle", status_path=status_path)
    loop_config_path = tmp_path / "projects.json"
    loop_config_path.write_text(json.dumps({
        "gitlab_instance": "acme",
        "projects": {"harbor": {"project_id": "acme/harbor/harbor"}},
    }))
    gitlab_config_path = tmp_path / "gitlab_config.json"
    gitlab_config_path.write_text(json.dumps({
        "instances": {"acme": {"url": "https://gitlab.acme.com"}},
    }))
    popen_called = {"value": False}

    def fake_popen(*args, **kwargs):
        popen_called["value"] = True
        raise AssertionError("Popen should not be called")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = ds._chat_tool_run_issue(
        "https://gitlab.acme.com/acme/some-other-project/-/issues/1",
        status_path=status_path, run_loop_path=tmp_path / "run-loop.sh",
        loop_config_path=loop_config_path, gitlab_config_path=gitlab_config_path,
    )

    assert result["ok"] is False
    assert "tracked project" in result["message"].lower()
    assert popen_called["value"] is False


def test_chat_tool_run_issue_refuses_when_script_missing(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    ds.write_status("idle", status_path=status_path)
    loop_config_path = tmp_path / "projects.json"
    loop_config_path.write_text(json.dumps({
        "gitlab_instance": "acme",
        "projects": {"harbor": {"project_id": "acme/harbor/harbor"}},
    }))
    gitlab_config_path = tmp_path / "gitlab_config.json"
    gitlab_config_path.write_text(json.dumps({
        "instances": {"acme": {"url": "https://gitlab.acme.com"}},
    }))
    popen_called = {"value": False}

    def fake_popen(*args, **kwargs):
        popen_called["value"] = True
        raise AssertionError("Popen should not be called")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = ds._chat_tool_run_issue(
        "https://gitlab.acme.com/acme/harbor/harbor/-/issues/482",
        status_path=status_path, run_loop_path=tmp_path / "does-not-exist.sh",
        loop_config_path=loop_config_path, gitlab_config_path=gitlab_config_path,
    )

    assert result["ok"] is False
    assert "not found" in result["message"]
    assert popen_called["value"] is False


def test_chat_tool_run_issue_launches_the_script(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    ds.write_status("idle", status_path=status_path)
    run_loop_path = tmp_path / "run-loop.sh"
    run_loop_path.write_text("#!/bin/bash\ntrue\n")
    run_loop_path.chmod(0o755)
    loop_config_path = tmp_path / "projects.json"
    loop_config_path.write_text(json.dumps({
        "gitlab_instance": "acme",
        "projects": {"harbor": {"project_id": "acme/harbor/harbor"}},
    }))
    gitlab_config_path = tmp_path / "gitlab_config.json"
    gitlab_config_path.write_text(json.dumps({
        "instances": {"acme": {"url": "https://gitlab.acme.com"}},
    }))

    captured = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    result = ds._chat_tool_run_issue(
        "https://gitlab.acme.com/acme/harbor/harbor/-/issues/482",
        status_path=status_path, run_loop_path=run_loop_path,
        loop_config_path=loop_config_path, gitlab_config_path=gitlab_config_path,
    )

    assert result == {"ok": True, "message": "Started work on harbor #482"}
    assert captured["args"] == ["bash", str(run_loop_path), "harbor", "482"]
    assert captured["kwargs"]["start_new_session"] is True


def test_dispatch_chat_tool_daemon_enable_without_filename_exits_1(capsys):
    with pytest.raises(SystemExit) as exc_info:
        ds._dispatch_chat_tool("daemon-enable", [])
    assert exc_info.value.code == 1


def test_dispatch_chat_tool_run_now_without_kind_exits_1(capsys):
    with pytest.raises(SystemExit) as exc_info:
        ds._dispatch_chat_tool("run-now", [])
    assert exc_info.value.code == 1


def test_dispatch_chat_tool_run_issue_without_url_exits_1(capsys):
    with pytest.raises(SystemExit) as exc_info:
        ds._dispatch_chat_tool("run-issue", [])
    assert exc_info.value.code == 1


def test_dispatch_chat_tool_run_issue_dispatches_to_chat_tool_run_issue(monkeypatch, capsys):
    captured = {}

    def fake_run_issue(url):
        captured["url"] = url
        return {"ok": True, "message": "Started work on harbor #482"}

    monkeypatch.setattr(ds, "_chat_tool_run_issue", fake_run_issue)
    ds._dispatch_chat_tool("run-issue", ["https://gitlab.acme.com/acme/harbor/harbor/-/issues/482"])

    assert captured["url"] == "https://gitlab.acme.com/acme/harbor/harbor/-/issues/482"
    assert "Started work on harbor #482" in capsys.readouterr().out


def test_chat_tool_history_delete(tmp_path):
    (tmp_path / "2026-08-20.md").write_text("content")
    result = ds._chat_tool_history_delete("2026-08-20.md", history_dir=tmp_path)
    assert result == {"ok": True, "message": "Deleted 2026-08-20.md"}
    assert not (tmp_path / "2026-08-20.md").exists()


def test_chat_tool_history_delete_missing_file(tmp_path):
    result = ds._chat_tool_history_delete("missing.md", history_dir=tmp_path)
    assert result == {"ok": False, "message": "missing.md not found"}


def test_dispatch_chat_tool_history_delete_is_not_reachable(capsys):
    """Fix 5 (scope change): history-delete is removed from the chat
    assistant's action surface entirely - GitLab issue content (attacker-
    influenceable) flows into the files chat-tool reads and into the
    assistant's own prompt, so a destructive action must not be reachable
    from chat. _chat_tool_history_delete itself is kept (and still tested
    directly above) for any future non-chat caller, but the CLI dispatcher
    must treat it exactly like any other unknown action now."""
    with pytest.raises(SystemExit) as exc_info:
        ds._dispatch_chat_tool("history-delete", ["2026-08-20.md"])
    assert exc_info.value.code == 1
    assert "Unknown chat-tool action" in capsys.readouterr().err


def test_chat_assistant_system_prompt_does_not_mention_history_delete():
    assert "history-delete" not in ds._CHAT_ASSISTANT_SYSTEM_PROMPT


def test_chat_assistant_system_prompt_documents_run_issue_action():
    assert "run-issue" in ds._CHAT_ASSISTANT_SYSTEM_PROMPT
    assert "GitLab issue link" in ds._CHAT_ASSISTANT_SYSTEM_PROMPT


def test_send_user_message_success(tmp_path):
    ok, message = ds.send_user_message("please hold off on brightleaf.web today", tmp_path / "messages.json")
    assert ok is True
    assert ds.read_messages(tmp_path / "messages.json")[0]["text"] == "please hold off on brightleaf.web today"


def test_send_user_message_blank_rejected(tmp_path):
    path = tmp_path / "messages.json"
    ok, message = ds.send_user_message("   ", path)
    assert ok is False
    assert ds.read_messages(path) == []


def test_delete_message_removes_matching_timestamp(tmp_path):
    path = tmp_path / "messages.json"
    ds.append_message("user", "first", path)
    ds.append_message("user", "second", path)
    target_timestamp = ds.read_messages(path)[0]["timestamp"]

    ok, message = ds.delete_message(target_timestamp, path)

    assert ok, message
    remaining = ds.read_messages(path)
    assert len(remaining) == 1
    assert remaining[0]["text"] == "second"


def test_delete_message_unknown_timestamp_rejected(tmp_path):
    path = tmp_path / "messages.json"
    ds.append_message("user", "only message", path)

    ok, message = ds.delete_message("2000-01-01T00:00:00+00:00", path)

    assert not ok
    assert "not found" in message.lower()
    assert len(ds.read_messages(path)) == 1


def test_trigger_manual_run_refuses_when_already_running(tmp_path):
    status_path = tmp_path / "status.json"
    ds.write_status("running", status_path=status_path)

    ok, message = ds.trigger_manual_run(status_path=status_path, run_loop_path=tmp_path / "run-loop.sh")

    assert not ok
    assert "already in progress" in message


def test_trigger_manual_run_refuses_when_script_missing(tmp_path):
    status_path = tmp_path / "status.json"
    ds.write_status("idle", status_path=status_path)

    ok, message = ds.trigger_manual_run(status_path=status_path, run_loop_path=tmp_path / "does-not-exist.sh")

    assert not ok
    assert "not found" in message


def test_trigger_manual_run_launches_the_script(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    ds.write_status("idle", status_path=status_path)
    run_loop_path = tmp_path / "run-loop.sh"
    run_loop_path.write_text("#!/bin/bash\ntrue\n")
    run_loop_path.chmod(0o755)

    captured = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    ok, message = ds.trigger_manual_run(status_path=status_path, run_loop_path=run_loop_path)

    assert ok, message
    assert captured["args"] == ["bash", str(run_loop_path)]
    assert captured["kwargs"]["start_new_session"] is True


def test_status_badge_markup_shows_progress_detail_while_running():
    status = {"state": "running", "current_issue": "brightleaf.web #1206", "current_step": "verifying"}

    output = ds._status_badge_markup(status)

    assert "Processing brightleaf.web #1206" in output
    assert "Running verification" in output
    assert "md-spinner" in output


def test_status_badge_markup_shows_plain_label_when_idle():
    output = ds._status_badge_markup({"state": "idle"})

    assert "Idle" in output
    assert "md-spinner" not in output


def test_render_shell_topbar_progress_bar_active_when_running(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    ds.write_status("running", status_path, current_issue="brightleaf.web #1206", current_step="verifying")
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "LOOP_DIR", tmp_path)
    (tmp_path / "outputs").mkdir(exist_ok=True)
    (tmp_path / "outputs" / "daily-review.md").write_text("All good.")

    output = ds.render_overview_page()

    assert "class=\"topbar-progress-bar is-active\"" in output


def test_render_shell_topbar_progress_bar_inactive_when_idle(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    ds.write_status("idle", status_path=status_path)
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "LOOP_DIR", tmp_path)
    (tmp_path / "outputs").mkdir(exist_ok=True)
    (tmp_path / "outputs" / "daily-review.md").write_text("All good.")

    output = ds.render_overview_page()

    assert "class=\"topbar-progress-bar\"" in output
    assert "topbar-progress-bar is-active" not in output


def test_render_overview_page_renders_message_thread_in_order(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    messages_path = tmp_path / "messages.json"
    ds.append_message("user", "please hold off on brightleaf.web today", messages_path)
    ds.append_message("loop", "understood, skipping it this run", messages_path)
    monkeypatch.setattr(ds, "MESSAGES_PATH", messages_path)

    output = ds.render_overview_page()

    assert output.index("please hold off on brightleaf.web today") < output.index("understood, skipping it this run")
    assert "You" in output
    assert "Loop" in output


def test_render_overview_page_message_row_has_separate_meta_and_text_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    messages_path = tmp_path / "messages.json"
    ds.append_message("user", "please hold off on brightleaf.web today", messages_path)
    monkeypatch.setattr(ds, "MESSAGES_PATH", messages_path)

    output = ds.render_overview_page()

    assert "<br>" not in output
    assert "<div class='message-meta'>" in output
    assert "please hold off on brightleaf.web today" in output
    assert "class='message-bubble message-bubble-user'" in output
    assert "class='message-row message-row-user'" in output


def test_render_overview_page_shows_relative_time_and_renders_markdown(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    messages_path = tmp_path / "messages.json"
    ds.append_message("loop", "ran `bundle exec rspec` and it passed", messages_path)
    monkeypatch.setattr(ds, "MESSAGES_PATH", messages_path)

    output = ds.render_overview_page()

    assert "<code>bundle exec rspec</code>" in output
    assert "class='message-time'>just now<" in output
    assert "class='message-bubble message-bubble-loop'" in output


def test_render_overview_page_message_has_delete_form(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    messages_path = tmp_path / "messages.json"
    ds.append_message("user", "hello", messages_path)
    timestamp = ds.read_messages(messages_path)[0]["timestamp"]
    monkeypatch.setattr(ds, "MESSAGES_PATH", messages_path)

    output = ds.render_overview_page()

    expected_action = f"/activity/messages/{urllib.parse.quote(timestamp, safe='')}/delete"
    assert f"action='{expected_action}'" in output


def test_render_overview_page_empty_thread_shows_placeholder(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds, "MESSAGES_PATH", tmp_path / "does-not-exist-messages.json")

    output = ds.render_overview_page()

    assert "(no messages yet)" in output


def test_render_overview_page_does_not_auto_refresh(monkeypatch, tmp_path):
    """Only Live GitLab, Topic Monitor, and Activity auto-refresh - the
    Overview page is mostly a user-edited message thread and shouldn't
    silently reload out from under someone reading or composing it."""
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds, "MESSAGES_PATH", tmp_path / "does-not-exist-messages.json")

    output = ds.render_overview_page()

    assert "auto-refreshes every 30s" not in output
    assert "location.reload();" not in output


def test_render_overview_page_flash_success(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds, "MESSAGES_PATH", tmp_path / "does-not-exist-messages.json")

    output = ds.render_overview_page(flash="Message sent", flash_ok=True)

    assert "<div class='flash flash-success'>Message sent</div>" in output


def test_render_overview_page_composer_form_has_js_hook_id(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(ds, "MESSAGES_PATH", tmp_path / "messages.json")
    page = ds.render_overview_page()
    assert "id='activity-composer-form'" in page


def test_render_overview_page_message_list_has_js_hook_id(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    messages_path = tmp_path / "messages.json"
    messages_path.write_text(json.dumps([
        {"from": "loop", "text": "hi", "timestamp": "2026-08-23T00:00:00+00:00"},
    ]))
    monkeypatch.setattr(ds, "MESSAGES_PATH", messages_path)
    page = ds.render_overview_page()
    assert "id='activity-message-list'" in page


def test_render_overview_page_shows_brand_icon_not_loop_text_for_loop_messages(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    messages_path = tmp_path / "messages.json"
    messages_path.write_text(json.dumps([
        {"from": "loop", "text": "hi", "timestamp": "2026-08-23T00:00:00+00:00"},
    ]))
    monkeypatch.setattr(ds, "MESSAGES_PATH", messages_path)
    page = ds.render_overview_page()
    thread_html = page[page.index("id='activity-message-list'"):]
    assert "aria-label='Loop X'" in thread_html
    assert ">Loop</span>" not in thread_html  # the old bare-text label is gone
    assert "message-brand-icon" in thread_html


def test_render_overview_page_user_messages_still_say_you(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    messages_path = tmp_path / "messages.json"
    messages_path.write_text(json.dumps([
        {"from": "user", "text": "hi", "timestamp": "2026-08-23T00:00:00+00:00", "seen_by_loop": True},
    ]))
    monkeypatch.setattr(ds, "MESSAGES_PATH", messages_path)
    page = ds.render_overview_page()
    assert "<span class='k'>You</span>" in page


def test_render_overview_page_shows_stats_section(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds, "MESSAGES_PATH", tmp_path / "does-not-exist-messages.json")
    monkeypatch.setattr(ds, "HISTORY_DIR", tmp_path / "does-not-exist-history")
    monkeypatch.setattr(ds, "read_loop_projects_config", lambda *a, **k: {"projects": {"demo": {}}})
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [{"name": "ai-news"}])

    page = ds.render_overview_page()

    assert "dash-stats-grid" in page
    assert "<span class='dash-stat-value'>1</span>" in page  # tracked projects
    assert "Tracked projects" in page
    assert "Configured topics" in page
    assert "activity-strip" in page


def test_render_overview_page_renames_thread_heading_to_conversation(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds, "MESSAGES_PATH", tmp_path / "does-not-exist-messages.json")
    monkeypatch.setattr(ds, "HISTORY_DIR", tmp_path / "does-not-exist-history")
    monkeypatch.setattr(ds, "read_loop_projects_config", lambda *a, **k: {})
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [])

    page = ds.render_overview_page()

    assert "<h2>Conversation</h2>" in page


def test_render_overview_page_inserts_day_separator_between_different_days(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    messages_path = tmp_path / "messages.json"
    messages_path.write_text(json.dumps([
        {"from": "user", "text": "message from yesterday", "timestamp": "2026-08-22T12:00:00+00:00"},
        {"from": "loop", "text": "reply from today", "timestamp": "2026-08-23T12:00:00+00:00"},
    ]))
    monkeypatch.setattr(ds, "MESSAGES_PATH", messages_path)
    monkeypatch.setattr(ds, "HISTORY_DIR", tmp_path / "does-not-exist-history")
    monkeypatch.setattr(ds, "read_loop_projects_config", lambda *a, **k: {})
    monkeypatch.setattr(ds, "get_configured_topics", lambda *a, **k: [])

    page = ds.render_overview_page()

    assert page.count("class='message-day-sep'") == 2  # one separator per distinct day
    first_sep = page.index("class='message-day-sep'")
    second_sep = page.index("class='message-day-sep'", first_sep + 1)
    assert first_sep < page.index("message from yesterday") < second_sep < page.index("reply from today")


def test_dashboard_server_integration_activity_route(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds, "MESSAGES_PATH", tmp_path / "does-not-exist-messages.json")

    with _running_server() as port:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/activity", timeout=10) as response:
            assert response.status == 200
            body = response.read().decode("utf-8")
            assert "Activity" in body
            assert "<a href='/activity' title='Activity' class='active'>" in body


def test_activity_route_send_message_requires_csrf(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds, "MESSAGES_PATH", tmp_path / "messages.json")

    with _running_server() as port:
        status, _headers, _body = _post(port, "/activity/messages", {"text": "hello", "csrf_token": ""})
        assert status == 403
    assert ds.read_messages(tmp_path / "messages.json") == []


def test_activity_route_send_message_success(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    messages_path = tmp_path / "messages.json"
    monkeypatch.setattr(ds, "MESSAGES_PATH", messages_path)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/")
        status, headers, _body = _post(port, "/activity/messages", {"text": "please hold off on brightleaf.web today", "csrf_token": token})
        assert status == 303
        flash_query = _flash_from_location(headers["Location"], prefix="/?")
        assert flash_query["ok"] == ["1"]
    assert ds.read_messages(messages_path)[0]["text"] == "please hold off on brightleaf.web today"


def test_activity_route_send_blank_message_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    messages_path = tmp_path / "messages.json"
    monkeypatch.setattr(ds, "MESSAGES_PATH", messages_path)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/")
        status, headers, _body = _post(port, "/activity/messages", {"text": "", "csrf_token": token})
        assert status == 303
        flash_query = _flash_from_location(headers["Location"], prefix="/?")
        assert flash_query["ok"] == ["0"]
    assert ds.read_messages(messages_path) == []


def test_activity_route_delete_message_success(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    messages_path = tmp_path / "messages.json"
    ds.append_message("user", "delete me", messages_path)
    timestamp = ds.read_messages(messages_path)[0]["timestamp"]
    monkeypatch.setattr(ds, "MESSAGES_PATH", messages_path)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/")
        status, headers, _body = _post(
            port, f"/activity/messages/{urllib.parse.quote(timestamp, safe='')}/delete",
            {"csrf_token": token},
        )
        assert status == 303
        flash_query = _flash_from_location(headers["Location"], prefix="/?")
        assert flash_query["ok"] == ["1"]
    assert ds.read_messages(messages_path) == []


def test_run_now_route_launches_when_idle(monkeypatch, tmp_path):
    status_path = tmp_path / "status.json"
    ds.write_status("idle", status_path=status_path)
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "RUN_LOOP_SH", tmp_path / "run-loop.sh")
    (tmp_path / "run-loop.sh").write_text("#!/bin/bash\ntrue\n")

    captured = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    with _running_server() as port:
        # "/" itself may not render a csrf_token input at all now - its Run
        # now form is disabled (no <form>, hence no token) when no GitLab
        # projects are configured, which this test doesn't set up - so
        # fetch it from a page that always renders one, same reasoning as
        # test_run_now_route_refuses_when_already_running below.
        token = _fetch_csrf_token(port, "/daemons")
        status, headers, _body = _post(port, "/run-now", {"csrf_token": token})
        assert status == 303
        flash_query = _flash_from_location(headers["Location"], prefix="/?")
        assert flash_query["ok"] == ["1"]
    assert captured["args"] == ["bash", str(tmp_path / "run-loop.sh")]


def test_run_now_route_refuses_when_already_running(monkeypatch, tmp_path):
    status_path = tmp_path / "status.json"
    ds.write_status("running", status_path=status_path)
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)

    with _running_server() as port:
        # The Run now form (and its csrf_token input) is hidden on "/" while
        # a run is in progress, so fetch the token from a page that always
        # renders one - the token itself is a per-process global, not tied
        # to which page rendered it.
        token = _fetch_csrf_token(port, "/daemons")
        status, headers, _body = _post(port, "/run-now", {"csrf_token": token})
        assert status == 303
        flash_query = _flash_from_location(headers["Location"], prefix="/?")
        assert flash_query["ok"] == ["0"]


def test_run_now_route_requires_csrf(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    ds.write_status("idle", status_path=status_path)
    monkeypatch.setattr(ds, "STATUS_PATH", status_path)

    with _running_server() as port:
        status, _headers, _body = _post(port, "/run-now", {"csrf_token": ""})
        assert status == 403


def test_skills_install_route_launches_when_idle(monkeypatch, tmp_path):
    status_path = tmp_path / "skills_install_status.json"
    setup_script_path = tmp_path / "setup.sh"
    setup_script_path.write_text("#!/bin/bash\ntrue\n")
    monkeypatch.setattr(ds, "SKILLS_INSTALL_STATUS_PATH", status_path)
    monkeypatch.setattr(ds, "SETUP_SH", setup_script_path)

    captured = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/daemons")
        status, headers, _body = _post(port, "/skills/install", {"csrf_token": token})
        assert status == 303
        flash_query = _flash_from_location(headers["Location"], prefix="/skills?")
        assert flash_query["ok"] == ["1"]
    assert str(setup_script_path) in captured["args"][2]


def test_skills_install_route_refuses_when_already_installing(monkeypatch, tmp_path):
    status_path = tmp_path / "skills_install_status.json"
    ds.write_status("installing", status_path=status_path)
    monkeypatch.setattr(ds, "SKILLS_INSTALL_STATUS_PATH", status_path)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/daemons")
        status, headers, _body = _post(port, "/skills/install", {"csrf_token": token})
        assert status == 303
        flash_query = _flash_from_location(headers["Location"], prefix="/skills?")
        assert flash_query["ok"] == ["0"]


def test_skills_install_route_requires_csrf(tmp_path, monkeypatch):
    status_path = tmp_path / "skills_install_status.json"
    monkeypatch.setattr(ds, "SKILLS_INSTALL_STATUS_PATH", status_path)

    with _running_server() as port:
        status, _headers, _body = _post(port, "/skills/install", {"csrf_token": ""})
        assert status == 403


def test_custom_select_with_empty_label_adds_leading_blank_option():
    output = ds._custom_select("bundle", ["vertex-limited"], "", empty_label="(use instance default)")

    # selected="" matches the empty_label pseudo-option's value, so it carries " selected"
    assert "<option value='' selected>(use instance default)</option>" in output
    assert "<span class='custom-select-value'>(use instance default)</span>" in output


def test_custom_select_empty_label_none_keeps_old_behavior():
    output = ds._custom_select("instance", ["acme", "vertex"], "vertex")

    assert "(use instance default)" not in output
    assert "<span class='custom-select-value'>vertex</span>" in output


def test_upsert_gitlab_project_accepts_valid_bundle(tmp_path):
    path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({
        "instances": {"acme": {"url": "https://gitlab.acme.com", "token": "tok"}},
        "bundles": {"vertex-limited": {"instance": "acme", "token": "bundle-tok"}},
        "projects": {},
    }, path)

    ok, message = ds.upsert_gitlab_project("vertex", "acme/vertex-app/vertex-app.web", "acme", "vertex-limited", path)

    assert ok, message
    assert ds.read_gitlab_config(path)["projects"]["vertex"]["bundle"] == "vertex-limited"


def test_upsert_gitlab_project_rejects_unknown_bundle(tmp_path):
    path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({
        "instances": {"acme": {"url": "https://gitlab.acme.com", "token": "tok"}},
        "projects": {},
    }, path)

    ok, message = ds.upsert_gitlab_project("vertex", "acme/vertex-app/vertex-app.web", "acme", "no-such-bundle", path)

    assert not ok
    assert "Unknown bundle" in message


def test_upsert_gitlab_project_rejects_bundle_for_wrong_instance(tmp_path):
    path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({
        "instances": {
            "acme": {"url": "https://gitlab.acme.com", "token": "tok"},
            "vertex": {"url": "https://gitlab.vertex.example", "token": "tok2"},
        },
        "bundles": {"vertex-limited": {"instance": "vertex", "token": "bundle-tok"}},
        "projects": {},
    }, path)

    ok, message = ds.upsert_gitlab_project("vertex", "acme/vertex-app/vertex-app.web", "acme", "vertex-limited", path)

    assert not ok
    assert "vertex-limited" in message and "vertex" in message


def test_upsert_gitlab_project_blank_bundle_clears_existing(tmp_path):
    path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({
        "instances": {"acme": {"url": "https://gitlab.acme.com", "token": "tok"}},
        "bundles": {"vertex-limited": {"instance": "acme", "token": "bundle-tok"}},
        "projects": {"vertex": {"project_id": "acme/vertex-app/vertex-app.web", "instance": "acme", "bundle": "vertex-limited"}},
    }, path)

    ok, message = ds.upsert_gitlab_project("vertex", "acme/vertex-app/vertex-app.web", "acme", "", path)

    assert ok, message
    assert "bundle" not in ds.read_gitlab_config(path)["projects"]["vertex"]


def test_upsert_access_bundle_creates_new_bundle_and_webhook(tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    slack_path = tmp_path / "slack.json"
    ds.write_gitlab_config({"instances": {"acme": {"url": "https://gitlab.acme.com", "token": "tok"}}}, gitlab_path)
    ds.write_slack_config({"webhook_url": "https://hooks.slack.com/services/DEFAULT"}, slack_path)

    ok, message = ds.upsert_access_bundle(
        "vertex-limited", "acme", "bundle-tok", "https://hooks.slack.com/services/VERTEX",
        gitlab_path, slack_path,
    )

    assert ok, message
    assert ds.read_gitlab_config(gitlab_path)["bundles"]["vertex-limited"] == {"instance": "acme", "token": "bundle-tok"}
    assert ds.read_slack_config(slack_path)["bundle_webhooks"]["vertex-limited"] == "https://hooks.slack.com/services/VERTEX"


def test_upsert_access_bundle_requires_token_for_new_bundle(tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({"instances": {"acme": {"url": "https://gitlab.acme.com", "token": "tok"}}}, gitlab_path)

    ok, message = ds.upsert_access_bundle("vertex-limited", "acme", "", "", gitlab_path, tmp_path / "slack.json")

    assert not ok
    assert "Token is required" in message


def test_upsert_access_bundle_rejects_unknown_instance(tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({"instances": {}}, gitlab_path)

    ok, message = ds.upsert_access_bundle("vertex-limited", "no-such-instance", "tok", "", gitlab_path, tmp_path / "slack.json")

    assert not ok
    assert "Unknown instance" in message


def test_upsert_access_bundle_blank_token_keeps_existing_on_edit(tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({
        "instances": {"acme": {"url": "https://gitlab.acme.com", "token": "tok"}},
        "bundles": {"vertex-limited": {"instance": "acme", "token": "original-tok"}},
    }, gitlab_path)

    ok, message = ds.upsert_access_bundle("vertex-limited", "acme", "", "", gitlab_path, tmp_path / "slack.json")

    assert ok, message
    assert ds.read_gitlab_config(gitlab_path)["bundles"]["vertex-limited"]["token"] == "original-tok"


def test_upsert_access_bundle_blank_webhook_leaves_existing_override_untouched(tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    slack_path = tmp_path / "slack.json"
    ds.write_gitlab_config({
        "instances": {"acme": {"url": "https://gitlab.acme.com", "token": "tok"}},
        "bundles": {"vertex-limited": {"instance": "acme", "token": "tok"}},
    }, gitlab_path)
    ds.write_slack_config({
        "webhook_url": "https://hooks.slack.com/services/DEFAULT",
        "bundle_webhooks": {"vertex-limited": "https://hooks.slack.com/services/VERTEX"},
    }, slack_path)

    ok, message = ds.upsert_access_bundle("vertex-limited", "acme", "new-tok", "", gitlab_path, slack_path)

    assert ok, message
    assert ds.read_slack_config(slack_path)["bundle_webhooks"]["vertex-limited"] == "https://hooks.slack.com/services/VERTEX"


def test_upsert_access_bundle_rejects_instance_change_when_referenced_by_a_project(tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({
        "instances": {
            "acme": {"url": "https://gitlab.acme.com", "token": "tok"},
            "other": {"url": "https://gitlab.other.com", "token": "tok2"},
        },
        "bundles": {"vertex-limited": {"instance": "acme", "token": "bundle-tok"}},
        "projects": {"vertex": {"project_id": "x", "instance": "acme", "bundle": "vertex-limited"}},
    }, gitlab_path)

    ok, message = ds.upsert_access_bundle("vertex-limited", "other", "", "", gitlab_path, tmp_path / "slack.json")

    assert not ok
    assert "vertex" in message
    assert ds.read_gitlab_config(gitlab_path)["bundles"]["vertex-limited"]["instance"] == "acme"


def test_upsert_access_bundle_allows_edit_when_instance_unchanged(tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({
        "instances": {"acme": {"url": "https://gitlab.acme.com", "token": "tok"}},
        "bundles": {"vertex-limited": {"instance": "acme", "token": "bundle-tok"}},
        "projects": {"vertex": {"project_id": "x", "instance": "acme", "bundle": "vertex-limited"}},
    }, gitlab_path)

    ok, message = ds.upsert_access_bundle("vertex-limited", "acme", "new-tok", "", gitlab_path, tmp_path / "slack.json")

    assert ok, message
    assert ds.read_gitlab_config(gitlab_path)["bundles"]["vertex-limited"]["token"] == "new-tok"


def test_upsert_access_bundle_allows_instance_change_when_unreferenced(tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({
        "instances": {
            "acme": {"url": "https://gitlab.acme.com", "token": "tok"},
            "other": {"url": "https://gitlab.other.com", "token": "tok2"},
        },
        "bundles": {"vertex-limited": {"instance": "acme", "token": "bundle-tok"}},
        "projects": {},
    }, gitlab_path)

    ok, message = ds.upsert_access_bundle("vertex-limited", "other", "", "", gitlab_path, tmp_path / "slack.json")

    assert ok, message
    assert ds.read_gitlab_config(gitlab_path)["bundles"]["vertex-limited"]["instance"] == "other"


def test_delete_access_bundle_removes_from_both_files(tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    slack_path = tmp_path / "slack.json"
    ds.write_gitlab_config({"bundles": {"vertex-limited": {"instance": "acme", "token": "tok"}}, "projects": {}}, gitlab_path)
    ds.write_slack_config({"bundle_webhooks": {"vertex-limited": "https://hooks.slack.com/services/VERTEX"}}, slack_path)

    ok, message = ds.delete_access_bundle("vertex-limited", gitlab_path, slack_path)

    assert ok, message
    assert "vertex-limited" not in ds.read_gitlab_config(gitlab_path).get("bundles", {})
    assert "vertex-limited" not in ds.read_slack_config(slack_path).get("bundle_webhooks", {})


def test_delete_access_bundle_rejects_when_referenced_by_a_project(tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    ds.write_gitlab_config({
        "bundles": {"vertex-limited": {"instance": "acme", "token": "tok"}},
        "projects": {"vertex": {"project_id": "x", "instance": "acme", "bundle": "vertex-limited"}},
    }, gitlab_path)

    ok, message = ds.delete_access_bundle("vertex-limited", gitlab_path, tmp_path / "slack.json")

    assert not ok
    assert "vertex" in message


def test_clear_bundle_webhook_removes_override(tmp_path):
    slack_path = tmp_path / "slack.json"
    ds.write_slack_config({"bundle_webhooks": {"vertex-limited": "https://hooks.slack.com/services/VERTEX"}}, slack_path)

    ok, message = ds.clear_bundle_webhook("vertex-limited", slack_path)

    assert ok, message
    assert "vertex-limited" not in ds.read_slack_config(slack_path).get("bundle_webhooks", {})


def test_clear_bundle_webhook_no_op_when_not_set(tmp_path):
    slack_path = tmp_path / "slack.json"
    ds.write_slack_config({}, slack_path)

    ok, message = ds.clear_bundle_webhook("vertex-limited", slack_path)

    assert not ok
    assert "No Slack webhook override" in message


def test_render_settings_page_shows_access_bundles_section(monkeypatch, tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    slack_path = tmp_path / "slack.json"
    gitlab_path.write_text(json.dumps({
        "instances": {"acme": {"url": "https://gitlab.acme.com", "token": "tok"}},
        "bundles": {"vertex-limited": {"instance": "acme", "token": "bundle-secret-1234"}},
        "projects": {"vertex": {"project_id": "acme/vertex-app/vertex-app.web", "instance": "acme", "bundle": "vertex-limited"}},
    }))
    slack_path.write_text(json.dumps({
        "webhook_url": "https://hooks.slack.com/services/DEFAULT",
        "bundle_webhooks": {"vertex-limited": "https://hooks.slack.com/services/VERTEX-9999"},
    }))
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", gitlab_path)
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", slack_path)

    output = ds.render_settings_page()

    assert "Access bundles" in output
    assert "vertex-limited" in output
    assert "bundle-secret-1234" not in output
    assert "••••1234" in output
    assert "https://hooks.slack.com/services/VERTEX-9999" not in output
    assert "••••9999" in output
    # the project row's Bundle select shows the current bundle
    assert "data-value='vertex-limited'" in output


def test_render_settings_page_access_bundle_inputs_have_distinct_placeholders(monkeypatch, tmp_path):
    """The add-bundle form's token field just said "required" (not what
    kind of token), and the edit-row form had the *same* "leave blank to
    keep current" placeholder on both its token and webhook fields -
    impossible to tell which was which at a glance."""
    gitlab_path = tmp_path / "gitlab.json"
    gitlab_path.write_text(json.dumps({
        "instances": {"acme": {"url": "https://gitlab.acme.com", "token": "tok"}},
        "bundles": {"vertex-limited": {"instance": "acme", "token": "bundle-secret-1234"}},
    }))
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", gitlab_path)
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "does-not-exist-slack.json")

    output = ds.render_settings_page()

    add_bundle_form = output.split("action='/settings/access-bundles'")[-1].split("</form>")[0]
    assert "placeholder='GitLab access token'" in add_bundle_form
    assert "placeholder='Slack webhook URL (optional)'" in add_bundle_form

    edit_bundle_form = output.split("action='/settings/access-bundles'")[1].split("</form>")[0]
    assert "placeholder='leave blank to keep current token'" in edit_bundle_form
    assert "placeholder='leave blank to keep current webhook'" in edit_bundle_form


def test_render_settings_page_no_bundles_shows_placeholder(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", tmp_path / "does-not-exist.json")
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "does-not-exist-slack.json")

    output = ds.render_settings_page()

    assert "(no access bundles configured)" in output


def test_settings_route_add_access_bundle_success(monkeypatch, tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    slack_path = tmp_path / "slack.json"
    gitlab_path.write_text(json.dumps({"instances": {"acme": {"url": "https://gitlab.acme.com", "token": "tok"}}}))
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", gitlab_path)
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", slack_path)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/settings")
        status, _headers, _body = _post(port, "/settings/access-bundles", {
            "name": "vertex-limited", "instance": "acme", "token": "bundle-tok",
            "webhook_url": "https://hooks.slack.com/services/VERTEX", "csrf_token": token,
        })
        assert status == 303

    assert ds.read_gitlab_config(gitlab_path)["bundles"]["vertex-limited"]["token"] == "bundle-tok"
    assert ds.read_slack_config(slack_path)["bundle_webhooks"]["vertex-limited"] == "https://hooks.slack.com/services/VERTEX"


def test_settings_route_delete_access_bundle_success(monkeypatch, tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    gitlab_path.write_text(json.dumps({"bundles": {"vertex-limited": {"instance": "acme", "token": "tok"}}, "projects": {}}))
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", gitlab_path)
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "slack.json")

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/settings")
        status, _headers, _body = _post(port, "/settings/access-bundles/vertex-limited/delete", {"csrf_token": token})
        assert status == 303

    assert "vertex-limited" not in ds.read_gitlab_config(gitlab_path).get("bundles", {})


def test_settings_route_clear_bundle_webhook_success(monkeypatch, tmp_path):
    slack_path = tmp_path / "slack.json"
    slack_path.write_text(json.dumps({"bundle_webhooks": {"vertex-limited": "https://hooks.slack.com/services/VERTEX"}}))
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", tmp_path / "gitlab.json")
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", slack_path)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/settings")
        status, _headers, _body = _post(port, "/settings/access-bundles/vertex-limited/clear-webhook", {"csrf_token": token})
        assert status == 303

    assert "vertex-limited" not in ds.read_slack_config(slack_path).get("bundle_webhooks", {})


def test_settings_route_update_project_with_bundle(monkeypatch, tmp_path):
    gitlab_path = tmp_path / "gitlab.json"
    gitlab_path.write_text(json.dumps({
        "instances": {"acme": {"url": "https://gitlab.acme.com", "token": "tok"}},
        "bundles": {"vertex-limited": {"instance": "acme", "token": "bundle-tok"}},
        "projects": {"vertex": {"project_id": "acme/vertex-app/vertex-app.web", "instance": "acme"}},
    }))
    monkeypatch.setattr(ds, "GITLAB_CONFIG_PATH", gitlab_path)
    monkeypatch.setattr(ds, "SLACK_CONFIG_PATH", tmp_path / "slack.json")

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/settings")
        status, _headers, _body = _post(port, "/settings/gitlab/projects", {
            "alias": "vertex", "project_id": "acme/vertex-app/vertex-app.web", "instance": "acme",
            "bundle": "vertex-limited", "csrf_token": token,
        })
        assert status == 303

    assert ds.read_gitlab_config(gitlab_path)["projects"]["vertex"]["bundle"] == "vertex-limited"


# --- In-memory chat job registry (Activity page live chat assistant) ---


def test_chat_job_create_returns_unique_keys():
    key1 = ds._chat_job_create()
    key2 = ds._chat_job_create()
    assert key1 != key2
    assert isinstance(key1, str) and key1


def test_chat_job_append_then_iterate_yields_chunks_then_done():
    key = ds._chat_job_create()
    ds._chat_job_append(key, "hello")
    ds._chat_job_append(key, " world")
    ds._chat_job_finish(key, final_text="hello world")
    events = list(ds._iter_chat_job_chunks(key))
    assert events == [("chunk", "hello"), ("chunk", " world"), ("done", None, "hello world")]


def test_chat_job_finish_with_error_is_reported_in_done_event():
    key = ds._chat_job_create()
    ds._chat_job_finish(key, error="something broke")
    events = list(ds._iter_chat_job_chunks(key))
    assert events == [("done", "something broke", None)]


def test_iter_chat_job_chunks_unknown_key_yields_nothing():
    events = list(ds._iter_chat_job_chunks("not-a-real-key"))
    assert events == []


def test_chat_job_append_after_finish_is_a_noop_not_a_crash():
    key = ds._chat_job_create()
    ds._chat_job_finish(key)
    ds._chat_job_append(key, "too late")  # must not raise


def test_chat_job_streams_live_across_threads():
    """Simulates the real usage: a background thread appends chunks with
    a small delay while the main thread iterates - proves the generator
    actually blocks and wakes rather than only working when everything
    is written before iteration starts."""
    key = ds._chat_job_create()

    def producer():
        time.sleep(0.05)
        ds._chat_job_append(key, "first")
        time.sleep(0.05)
        ds._chat_job_append(key, "second")
        ds._chat_job_finish(key)

    t = threading.Thread(target=producer)
    t.start()
    events = list(ds._iter_chat_job_chunks(key))
    t.join()
    assert events == [("chunk", "first"), ("chunk", "second"), ("done", None, None)]


def test_iter_chat_job_chunks_emits_keepalive_idle_tuple_before_real_data():
    """Fix 4: the SSE route (_stream_chat_reply) needs a way to tell "the
    job is still alive, just nothing new yet" apart from "here's a real
    chunk" so it can write a keepalive comment line during a slow-to-start
    reply. idle_timeout is shrunk way down here (instead of waiting out
    the real ~15s default) so this stays a fast test."""
    key = ds._chat_job_create()

    def producer():
        time.sleep(0.15)
        ds._chat_job_append(key, "finally")
        ds._chat_job_finish(key, final_text="finally")

    t = threading.Thread(target=producer)
    t.start()
    events = list(ds._iter_chat_job_chunks(key, idle_timeout=0.02))
    t.join()
    assert ("idle", None) in events
    assert events[-2:] == [("chunk", "finally"), ("done", None, "finally")]


# --- Stream JSON line parser (Activity page live chat assistant) ---


def test_parse_chat_stream_line_blank_returns_none():
    assert ds.parse_chat_stream_line("") is None
    assert ds.parse_chat_stream_line("   \n") is None


def test_parse_chat_stream_line_invalid_json_returns_none():
    assert ds.parse_chat_stream_line("not json at all") is None


def test_parse_chat_stream_line_ignores_non_delta_events():
    system_init = '{"type":"system","subtype":"init","cwd":"/x","session_id":"abc"}'
    assert ds.parse_chat_stream_line(system_init) is None
    rate_limit = '{"type":"rate_limit_event","rate_limit_info":{}}'
    assert ds.parse_chat_stream_line(rate_limit) is None
    message_start = (
        '{"type":"stream_event","event":{"type":"message_start",'
        '"message":{"role":"assistant"}}}'
    )
    assert ds.parse_chat_stream_line(message_start) is None
    content_block_start = (
        '{"type":"stream_event","event":{"type":"content_block_start",'
        '"index":0,"content_block":{"type":"text","text":""}}}'
    )
    assert ds.parse_chat_stream_line(content_block_start) is None


def test_parse_chat_stream_line_extracts_text_delta():
    line = (
        '{"type":"stream_event","event":{"type":"content_block_delta",'
        '"index":0,"delta":{"type":"text_delta","text":"hello there"}}}'
    )
    assert ds.parse_chat_stream_line(line) == ("delta", "hello there")


def test_parse_chat_stream_line_extracts_successful_result():
    line = (
        '{"is_error":false,"result":"hello there friend","type":"result",'
        '"subtype":"success"}'
    )
    assert ds.parse_chat_stream_line(line) == ("result", "hello there friend", False)


def test_parse_chat_stream_line_extracts_failed_result():
    line = '{"is_error":true,"result":"Not logged in","type":"result"}'
    assert ds.parse_chat_stream_line(line) == ("result", "Not logged in", True)


def test_parse_chat_stream_line_result_with_no_text_defaults_to_empty_string():
    line = '{"is_error":false,"type":"result"}'
    assert ds.parse_chat_stream_line(line) == ("result", "", False)


def test_build_chat_prompt_with_no_history_returns_bare_text():
    assert ds.build_chat_prompt("what's the status?", []) == "what's the status?"


def test_build_chat_prompt_includes_recent_conversation():
    recent = [
        {"from": "user", "text": "pause the topic monitor"},
        {"from": "loop", "text": "Done - topic monitor paused."},
    ]
    prompt = ds.build_chat_prompt("now resume it", recent)
    assert "Recent conversation:" in prompt
    assert "User: pause the topic monitor" in prompt
    assert "Assistant: Done - topic monitor paused." in prompt
    assert "New message: now resume it" in prompt


def test_chat_assistant_system_prompt_names_the_only_allowed_command():
    assert "chat-tool" in ds._CHAT_ASSISTANT_SYSTEM_PROMPT
    assert str(ds.LOOP_DIR) in ds._CHAT_ASSISTANT_SYSTEM_PROMPT


def test_build_chat_command_wraps_in_login_shell_with_timeout():
    argv = ds.build_chat_command("hello")
    assert argv[:3] == ["zsh", "-i", "-l"]
    assert argv[3] == "-c"
    command = argv[4]
    assert command.startswith("timeout 90 ")
    assert "claude" in command
    assert "--output-format" in command and "stream-json" in command
    assert "--include-partial-messages" in command
    assert "--safe-mode" in command
    assert "--allowedTools" in command
    assert "chat-tool" in command
    assert "--disallowedTools" in command


def test_build_chat_command_has_no_permission_mode():
    """Fix 7.1: a prior version passed --permission-mode acceptEdits, which
    this assistant has no legitimate editing role to justify - the safety
    story should rest on --allowedTools/--disallowedTools alone, not also
    on a permission mode that implies auto-approval of anything."""
    argv = ds.build_chat_command("hello")
    command = argv[4]
    assert "--permission-mode" not in command
    assert "acceptEdits" not in command


def test_build_chat_command_disallowed_tools_widened():
    """Fix 7.2: Grep/Glob read file content just as much as Read does (an
    oversight in the original list), and curl/sh/bash/zsh/python3 -c/nc/
    osascript/launchctl are disallowed as the same defense-in-depth the
    existing git*/rm* entries already use."""
    argv = ds.build_chat_command("hello")
    command = argv[4]
    for expected in (
        "Grep", "Glob",
        "Bash(curl*)", "Bash(sh*)", "Bash(bash*)", "Bash(zsh*)",
        "Bash(python3 -c*)", "Bash(nc*)", "Bash(osascript*)", "Bash(launchctl*)",
    ):
        assert expected in command, f"expected {expected!r} in disallowedTools"


def test_build_chat_command_shell_quotes_the_prompt_safely():
    argv = ds.build_chat_command("say `rm -rf /` and 'quote' this")
    command = argv[4]
    # shlex.join must have quoted the hostile text so a shell parses it
    # as one literal argument, not a nested command substitution.
    import shlex
    parsed = shlex.split(command)
    assert "say `rm -rf /` and 'quote' this" in parsed


class _FakeChatPopenProcess:
    def __init__(self, lines):
        self.stdout = iter(lines)

    def wait(self):
        return 0


def test_run_chat_job_streams_deltas_and_saves_final_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "UNIFIED_LOG_PATH", tmp_path / "logs" / "loop-engineering.log")
    messages_path = tmp_path / "messages.json"
    messages_path.write_text("[]")
    lines = [
        '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hel"}}}\n',
        '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"lo"}}}\n',
        '{"is_error":false,"result":"hello","type":"result"}\n',
    ]
    monkeypatch.setattr(ds.subprocess, "Popen", lambda *a, **k: _FakeChatPopenProcess(lines))
    key = ds._chat_job_create()
    ds._run_chat_job(key, "hi", messages_path=messages_path)
    events = list(ds._iter_chat_job_chunks(key))
    assert events == [("chunk", "hel"), ("chunk", "lo"), ("done", None, "hello")]
    saved = json.loads(messages_path.read_text())
    assert saved[-1]["from"] == "loop"
    assert saved[-1]["text"] == "hello"


def test_run_chat_job_failed_result_does_not_save_a_message(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "UNIFIED_LOG_PATH", tmp_path / "logs" / "loop-engineering.log")
    messages_path = tmp_path / "messages.json"
    messages_path.write_text("[]")
    lines = ['{"is_error":true,"result":"Not logged in","type":"result"}\n']
    monkeypatch.setattr(ds.subprocess, "Popen", lambda *a, **k: _FakeChatPopenProcess(lines))
    key = ds._chat_job_create()
    ds._run_chat_job(key, "hi", messages_path=messages_path)
    events = list(ds._iter_chat_job_chunks(key))
    assert events == [("done", "Not logged in", None)]
    assert json.loads(messages_path.read_text()) == []


def test_run_chat_job_popen_raising_oserror_finishes_job_with_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "UNIFIED_LOG_PATH", tmp_path / "logs" / "loop-engineering.log")
    messages_path = tmp_path / "messages.json"
    messages_path.write_text("[]")

    def raise_oserror(*a, **k):
        raise OSError("claude not found")

    monkeypatch.setattr(ds.subprocess, "Popen", raise_oserror)
    key = ds._chat_job_create()
    ds._run_chat_job(key, "hi", messages_path=messages_path)
    events = list(ds._iter_chat_job_chunks(key))
    assert events[0][0] == "done"
    assert "claude not found" in events[0][1]


class _FakeChatPopenProcessRaisingMidStream:
    """Fake Popen result whose stdout raises partway through iteration - the
    kind of I/O error (e.g. a UnicodeDecodeError from unexpected bytes under
    text=True) that must not propagate out of _run_chat_job uncaught, since
    it runs in a background thread."""

    def __init__(self, lines, exc):
        self._lines = iter(lines)
        self._exc = exc
        self.stdout = self

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self._lines)
        except StopIteration:
            raise self._exc

    def wait(self):
        return 0


def test_run_chat_job_stdout_read_error_finishes_job_with_error_not_a_crash(
        tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "UNIFIED_LOG_PATH", tmp_path / "logs" / "loop-engineering.log")
    messages_path = tmp_path / "messages.json"
    messages_path.write_text("[]")
    lines = [
        '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hel"}}}\n',
    ]
    fake_process = _FakeChatPopenProcessRaisingMidStream(
        lines, RuntimeError("broken pipe"))
    monkeypatch.setattr(ds.subprocess, "Popen", lambda *a, **k: fake_process)
    key = ds._chat_job_create()
    ds._run_chat_job(key, "hi", messages_path=messages_path)  # must not raise
    events = list(ds._iter_chat_job_chunks(key))
    assert events[0] == ("chunk", "hel")
    assert events[-1][0] == "done"
    assert "broken pipe" in events[-1][1]
    assert json.loads(messages_path.read_text()) == []


def test_run_chat_job_append_message_raising_still_finishes_job_with_error(tmp_path, monkeypatch):
    """Fix 2: append_message runs AFTER the stdout-read loop's own
    try/except, so a prior fix round that only wrapped the loop itself
    left this call unguarded - if it raised (disk full, permission error,
    _atomic_write_json's own re-raise-after-unlink-on-failure), the
    exception would propagate out of _run_chat_job uncaught, the
    background thread would die, and _chat_job_finish would never be
    called - _iter_chat_job_chunks then blocks forever. This proves the
    whole call (stdout loop AND the append_message call after it) is now
    covered by one guarantee: _chat_job_finish is always reached."""
    monkeypatch.setattr(ds, "UNIFIED_LOG_PATH", tmp_path / "logs" / "loop-engineering.log")
    messages_path = tmp_path / "messages.json"
    messages_path.write_text("[]")
    lines = ['{"is_error":false,"result":"hello","type":"result"}\n']
    monkeypatch.setattr(ds.subprocess, "Popen", lambda *a, **k: _FakeChatPopenProcess(lines))

    def raise_disk_full(*a, **k):
        raise OSError("No space left on device")

    monkeypatch.setattr(ds, "append_message", raise_disk_full)
    key = ds._chat_job_create()
    ds._run_chat_job(key, "hi", messages_path=messages_path)  # must not raise
    events = list(ds._iter_chat_job_chunks(key))
    assert events[-1][0] == "done"
    assert "No space left on device" in events[-1][1]


def test_run_chat_job_stdout_read_error_kills_the_child_process(tmp_path, monkeypatch):
    """Bundled cheap fix: on the stdout-read exception path, the child
    process is killed and waited on rather than left for the `timeout 90`
    wrapper to eventually reap it."""
    monkeypatch.setattr(ds, "UNIFIED_LOG_PATH", tmp_path / "logs" / "loop-engineering.log")
    messages_path = tmp_path / "messages.json"
    messages_path.write_text("[]")
    lines = [
        '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hel"}}}\n',
    ]
    fake_process = _FakeChatPopenProcessRaisingMidStream(
        lines, RuntimeError("broken pipe"))
    killed = {"called": False}
    fake_process.kill = lambda: killed.__setitem__("called", True)
    monkeypatch.setattr(ds.subprocess, "Popen", lambda *a, **k: fake_process)
    key = ds._chat_job_create()
    ds._run_chat_job(key, "hi", messages_path=messages_path)
    assert killed["called"] is True


# --- POST /activity/chat route (Activity page live chat assistant) ---


def test_activity_route_chat_requires_csrf(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    messages_path = tmp_path / "messages.json"
    monkeypatch.setattr(ds, "MESSAGES_PATH", messages_path)

    with _running_server() as port:
        status, _headers, _body = _post(port, "/activity/chat", {"text": "hello", "csrf_token": ""})
        assert status == 403
    assert ds.read_messages(messages_path) == []


def test_activity_route_chat_rejects_blank_text(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    messages_path = tmp_path / "messages.json"
    monkeypatch.setattr(ds, "MESSAGES_PATH", messages_path)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/")
        status, headers, body = _post(port, "/activity/chat", {"text": "   ", "csrf_token": token})
        assert status == 400
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        parsed = json.loads(body)
        assert "error" in parsed
    assert ds.read_messages(messages_path) == []


def test_activity_route_chat_appends_user_message_and_streams_a_reply(monkeypatch, tmp_path):
    """Proves the whole route end-to-end through the real threaded server:
    the user's message is saved synchronously before the response comes
    back, the JSON response carries a reply_key immediately (not a
    redirect - the frontend needs it right away to open a streaming
    connection), and the background thread it starts really is
    _run_chat_job wired up to that same reply_key (proven by waiting for
    the job to finish via the real job registry and seeing the assistant's
    reply land in messages.json), not just some thread that happens to
    return 200."""
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds, "UNIFIED_LOG_PATH", tmp_path / "logs" / "loop-engineering.log")
    messages_path = tmp_path / "messages.json"
    monkeypatch.setattr(ds, "MESSAGES_PATH", messages_path)
    lines = ['{"is_error":false,"result":"hello there","type":"result"}\n']
    monkeypatch.setattr(ds.subprocess, "Popen", lambda *a, **k: _FakeChatPopenProcess(lines))

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/")
        status, headers, body = _post(port, "/activity/chat", {"text": "hi there", "csrf_token": token})
        assert status == 200
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        parsed = json.loads(body)
        assert "reply_key" in parsed and parsed["reply_key"]

        saved = ds.read_messages(messages_path)
        assert saved[-1]["from"] == "user"
        assert saved[-1]["text"] == "hi there"
        assert saved[-1]["seen_by_loop"] is False

        # Blocks until the background thread finishes the job - the same
        # synchronization an SSE client gets for free.
        events = list(ds._iter_chat_job_chunks(parsed["reply_key"]))
        assert events == [("done", None, "hello there")]

    saved = ds.read_messages(messages_path)
    assert saved[-1]["from"] == "loop"
    assert saved[-1]["text"] == "hello there"

    log_content = (tmp_path / "logs" / "loop-engineering.log").read_text()
    assert "chat-assistant ---- question" in log_content
    assert "hi there" in log_content


def test_activity_route_chat_thread_start_failure_still_finishes_the_job(monkeypatch, tmp_path):
    """Fix 2's second failure mode: if threading.Thread.start() itself
    raises (e.g. resource exhaustion) after the job was already created
    via _chat_job_create(), the job must not sit in the registry forever
    with no cleanup timer ever scheduled - the route must finish it with
    an error right there. Also proves the "question" entry (written
    synchronously in the route handler, before _chat_job_create()/
    thread.start() even run) is logged even when starting the background
    thread afterwards fails."""
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds, "UNIFIED_LOG_PATH", tmp_path / "logs" / "loop-engineering.log")
    messages_path = tmp_path / "messages.json"
    monkeypatch.setattr(ds, "MESSAGES_PATH", messages_path)

    real_start = ds.threading.Thread.start

    def maybe_raise(self):
        # Only the chat job's own background thread should fail to
        # start - the test HTTP server itself is also a threading.Thread
        # (see _running_server) and must keep working normally, or this
        # test can't even stand up a server to POST against.
        if self._target is ds._run_chat_job:
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(ds.threading.Thread, "start", maybe_raise)

    with _running_server() as port:
        token = _fetch_csrf_token(port, "/")
        status, headers, body = _post(port, "/activity/chat", {"text": "hi there", "csrf_token": token})
        assert status == 200
        parsed = json.loads(body)
        events = list(ds._iter_chat_job_chunks(parsed["reply_key"]))
        assert events[0][0] == "done"
        assert "can't start new thread" in events[0][1]

    log_content = (tmp_path / "logs" / "loop-engineering.log").read_text()
    assert "chat-assistant ---- question" in log_content
    assert "hi there" in log_content


# --- GET /activity/chat-stream route (Activity page live chat assistant) ---


def test_sse_frame_json_encodes_the_data_field():
    frame = ds._sse_frame("chunk", "hello\nworld")
    assert frame == b'event: chunk\ndata: "hello\\nworld"\n\n'


def test_sse_frame_done_with_no_error():
    frame = ds._sse_frame("done", "")
    assert frame == b'event: done\ndata: ""\n\n'


def test_stream_chat_reply_unknown_key_sends_error_event():
    """An SSE client for a reply_key the registry has never seen (or one
    already cleaned up 60s after finishing) gets a real error event and
    the connection ends - it must never hang waiting for a job that will
    never exist."""
    with _running_server() as port:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/activity/chat-stream?reply_key=not-a-real-key", timeout=10
        ) as response:
            assert response.status == 200
            body = response.read()
    assert b"event: error" in body


def test_stream_chat_reply_known_job_streams_chunks_then_done():
    """A job whose chunks were already buffered and finished before the
    SSE client ever connects (the common case for a fast reply) still
    gets the full replay - the buffered chunk followed by the terminal
    done event - not just the done event."""
    key = ds._chat_job_create()
    ds._chat_job_append(key, "hi")
    ds._chat_job_finish(key)

    with _running_server() as port:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/activity/chat-stream?reply_key={key}", timeout=10
        ) as response:
            body = response.read()
    assert b'event: chunk\ndata: "hi"' in body
    assert b"event: done" in body


def test_stream_chat_reply_done_event_carries_the_authoritative_final_text():
    """Fix 3 (server side): the streamed chunks and the persisted reply
    can genuinely diverge (only text_delta chunks are streamed live, only
    the terminal `result` event's text gets saved via append_message) -
    the done frame must carry that same saved text, not an empty string,
    so the frontend can make the bubble match exactly what a page reload
    would show instead of trusting the streamed chunks."""
    key = ds._chat_job_create()
    ds._chat_job_append(key, "hel")
    ds._chat_job_append(key, "lo")
    ds._chat_job_finish(key, final_text="hello")

    with _running_server() as port:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/activity/chat-stream?reply_key={key}", timeout=10
        ) as response:
            body = response.read()
    assert b'event: done\ndata: "hello"' in body


def test_stream_chat_reply_emits_keepalive_comment_during_an_idle_reply(monkeypatch):
    """Fix 4: the docstring on _iter_chat_job_chunks has always promised
    that an idle wakeup turns into an SSE keepalive comment line, but
    _stream_chat_reply never actually wrote one - a reply whose first
    token takes longer than nginx's default proxy_read_timeout (60s, see
    bin/scripts/setup-nginx.sh) to arrive would have its stream silently
    killed on the http://loop.local/ path. _CHAT_STREAM_IDLE_TIMEOUT_SECONDS
    is shrunk here so this test doesn't have to wait out a real ~15s idle
    period; the job is only finished after the client has had a chance to
    observe at least one keepalive tick."""
    monkeypatch.setattr(ds, "_CHAT_STREAM_IDLE_TIMEOUT_SECONDS", 0.05)
    key = ds._chat_job_create()

    def finisher():
        time.sleep(0.3)
        ds._chat_job_finish(key, final_text="done thinking")

    t = threading.Thread(target=finisher)
    t.start()
    with _running_server() as port:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/activity/chat-stream?reply_key={key}", timeout=10
        ) as response:
            body = response.read()
    t.join()
    assert b": keepalive\n\n" in body
    assert b'event: done\ndata: "done thinking"' in body


def test_stream_chat_reply_on_job_error_sends_error_event_not_done():
    key = ds._chat_job_create()
    ds._chat_job_finish(key, error="boom")

    with _running_server() as port:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/activity/chat-stream?reply_key={key}", timeout=10
        ) as response:
            body = response.read()
    assert b'event: error\ndata: "boom"' in body
    assert b"event: done" not in body


def test_stream_chat_reply_sends_no_buffering_headers():
    key = ds._chat_job_create()
    ds._chat_job_finish(key)

    with _running_server() as port:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/activity/chat-stream?reply_key={key}", timeout=10
        ) as response:
            assert response.headers.get("X-Accel-Buffering") == "no"
            assert response.headers.get("Content-Type") == "text/event-stream"
            response.read()


def test_stream_chat_reply_client_disconnect_mid_stream_does_not_crash_server(capfd):
    """A client that vanishes mid-stream (closed browser tab, dropped
    network) makes the next self.wfile.write() raise
    BrokenPipeError/ConnectionResetError - that must be swallowed quietly
    by _stream_chat_reply, not left to propagate out of the request-
    handling thread (which would otherwise reach socketserver's
    handle_error and print a traceback for what is, from the server's
    perspective, a completely routine event). Proven by disconnecting
    while the job is still open, feeding it a chunk and finishing it (so
    the server-side write actually happens against the closed socket),
    then asserting no traceback landed on stderr and that the server is
    still alive and answers a completely unrelated request afterwards.
    Without the except clause in _stream_chat_reply, this test fails on
    the "no traceback" assertion (confirmed manually)."""
    key = ds._chat_job_create()

    with _running_server() as port:
        conn = socket.create_connection(("127.0.0.1", port), timeout=10)
        conn.sendall(
            f"GET /activity/chat-stream?reply_key={key} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n\r\n".encode()
        )
        # Read just the response headers so streaming has actually begun,
        # then disappear without reading the body at all.
        conn.recv(4096)
        conn.close()

        # Give the socket time to actually tear down, then push a chunk
        # and finish the job - _stream_chat_reply's write against the
        # now-dead connection must raise and be caught right here.
        time.sleep(0.2)
        ds._chat_job_append(key, "will not be delivered")
        ds._chat_job_finish(key)
        time.sleep(0.5)

        # The server itself must still be alive and responsive to a
        # completely unrelated request.
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/activity", timeout=10) as response:
            assert response.status == 200

    captured = capfd.readouterr()
    assert "Traceback" not in captured.err
    assert "BrokenPipeError" not in captured.err
    assert "ConnectionResetError" not in captured.err


def test_render_shell_includes_activity_chat_streaming_script(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    page = ds._render_shell("Test", "activity", "", "<p>body</p>")
    assert "activity-composer-form" in page
    assert "EventSource" in page
    assert "/activity/chat-stream" in page
    assert "/activity/chat" in page


def test_chat_composer_lookup_is_deferred_to_domcontentloaded(tmp_path, monkeypatch):
    """Regression test for a real bug that survived multiple prior review
    rounds: the chat composer's DOM lookups used to run immediately at
    <script> parse time - the whole script block is emitted in <head>,
    before #activity-composer-form exists in the page - so in a real
    browser this silently found null and no-opped on every single page
    load. Every previous check for this feature (including live curl
    checks) only asserted that certain strings were PRESENT somewhere in
    the served HTML, which is exactly why this went unnoticed: the
    strings were present, just never executed in the right order.

    This test does not merely check for string presence - it walks the
    actual brace nesting of the rendered script to prove the specific
    `getElementById('activity-composer-form')` call sits directly inside
    a `document.addEventListener('DOMContentLoaded', function() {...})`
    callback, not at the enclosing IIFE's top level. Verified against the
    pre-fix source that this exact check correctly fails there."""
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    page = ds._render_shell("Test", "activity", "", "<p>body</p>")
    script_start = page.index("<script>")
    script_end = page.index("</script>", script_start)
    script = page[script_start:script_end]

    marker = "getElementById('activity-composer-form')"
    marker_pos = script.index(marker)

    # Walk backward from the marker, tracking brace depth, to find the
    # nearest unmatched '{' that opens the function body directly
    # containing it.
    depth = 0
    i = marker_pos
    enclosing_start = None
    while i > 0:
        i -= 1
        ch = script[i]
        if ch == "}":
            depth += 1
        elif ch == "{":
            if depth == 0:
                enclosing_start = i
                break
            depth -= 1
    assert enclosing_start is not None, "could not find an enclosing function body"

    preceding = script[:enclosing_start]
    assert preceding.rstrip().endswith(
        "document.addEventListener('DOMContentLoaded', function()"
    ), (
        "the activity-composer-form lookup is not directly inside a "
        "DOMContentLoaded callback - it would run at <script> parse "
        "time instead, before the element exists in the page"
    )


def test_render_shell_auto_refresh_defers_to_an_in_flight_chat_stream():
    """Fix 1: a routine 30s auto-refresh (location.reload()) must not tear
    down an in-flight chat stream - the pending bubble, its accumulated
    text, and the EventSource connection would all vanish mid-reply. The
    chat script sets window.__loopChatStreaming while streaming and the
    refresh-scheduling script must check it before reloading."""
    page = ds._render_shell("Test", "activity", "", "<p>body</p>", refresh=True)
    assert "__loopChatStreaming" in page
    # Both halves of the coordination must be present: the refresh timer
    # actually checking the flag, and the chat script actually setting it.
    refresh_section = page.split("location.reload()")[0][-400:]
    assert "__loopChatStreaming" in refresh_section
    assert "window.__loopChatStreaming = true" in page


def test_render_shell_chat_script_marks_partial_replies_on_error():
    """Fix 3 (client side): a timeout/failure with partial streamed text
    already in the bubble must not be silently swallowed - the user needs
    a visible signal the reply was interrupted rather than seeing what
    looks like a complete answer that was never actually saved."""
    page = ds._render_shell("Test", "activity", "", "<p>body</p>")
    assert "reply interrupted" in page


def test_message_bubble_has_asymmetric_tail_radius():
    bubble_section = ds._STYLE.split(".message-bubble {")[1].split("\n}")[0]
    assert "border-radius" in bubble_section
    user_section = ds._STYLE.split(".message-bubble-user {")[1].split("\n}")[0]
    assert "border-bottom-right-radius" in user_section
    loop_section = ds._STYLE.split(".message-bubble-loop {")[1].split("\n}")[0]
    assert "border-bottom-left-radius" in loop_section


def test_message_brand_icon_is_sized():
    assert ".message-brand-icon" in ds._STYLE
    icon_section = ds._STYLE.split(".message-brand-icon {")[1].split("\n}")[0]
    assert "color:" in icon_section or "color :" in icon_section


def test_activity_message_list_is_bounded_and_scrollable():
    assert "#activity-message-list" in ds._STYLE
    section = ds._STYLE.split("#activity-message-list {")[1].split("\n}")[0]
    assert "overflow-y: auto" in section
    assert "max-height" in section


def test_material_symbols_icon_names_includes_monitoring():
    assert "monitoring" in ds._MATERIAL_SYMBOLS_ICON_NAMES


def test_sidebar_html_marks_analytics_active():
    sidebar = ds._sidebar_html("analytics")
    assert "<a href='/analytics' title='Analytics' class='active'>" in sidebar


def test_render_analytics_page_empty_event_log_renders_without_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds.metrics.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")

    output = ds.render_analytics_page(days=7)

    assert "Analytics" in output
    assert "N/A" in output


def test_render_analytics_page_health_score_shows_partial_note_and_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds.metrics.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")

    output = ds.render_analytics_page(days=7)

    assert "Partial score" in output
    assert "cost_efficiency" in output


def test_render_analytics_page_quality_section_shows_na_tiles_with_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds.metrics.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")

    output = ds.render_analytics_page(days=7)

    assert "First-pass MR" in output
    assert "needs Phase 10 human-review data" in output


def test_render_analytics_page_invalid_days_value_defaults_to_seven(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds.metrics.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")
    captured = {}
    real_build_report = ds.metrics.build_report

    def spy_build_report(events_dir=None, since_date=None, until_date=None):
        captured.setdefault("since_date", since_date)  # only the page's own main-report call, not the Trend section's per-bucket calls
        return real_build_report(events_dir=events_dir, since_date=since_date, until_date=until_date)

    monkeypatch.setattr(ds.metrics, "build_report", spy_build_report)

    ds.render_analytics_page(days=999)  # not in (7, 30, 90)

    today = datetime.now(timezone.utc).date()
    assert captured["since_date"] == (today - timedelta(days=6)).isoformat()


def test_render_analytics_page_days_30_changes_query_window(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds.metrics.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")
    captured = {}
    real_build_report = ds.metrics.build_report

    def spy_build_report(events_dir=None, since_date=None, until_date=None):
        # only the page's own main-report call, not the Trend section's per-bucket calls
        captured.setdefault("since_date", since_date)
        captured.setdefault("until_date", until_date)
        return real_build_report(events_dir=events_dir, since_date=since_date, until_date=until_date)

    monkeypatch.setattr(ds.metrics, "build_report", spy_build_report)

    ds.render_analytics_page(days=30)

    today = datetime.now(timezone.utc).date()
    assert captured["since_date"] == (today - timedelta(days=29)).isoformat()
    assert captured["until_date"] == today.isoformat()


def test_trend_line_chart_svg_renders_polyline_for_real_values():
    svg = ds._trend_line_chart_svg("Autonomy rate", [("2026-09-01", 50.0), ("2026-09-02", 75.0)], unit="%")

    assert "<polyline" in svg
    assert "Autonomy rate" in svg


def test_trend_line_chart_svg_breaks_line_across_none_gap():
    svg = ds._trend_line_chart_svg("Autonomy rate", [("d1", 50.0), ("d2", None), ("d3", 60.0)], unit="%")

    assert svg.count("<polyline") == 2  # one segment before the gap, one after


def test_trend_line_chart_svg_empty_data_shows_no_data_message():
    svg = ds._trend_line_chart_svg("Autonomy rate", [("d1", None), ("d2", None)], unit="%")

    assert "no data" in svg
    assert "<svg" not in svg


def test_render_analytics_page_trend_section_shows_four_charts_and_mr_acceptance_placeholder(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds.metrics.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")

    output = ds.render_analytics_page(days=7)

    assert "Trend" in output
    assert "Autonomy rate" in output
    assert "Resolution rate" in output
    assert "Verification pass rate" in output
    assert "Cost per resolution" in output
    assert "MR acceptance" in output
    assert "Not yet tracked" in output


def test_trend_line_chart_svg_percentage_uses_fixed_0_100_scale_not_tight_range():
    # a tight data range (79-81) would, under min/max auto-scaling, spread these
    # three points across nearly the full plot height; anchored to a fixed 0-100
    # domain they should instead sit near the top of the chart.
    svg = ds._trend_line_chart_svg("Resolution rate", [("d1", 79.0), ("d2", 80.0), ("d3", 81.0)], unit="%")

    assert "<polyline" in svg
    points_attr = svg.split("points='")[1].split("'")[0]
    y_values = [float(pair.split(",")[1]) for pair in points_attr.split(" ")]
    plot_top, plot_bottom = 12, 140 - 12
    plot_h = plot_bottom - plot_top
    for y in y_values:
        assert y < plot_top + plot_h * 0.3


def test_trend_line_chart_svg_cost_chart_shows_max_value_label():
    svg = ds._trend_line_chart_svg("Cost per resolution", [("d1", 1.5), ("d2", 3.25)], unit="$")

    assert "3.25" in svg
    assert "max" in svg.lower()


def test_trend_line_chart_svg_note_renders_as_caption():
    svg = ds._trend_line_chart_svg(
        "Autonomy rate", [("d1", 50.0)], unit="%", note="placeholder: currently identical to resolution rate"
    )

    assert "placeholder: currently identical to resolution rate" in svg


def test_render_analytics_page_populated_event_log_shows_real_numbers(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    events_dir = tmp_path / "events"
    monkeypatch.setattr(ds.metrics.events, "DEFAULT_EVENTS_DIR", events_dir)

    run_id = "run_pop"
    events.emit("issue.started", run_id=run_id, issue_run_id="run_pop_i1", events_dir=events_dir)
    events.emit("issue.completed", run_id=run_id, issue_run_id="run_pop_i1", events_dir=events_dir)
    events.emit("issue.started", run_id=run_id, issue_run_id="run_pop_i2", events_dir=events_dir)
    events.emit("issue.escalated", run_id=run_id, issue_run_id="run_pop_i2", events_dir=events_dir)
    events.emit("verification.started", run_id=run_id, issue_run_id="run_pop_i1", events_dir=events_dir)
    events.emit("verification.passed", run_id=run_id, issue_run_id="run_pop_i1", events_dir=events_dir)
    events.emit("verification.started", run_id=run_id, issue_run_id="run_pop_i2", events_dir=events_dir)
    events.emit("verification.failed", run_id=run_id, issue_run_id="run_pop_i2", events_dir=events_dir)
    events.emit(
        "run.completed", run_id=run_id, events_dir=events_dir,
        data={"cost_usd": 12.0, "input_tokens": 1000, "output_tokens": 500, "cache_read_tokens": 0, "cache_write_tokens": 0},
    )

    output = ds.render_analytics_page(days=7)

    # real issue counts (2 processed, 1 completed, 1 escalated) - not "N/A"
    assert "50.0%" in output  # resolution rate AND autonomy rate: 1 completed / 2 processed
    assert "$12.00" in output  # total AI cost
    assert "$6.00" in output  # cost per issue: 12.0 / 2 processed (same priced run_id)
    assert "50/100" in output  # health score: every known component computes to 50 with this fixture

    outcomes_html = output.split("<h2>Outcomes</h2>")[1].split("<h2>Quality</h2>")[0]
    assert "N/A" not in outcomes_html

    cost_html = output.split("<h2>Cost</h2>")[1].split("<h2>Learning</h2>")[0]
    assert "N/A" not in cost_html

    # finding 1: autonomy placeholder disclosed (Outcomes tile, Health tile, Trend caption)
    assert output.count("placeholder: currently identical to resolution rate") >= 2

    # finding 2: escalation tile's inverted meaning is disclosed
    assert "Non-escalation" in output
    assert "higher is healthier" in output


def test_render_analytics_page_risk_classification_section_shows_zero_state(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds.metrics.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")

    output = ds.render_analytics_page(days=7)

    assert "Risk" in output
    assert "Classification" in output
    assert "By type" in output
    assert "No data" in output


def test_render_analytics_page_risk_classification_section_shows_real_breakdown(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    events_dir = tmp_path / "events"
    monkeypatch.setattr(ds.metrics.events, "DEFAULT_EVENTS_DIR", events_dir)
    events.emit(
        "issue.classified", run_id="run_1", issue_run_id="run_1_kurrant_1",
        project="kurrant", issue_iid=1,
        data={"type": "bug", "complexity": "M", "risk_level": "MEDIUM"},
        events_dir=events_dir,
    )

    output = ds.render_analytics_page(days=7)

    assert "bug" in output
    assert "Medium" in output


def test_render_analytics_page_failure_breakdown_section_shows_na_when_no_escalations(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds.metrics.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")

    output = ds.render_analytics_page(days=7)

    assert "Failure Breakdown" in output
    assert "no escalations in this window" in output


def test_render_analytics_page_failure_breakdown_section_shows_percentage(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    events_dir = tmp_path / "events"
    monkeypatch.setattr(ds.metrics.events, "DEFAULT_EVENTS_DIR", events_dir)
    events.emit(
        "issue.escalated", run_id="run_1", issue_run_id="run_1_kurrant_1",
        project="kurrant", issue_iid=1, data={"reason": "needs_clarification"},
        events_dir=events_dir,
    )

    output = ds.render_analytics_page(days=7)

    assert "Requirement" in output
    assert "100.0%" in output
    assert "Escalations" in output


def test_render_analytics_page_quality_section_shows_first_pass_verification_tile(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds.metrics.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")

    output = ds.render_analytics_page(days=7)

    quality_html = output.split("<h2>Quality</h2>")[1].split("</section>")[0]
    assert "First-pass verification" in quality_html


def test_render_analytics_page_sections_ordered_quality_then_risk_then_failure_then_cost(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds.metrics.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")

    output = ds.render_analytics_page(days=7)

    quality_idx = output.index("<h2>Quality</h2>")
    risk_idx = output.index("Classification</h2>")
    failure_idx = output.index("<h2>Failure Breakdown</h2>")
    cost_idx = output.index("<h2>Cost</h2>")
    assert quality_idx < risk_idx < failure_idx < cost_idx


def test_render_analytics_page_learning_section_shows_zero_state(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds.metrics.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")
    monkeypatch.setattr(ds.learning.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")

    output = ds.render_analytics_page(days=7)

    assert "Learning" in output
    assert "Lessons created" in output
    assert "Failures prevented" in output
    assert ds.learning.FAILURES_PREVENTED_REASON in output


def test_render_analytics_page_learning_section_shows_real_numbers(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    events_dir = tmp_path / "events"
    monkeypatch.setattr(ds.metrics.events, "DEFAULT_EVENTS_DIR", events_dir)
    monkeypatch.setattr(ds.learning.events, "DEFAULT_EVENTS_DIR", events_dir)
    events.emit("memory.created", run_id="run_1", project="kurrant", data={"lesson_id": "lesson_1", "category": "testing"}, events_dir=events_dir)
    events.emit("issue.started", run_id="run_1", issue_run_id="run_1_kurrant_1", project="kurrant", events_dir=events_dir)
    events.emit("memory.reused", run_id="run_1", issue_run_id="run_1_kurrant_1", project="kurrant", data={"lesson_id": "lesson_1"}, events_dir=events_dir)
    events.emit("issue.completed", run_id="run_1", issue_run_id="run_1_kurrant_1", project="kurrant", events_dir=events_dir)

    output = ds.render_analytics_page(days=7)

    learning_html = output.split("<h2>Learning</h2>")[1].split("</section>")[0]
    assert "100.0%" in learning_html  # both reuse rate and success rate are 100% with this fixture


def test_render_analytics_page_sections_include_learning_between_cost_and_trend(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "does-not-exist-status.json")
    monkeypatch.setattr(ds.metrics.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")
    monkeypatch.setattr(ds.learning.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")

    output = ds.render_analytics_page(days=7)

    cost_idx = output.index("<h2>Cost</h2>")
    learning_idx = output.index("<h2>Learning</h2>")
    trend_idx = output.index("<h2>Trend</h2>")
    assert cost_idx < learning_idx < trend_idx


def test_render_memory_page_shows_category_pill_and_reuse_stats(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    events_dir = tmp_path / "events"
    monkeypatch.setattr(ds.learning.events, "DEFAULT_EVENTS_DIR", events_dir)
    monkeypatch.setattr(ds, "get_project_memory", lambda *a, **k: {
        "myproj": {"legacy": [], "tasks": [{
            "body": "Always run tests.", "issue_iid": 1, "tags": [],
            "lesson_id": "lesson_1", "category": "testing",
        }]},
    })
    monkeypatch.setattr(ds, "gitlab_issue_url_prefixes", lambda *a, **k: {})
    events.emit("memory.created", run_id="run_1", project="myproj", data={"lesson_id": "lesson_1", "category": "testing"}, events_dir=events_dir)
    events.emit("issue.started", run_id="run_1", issue_run_id="run_1_myproj_2", project="myproj", events_dir=events_dir)
    events.emit("memory.reused", run_id="run_1", issue_run_id="run_1_myproj_2", project="myproj", data={"lesson_id": "lesson_1"}, events_dir=events_dir)
    events.emit("issue.completed", run_id="run_1", issue_run_id="run_1_myproj_2", project="myproj", events_dir=events_dir)

    output = ds.render_memory_page()

    assert "<span class='pill pill-grey'>testing</span>" in output
    assert "Reused 1×" in output
    assert "1 successful, 0 failed" in output


def test_render_memory_page_shows_not_yet_reused_for_lesson_with_no_reuses(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(ds.learning.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")
    monkeypatch.setattr(ds, "get_project_memory", lambda *a, **k: {
        "myproj": {"legacy": [], "tasks": [{
            "body": "Always run tests.", "issue_iid": 1, "tags": [],
            "lesson_id": "lesson_1", "category": "testing",
        }]},
    })
    monkeypatch.setattr(ds, "gitlab_issue_url_prefixes", lambda *a, **k: {})

    output = ds.render_memory_page()

    assert "Not yet reused" in output


def test_render_memory_page_pre_sprint_6_entry_shows_no_category_pill_or_reuse_stats(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(ds.learning.events, "DEFAULT_EVENTS_DIR", tmp_path / "events")
    monkeypatch.setattr(ds, "get_project_memory", lambda *a, **k: {
        "myproj": {"legacy": [], "tasks": [{
            "body": "Always run tests.", "issue_iid": 1, "tags": [],
            "lesson_id": None, "category": None,
        }]},
    })
    monkeypatch.setattr(ds, "gitlab_issue_url_prefixes", lambda *a, **k: {})

    output = ds.render_memory_page()

    assert "Not yet reused" not in output
    assert "Reused" not in output
