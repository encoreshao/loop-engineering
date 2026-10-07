import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import connectors
import connectors_config as cc
from connectors.base import Connector, Field


class MemStore:
    def __init__(self, fail_delete=False):
        self.data = {}
        self.fail_delete = fail_delete

    def get(self, ref):
        return self.data.get(ref)

    def put(self, ref, s):
        self.data[ref] = s

    def delete(self, ref):
        if self.fail_delete:
            raise RuntimeError("locked")
        self.data.pop(ref, None)


class FakeGithub(Connector):
    type = "github"
    label = "GitHub"
    capabilities = frozenset({"issues", "merge_requests", "pipelines"})
    fields = (Field("api_url", "API URL", kind="url"), Field("username", "Username"))
    secret_label = "Token"


class FakeGitlab(Connector):
    type = "gitlab"
    label = "GitLab"
    capabilities = frozenset({"issues", "merge_requests", "pipelines"})
    external = True


class FakeSlack(Connector):
    type = "slack"
    label = "Slack"
    capabilities = frozenset({"notify"})
    external = True


class FakeMailbox(Connector):
    type = "mailbox"
    label = "Mailbox"
    capabilities = frozenset({"mail"})
    external = True


@pytest.fixture(autouse=True)
def fake_types(monkeypatch):
    types = {c.type: c for c in (FakeGithub, FakeGitlab, FakeSlack, FakeMailbox)}
    monkeypatch.setattr(connectors, "CONNECTOR_TYPES", types)
    monkeypatch.setattr(connectors, "_load_all", lambda: None)


@pytest.fixture
def paths(tmp_path):
    (tmp_path / "gitlab.json").write_text(json.dumps({"instances": {"work": {"url": "https://gl.example", "token": "glpat"}}}))
    (tmp_path / "slack.json").write_text(json.dumps({"webhook_url": "https://hooks.slack.com/x",
                                                     "bundle_webhooks": {"ops": "https://hooks.slack.com/ops"}}))
    (tmp_path / "inboxes.json").write_text(json.dumps({"default_categories": [], "inboxes": [
        {"name": "me-gmail", "label": "Me", "provider": "gmail", "account": "a@b.co", "enabled": True}]}))
    return {"config_path": tmp_path / "connectors.json", "gitlab_config_path": tmp_path / "gitlab.json",
            "slack_config_path": tmp_path / "slack.json", "inbox_config_path": tmp_path / "inboxes.json"}


def gh(id_, label="x", **kw):
    return {"id": id_, "type": "github", "label": label, "api_url": "https://api.github.com", "username": "u", **kw}


def test_list_accounts_merges_external(paths):
    accounts = {a["id"]: a for a in cc.list_accounts(**paths)}
    assert accounts["work"]["type"] == "gitlab" and accounts["work"]["managed_by"] == "gitlab-config"
    assert accounts["slack-default"]["managed_by"] == "slack-config"
    assert accounts["slack-ops"]["type"] == "slack"
    assert accounts["me-gmail"]["type"] == "mailbox"
    assert accounts["me-gmail"]["managed_by"] == "inboxes"
    assert accounts["me-gmail"]["settings"] == {"provider": "gmail", "account": "a@b.co"}
    blob = json.dumps(accounts)
    assert "glpat" not in blob and "hooks.slack.com" not in blob


def test_upsert_native_stores_secret_in_store_not_json(paths):
    store = MemStore()
    ok, _ = cc.upsert_account(gh("gh", "GH"), "tok", store=store, **paths)
    assert ok
    assert store.data["gh"] == "tok"
    assert "tok" not in paths["config_path"].read_text()
    assert cc.get_account("gh", **paths)["managed_by"] == "native"


def test_upsert_blank_secret_keeps_existing(paths):
    store = MemStore()
    cc.upsert_account(gh("gh", "GH"), "tok", store=store, **paths)
    ok, _ = cc.upsert_account(gh("gh", "GH2"), "", original_id="gh", store=store, **paths)
    assert ok and store.data["gh"] == "tok"
    assert cc.get_account("gh", **paths)["label"] == "GH2"


def test_upsert_new_requires_secret(paths):
    ok, msg = cc.upsert_account(gh("gh"), "", store=MemStore(), **paths)
    assert not ok and "Token" in msg


def test_upsert_rejects_id_used_by_external_account(paths):
    ok, msg = cc.upsert_account(gh("work"), "t", store=MemStore(), **paths)
    assert not ok and "already" in msg


