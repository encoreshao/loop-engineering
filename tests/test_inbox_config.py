import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import inbox_config  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _inbox(**overrides):
    base = {
        "name": "work-gmail", "label": "Work Gmail", "provider": "gmail",
        "account": "me@example.com", "enabled": True, "urgent_brief": "",
        "vip_senders": [], "exclude_senders": [], "categories": None, "slack_bundle": None,
    }
    base.update(overrides)
    return base


def _write(path, inboxes, default_categories=None):
    path.write_text(json.dumps({
        "default_categories": default_categories or inbox_config.DEFAULT_CATEGORIES,
        "inboxes": inboxes,
    }))


def test_template_parses_and_is_valid():
    data = json.loads((REPO_ROOT / "config" / "inboxes.json.template").read_text())
    assert [c["key"] for c in data["default_categories"]] == [
        "urgent", "action", "fyi", "notifications", "newsletters"]
    assert [c["label"] for c in data["default_categories"]] == [
        "Loop/Urgent", "Loop/Action", "Loop/FYI", "Loop/Notifications", "Loop/Newsletters"]
    assert [c["key"] for c in data["default_categories"] if c["draft"]] == ["urgent"]
    for inbox in data["inboxes"]:
        assert inbox_config.validate_inbox(inbox) == []
        assert inbox["enabled"] is False


def test_load_config_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="inboxes.json.template"):
        inbox_config.load_config(tmp_path / "inboxes.json")


def test_load_config_or_empty_returns_defaults_when_missing(tmp_path):
    data = inbox_config.load_config_or_empty(tmp_path / "inboxes.json")
    assert data == {"default_categories": inbox_config.DEFAULT_CATEGORIES, "inboxes": []}


def test_default_path_resolved_at_call_time(tmp_path, monkeypatch):
    target = tmp_path / "inboxes.json"
    _write(target, [_inbox()])
    monkeypatch.setattr(inbox_config, "DEFAULT_CONFIG_PATH", target)
    assert inbox_config.load_config()["inboxes"][0]["name"] == "work-gmail"


@pytest.mark.parametrize("overrides,fragment", [
    ({"name": "Bad Name"}, "name"),
    ({"name": ""}, "name"),
    ({"provider": "yahoo"}, "provider"),
    ({"account": "not-an-email"}, "account"),
    ({"vip_senders": "a@b.com"}, "vip_senders"),
])
def test_validate_inbox_rejects_bad_fields(overrides, fragment):
    errors = inbox_config.validate_inbox(_inbox(**overrides))
    assert any(fragment in e for e in errors)


def test_validate_inbox_rejects_custom_categories_missing_urgent():
    custom = [{"key": "fyi", "label": "Loop/FYI", "description": "x", "draft": False}]
    errors = inbox_config.validate_inbox(_inbox(categories=custom))
    assert any("urgent" in e for e in errors)


def test_load_config_rejects_duplicate_names(tmp_path):
    path = tmp_path / "inboxes.json"
    _write(path, [_inbox(), _inbox()])
    with pytest.raises(ValueError, match="duplicate"):
        inbox_config.load_config(path)


def test_categories_for_defaults_and_override():
    config = {"default_categories": inbox_config.DEFAULT_CATEGORIES, "inboxes": []}
    assert inbox_config.categories_for(_inbox(), config) == inbox_config.DEFAULT_CATEGORIES
    custom = [{"key": "urgent", "label": "Loop/Urgent", "description": "x", "draft": True}]
    assert inbox_config.categories_for(_inbox(categories=custom), config) == custom


def test_enabled_inboxes_skips_disabled(tmp_path):
    path = tmp_path / "inboxes.json"
    _write(path, [_inbox(), _inbox(name="home", enabled=False)])
    assert [i["name"] for i in inbox_config.enabled_inboxes(path)] == ["work-gmail"]


def test_upsert_inbox_creates_then_updates_preserving_enabled(tmp_path):
    path = tmp_path / "inboxes.json"
    ok, _ = inbox_config.upsert_inbox(_inbox(), is_new=True, config_path=path)
    assert ok
    inbox_config.set_enabled("work-gmail", False, config_path=path)
    ok, _ = inbox_config.upsert_inbox(_inbox(label="Renamed label", enabled=True), is_new=False, config_path=path)
    assert ok
    saved = inbox_config.get_inbox("work-gmail", path)
    assert saved["label"] == "Renamed label"
    assert saved["enabled"] is False


def test_upsert_inbox_new_with_existing_name_is_rejected(tmp_path):
    path = tmp_path / "inboxes.json"
    inbox_config.upsert_inbox(_inbox(), is_new=True, config_path=path)
    ok, message = inbox_config.upsert_inbox(_inbox(), is_new=True, config_path=path)
    assert not ok and "already exists" in message


def test_upsert_inbox_rejects_rename(tmp_path):
    """Review Focus 5: `name` keys the Keychain token and seen-state, so an
    edit that targets a name that doesn't exist is a rename attempt and is
    refused rather than silently creating a second, unconnected inbox."""
    path = tmp_path / "inboxes.json"
    inbox_config.upsert_inbox(_inbox(), is_new=True, config_path=path)
    ok, message = inbox_config.upsert_inbox(_inbox(name="renamed"), is_new=False, config_path=path)
    assert not ok and "cannot be renamed" in message
    assert [i["name"] for i in inbox_config.load_config(path)["inboxes"]] == ["work-gmail"]