def test_upsert_rejects_duplicate_native_id(paths):
    store = MemStore()
    cc.upsert_account(gh("gh"), "t", store=store, **paths)
    ok, msg = cc.upsert_account(gh("gh"), "t", store=store, **paths)
    assert not ok and "already" in msg


def test_upsert_rejects_bad_id(paths):
    ok, _ = cc.upsert_account(gh("../x"), "t", store=MemStore(), **paths)
    assert not ok


def test_upsert_rejects_unknown_and_external_type(paths):
    assert not cc.upsert_account({**gh("a"), "type": "nope"}, "t", store=MemStore(), **paths)[0]
    assert not cc.upsert_account({**gh("a"), "type": "gitlab"}, "t", store=MemStore(), **paths)[0]


def test_upsert_validation_errors_in_message(paths):
    ok, msg = cc.upsert_account(gh("a", api_url="ftp://x"), "t", store=MemStore(), **paths)
    assert not ok and "http" in msg
    assert not paths["config_path"].exists()


def test_upsert_non_string_values_do_not_crash(paths):
    ok, _ = cc.upsert_account({"id": "a", "type": "github", "label": 5, "api_url": None, "username": ["x"]},
                              "t", store=MemStore(), **paths)
    assert not ok


def test_upsert_rename_moves_secret(paths):
    store = MemStore()
    cc.upsert_account(gh("a"), "t", store=store, **paths)
    ok, _ = cc.upsert_account(gh("b"), "", original_id="a", store=store, **paths)
    assert ok and store.data == {"b": "t"}
    assert [a["id"] for a in cc.list_accounts(**paths) if a["managed_by"] == "native"] == ["b"]


def test_delete_removes_secret_before_json(paths):
    cc.upsert_account(gh("gh"), "t", store=MemStore(), **paths)
    ok, _ = cc.delete_account("gh", config_path=paths["config_path"], store=MemStore(fail_delete=True))
    assert not ok
    assert "gh" in paths["config_path"].read_text()


def test_delete_native_succeeds(paths):
    store = MemStore()
    cc.upsert_account(gh("gh"), "t", store=store, **paths)
    ok, _ = cc.delete_account("gh", config_path=paths["config_path"], store=store)
    assert ok and store.data == {}
    assert json.loads(paths["config_path"].read_text()) == []


def test_delete_external_refused(paths):
    ok, _ = cc.delete_account("work", config_path=paths["config_path"], store=MemStore())
    assert not ok


def test_accounts_with_capability(paths):
    assert [a["id"] for a in cc.accounts_with_capability("merge_requests", **paths)] == ["work"]
    cc.upsert_account(gh("gh"), "t", store=MemStore(), **paths)
    assert {a["id"] for a in cc.accounts_with_capability("merge_requests", **paths)} == {"work", "gh"}


def test_get_account_unknown_raises_keyerror(paths):
    with pytest.raises(KeyError):
        cc.get_account("nope", **paths)


def test_list_accounts_malformed_json_raises_config_error(paths):
    paths["config_path"].write_text("{not json")
    with pytest.raises(cc.ConnectorConfigError):
        cc.list_accounts(**paths)


def test_hand_edited_non_string_values_do_not_crash(paths):
    paths["config_path"].write_text(json.dumps([{"id": "x", "type": "github", "label": 7, "enabled": "yes",
                                                 "settings": {"api_url": 3}}, "junk"]))
    accounts = cc.list_accounts(**paths)
    assert [a["id"] for a in accounts if a["managed_by"] == "native"] == ["x"]


def test_missing_files_are_empty(tmp_path):
    assert cc.list_accounts(config_path=tmp_path / "a", gitlab_config_path=tmp_path / "b",
                            slack_config_path=tmp_path / "c", inbox_config_path=tmp_path / "d") == []


def test_load_connector_native_and_external(paths):
    store = MemStore()
    cc.upsert_account(gh("gh"), "tok", store=store, **paths)
    c = cc.load_connector("gh", store=store, **paths)
    assert isinstance(c, FakeGithub) and c.secret == "tok"
    assert cc.load_connector("work", store=store, **paths).secret == "glpat"
    assert cc.load_connector("slack-default", store=store, **paths).secret == "https://hooks.slack.com/x"
    assert cc.load_connector("slack-ops", store=store, **paths).secret == "https://hooks.slack.com/ops"
    assert cc.load_connector("me-gmail", store=store, **paths).secret is None


def test_id_length_boundary(paths):
    assert cc.upsert_account(gh("a" * 48), "t", store=MemStore(), **paths)[0]
    assert not cc.upsert_account(gh("a" * 49), "t", store=MemStore(), **paths)[0]


def test_upsert_bool_values_rejected_except_enabled(paths):
    assert not cc.upsert_account({**gh("a"), "id": True}, "t", store=MemStore(), **paths)[0]
    assert not cc.upsert_account({**gh("a"), "api_url": True}, "t", store=MemStore(), **paths)[0]
    assert cc.upsert_account({**gh("a"), "enabled": False}, "t", store=MemStore(), **paths)[0]
    assert cc.get_account("a", **paths)["enabled"] is False


def test_upsert_with_malformed_connectors_json_returns_error(paths):
    paths["config_path"].write_text("{not json")
    ok, msg = cc.upsert_account(gh("gh"), "t", store=MemStore(), **paths)
    assert ok is False and "connectors" in msg.lower()


def test_delete_with_malformed_connectors_json_returns_error(paths):
    paths["config_path"].write_text("{not json")
    ok, msg = cc.delete_account("gh", config_path=paths["config_path"], store=MemStore())
    assert ok is False and "connectors" in msg.lower()


def test_upsert_and_delete_write_failure_returns_error(paths, monkeypatch):
    store = MemStore()
    cc.upsert_account(gh("gh"), "t", store=store, **paths)

    def boom(entries, config_path):
        raise OSError("disk full")
    monkeypatch.setattr(cc, "_write", boom)
    ok, msg = cc.upsert_account(gh("gh2"), "t", store=store, **paths)
    assert ok is False and "disk full" in msg
    ok, msg = cc.delete_account("gh", config_path=paths["config_path"], store=store)
    assert ok is False and "disk full" in msg


@pytest.mark.parametrize("secret", ["ghp_TOP\nSECRET", "a\rb", "a\x00b", "a\tb", "a\x7fb"])
def test_upsert_rejects_secrets_with_control_characters(paths, secret):
    store = MemStore()
    ok, msg = cc.upsert_account(gh("gh"), secret, store=store, **paths)
    assert ok is False and "control" in msg.lower()
    assert store.data == {} and not paths["config_path"].exists()
    assert secret not in msg


def test_upsert_refuses_changing_an_existing_accounts_type(paths, monkeypatch):
    class FakeJira(Connector):
        type = "jira"
        label = "Jira"
        capabilities = frozenset({"issues"})
        fields = (Field("api_url", "API URL", kind="url"), Field("username", "Username"))
        secret_label = "Token"
    monkeypatch.setitem(connectors.CONNECTOR_TYPES, "jira", FakeJira)
    store = MemStore()
    cc.upsert_account(gh("gh"), "github-token", store=store, **paths)
    ok, msg = cc.upsert_account({**gh("gh"), "type": "jira"}, "", original_id="gh", store=store, **paths)
    assert ok is False and "type" in msg.lower()
    assert cc.get_account("gh", **paths)["type"] == "github"


def test_store_failure_message_is_fixed_and_carries_no_exception_detail(tmp_path):
    class FailingStore(MemStore):
        def put(self, ref, s):
            raise RuntimeError(f"security: could not add {s}")
    paths = dict(config_path=tmp_path / "c.json")
    ok, msg = cc.upsert_account(gh("gh", "GH"), "SUPERSECRET", store=FailingStore(), **paths)
    assert not ok and msg == "Could not store the secret in the Keychain"
    assert "SUPERSECRET" not in msg and "security" not in msg


# --- P2c: oauth_google types --------------------------------------------------

class FakeCalendar(Connector):
    type = "google_calendar"
    label = "Google Calendar"
    capabilities = frozenset({"calendar"})
    fields = (Field("calendar_id", "Calendar ID", default="primary"),)
    secret_label = None
    auth = "oauth_google"


@pytest.fixture
def with_calendar(monkeypatch, fake_types):
    types = dict(connectors.CONNECTOR_TYPES)
    types["google_calendar"] = FakeCalendar
    monkeypatch.setattr(connectors, "CONNECTOR_TYPES", types)


def cal(id_, label="Cal", **kw):
    return {"id": id_, "type": "google_calendar", "label": label, "calendar_id": "primary", **kw}


def test_upsert_oauth_type_needs_no_secret(paths, with_calendar):
    store = MemStore()
    ok, msg = cc.upsert_account(cal("gcal"), None, store=store, **paths)
    assert ok, msg
    assert store.data == {}
    assert cc.get_account("gcal", **paths)["settings"] == {"calendar_id": "primary"}


def test_upsert_oauth_type_rejects_pasted_secret(paths, with_calendar):
    store = MemStore()
    ok, msg = cc.upsert_account(cal("gcal"), "pasted-refresh-token", store=store, **paths)
    assert not ok and "pasted-refresh-token" not in msg
    assert store.data == {} and not paths["config_path"].exists()