def test_delete_inbox(tmp_path):
    path = tmp_path / "inboxes.json"
    inbox_config.upsert_inbox(_inbox(), is_new=True, config_path=path)
    assert inbox_config.delete_inbox("work-gmail", config_path=path)[0]
    assert inbox_config.load_config(path)["inboxes"] == []
    assert not inbox_config.delete_inbox("work-gmail", config_path=path)[0]


def test_save_oauth_client_writes_0600_and_merges(tmp_path):
    path = tmp_path / "mail_oauth.json"
    assert inbox_config.save_oauth_client("google", "gid", "gsecret", oauth_path=path)[0]
    assert inbox_config.save_oauth_client("microsoft", "mid", oauth_path=path)[0]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert inbox_config.load_oauth(path) == {
        "google": {"client_id": "gid", "client_secret": "gsecret"},
        "microsoft": {"client_id": "mid"},
    }


def test_save_oauth_client_rejects_blank_or_unknown(tmp_path):
    path = tmp_path / "mail_oauth.json"
    assert not inbox_config.save_oauth_client("google", "", "s", oauth_path=path)[0]
    assert not inbox_config.save_oauth_client("google", "id", "", oauth_path=path)[0]
    assert not inbox_config.save_oauth_client("yahoo", "id", oauth_path=path)[0]


def test_load_oauth_missing_is_empty(tmp_path):
    assert inbox_config.load_oauth(tmp_path / "nope.json") == {}


@pytest.mark.parametrize("header,expected", [
    ("Alice <Alice@Example.com>", "alice@example.com"),
    ("bob@example.com", "bob@example.com"),
    ('"Doe, Jane" <jane@x.org>', "jane@x.org"),
    ("", ""),
])
def test_parse_address(header, expected):
    assert inbox_config.parse_address(header) == expected


def test_sender_matches_exact_and_domain():
    assert inbox_config.sender_matches("A@Client.com", ["a@client.com"])
    assert inbox_config.sender_matches("x@client.com", ["@client.com"])
    assert not inbox_config.sender_matches("x@notclient.com", ["@client.com"])
    assert not inbox_config.sender_matches("", ["@client.com"])


def test_default_categories_not_aliased_in_config(tmp_path):
    """Verify DEFAULT_CATEGORIES is copied, not aliased, so mutations don't leak."""
    original_urgent_label = inbox_config.DEFAULT_CATEGORIES[0]["label"]

    path = tmp_path / "inboxes.json"
    _write(path, [_inbox()])
    config = inbox_config.load_config(path)
    config["default_categories"][0]["label"] = "CORRUPTED"

    assert inbox_config.DEFAULT_CATEGORIES[0]["label"] == original_urgent_label


def test_default_categories_not_aliased_in_empty_config(tmp_path):
    """Verify load_config_or_empty returns a copy of DEFAULT_CATEGORIES."""
    original_urgent_label = inbox_config.DEFAULT_CATEGORIES[0]["label"]

    config = inbox_config.load_config_or_empty(tmp_path / "nope.json")
    config["default_categories"][0]["label"] = "CORRUPTED"

    assert inbox_config.DEFAULT_CATEGORIES[0]["label"] == original_urgent_label


def test_categories_for_returns_copy():
    """Verify categories_for's fallback returns a copy of DEFAULT_CATEGORIES."""
    original_urgent_label = inbox_config.DEFAULT_CATEGORIES[0]["label"]
    config = {"default_categories": inbox_config.DEFAULT_CATEGORIES, "inboxes": []}

    cats = inbox_config.categories_for(_inbox(), config)
    cats[0]["label"] = "CORRUPTED"

    assert inbox_config.DEFAULT_CATEGORIES[0]["label"] == original_urgent_label


_CUSTOM_CATEGORIES = [{"key": "urgent", "label": "Urgent", "description": "Needs me today"},
                      {"key": "fyi", "label": "FYI", "description": "Everything else"}]


def test_upsert_inbox_edit_without_categories_keeps_existing(tmp_path):
    path = tmp_path / "inboxes.json"
    inbox_config.upsert_inbox(_inbox(categories=_CUSTOM_CATEGORIES), is_new=True, config_path=path)
    fields = _inbox(label="Edited")
    del fields["categories"]
    ok, _ = inbox_config.upsert_inbox(fields, is_new=False, config_path=path)
    assert ok
    saved = inbox_config.get_inbox("work-gmail", path)
    assert saved["label"] == "Edited" and saved["categories"] == _CUSTOM_CATEGORIES


def test_upsert_inbox_edit_with_explicit_categories_replaces_them(tmp_path):
    path = tmp_path / "inboxes.json"
    inbox_config.upsert_inbox(_inbox(categories=_CUSTOM_CATEGORIES), is_new=True, config_path=path)
    ok, _ = inbox_config.upsert_inbox(_inbox(categories=None), is_new=False, config_path=path)
    assert ok and inbox_config.get_inbox("work-gmail", path)["categories"] is None
    replacement = _CUSTOM_CATEGORIES[:1]
    ok, _ = inbox_config.upsert_inbox(_inbox(categories=replacement), is_new=False, config_path=path)
    assert ok and inbox_config.get_inbox("work-gmail", path)["categories"] == replacement