def test_upsert_oauth_type_keeps_and_renames_stored_token(paths, with_calendar):
    store = MemStore()
    cc.upsert_account(cal("gcal"), None, store=store, **paths)
    store.data["gcal"] = "RT"
    ok, _ = cc.upsert_account(cal("gcal2", "Renamed"), "", original_id="gcal", store=store, **paths)
    assert ok and store.data == {"gcal2": "RT"}


def test_set_oauth_secret_stores_token_for_oauth_account(paths, with_calendar):
    store = MemStore()
    cc.upsert_account(cal("gcal"), None, store=store, **paths)
    ok, msg = cc.set_oauth_secret("gcal", "RT-SECRET", store=store, **paths)
    assert ok and store.data["gcal"] == "RT-SECRET" and "RT-SECRET" not in msg


def test_set_oauth_secret_refuses_unknown_or_non_oauth_account(paths, with_calendar):
    store = MemStore()
    cc.upsert_account(gh("gh", "GH"), "tok", store=store, **paths)
    for account_id in ("gh", "nope", "work", "me-gmail"):
        ok, msg = cc.set_oauth_secret(account_id, "RT-SECRET", store=store, **paths)
        assert not ok and "RT-SECRET" not in msg, account_id
    assert store.data == {"gh": "tok"}


def test_set_oauth_secret_keychain_failure_is_generic(paths, with_calendar):
    class Failing(MemStore):
        def put(self, ref, s):
            raise RuntimeError(f"security: could not store {s}")
    store = Failing()
    cc.upsert_account(cal("gcal"), None, store=MemStore(), **paths)
    ok, msg = cc.set_oauth_secret("gcal", "RT-SECRET", store=store, **paths)
    assert not ok and "RT-SECRET" not in msg and "Keychain" in msg


def test_set_oauth_secret_rejects_blank_or_control_char_token(paths, with_calendar):
    store = MemStore()
    cc.upsert_account(cal("gcal"), None, store=store, **paths)
    for bad in ("", "a\nb", None):
        assert cc.set_oauth_secret("gcal", bad, store=store, **paths)[0] is False
    assert store.data == {}


def test_oauth_connected_marker_set_by_sign_in_and_kept_on_edit(paths, with_calendar):
    store = MemStore()
    cc.upsert_account(cal("gcal"), None, store=store, **paths)
    assert not cc.get_account("gcal", **paths).get("oauth_connected")
    cc.set_oauth_secret("gcal", "RT", store=store, **paths)
    assert cc.get_account("gcal", **paths)["oauth_connected"] is True
    assert '"RT"' not in paths["config_path"].read_text()
    cc.upsert_account(cal("gcal", "Edited"), "", original_id="gcal", store=store, **paths)
    assert cc.get_account("gcal", **paths)["oauth_connected"] is True


def _failing_write(monkeypatch):
    def boom(entries, config_path):
        raise OSError("disk full")
    monkeypatch.setattr(cc, "_write", boom)


def test_failed_save_of_a_new_account_leaves_no_secret_behind(paths, monkeypatch):
    store = MemStore()
    _failing_write(monkeypatch)
    ok, _ = cc.upsert_account(gh("gh"), "tok", store=store, **paths)
    assert ok is False and store.data == {}


def test_failed_save_of_a_new_secret_restores_the_old_one(paths, monkeypatch):
    store = MemStore()
    cc.upsert_account(gh("gh"), "old", store=store, **paths)
    _failing_write(monkeypatch)
    ok, _ = cc.upsert_account(gh("gh"), "new", original_id="gh", store=store, **paths)
    assert ok is False and store.data == {"gh": "old"}


def test_failed_rename_moves_the_secret_back(paths, monkeypatch):
    store = MemStore()
    cc.upsert_account(gh("gh"), "tok", store=store, **paths)
    _failing_write(monkeypatch)
    ok, _ = cc.upsert_account(gh("gh-renamed"), "", original_id="gh", store=store, **paths)
    assert ok is False and store.data == {"gh": "tok"}


def test_connectors_json_temp_file_is_unique_per_write(tmp_path, monkeypatch):
    names = []
    real_open = open

    def spy(path, *a, **k):
        names.append(Path(path).name)
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", spy)
    cc._write([], tmp_path / "connectors.json")
    cc._write([], tmp_path / "connectors.json")
    tmps = [n for n in names if n.endswith(".tmp")]
    assert len(tmps) == 2 and tmps[0] != tmps[1]
