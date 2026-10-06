#!/usr/bin/env python3
"""Connector account registry: ~/.loop-engineering/connectors.json holds the
native accounts (no secrets - those live in the Keychain via secret_store,
keyed by the connector id). GitLab instances (~/.gitlab/config.json), Slack
webhooks (~/.slack/config.json) and mailboxes (inboxes.json) are read through
as external, read-only accounts that their own pages keep managing.
Nothing returned by list_accounts/get_account ever contains a secret."""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import connectors
import i18n

LOOP_ENGINEERING_HOME = Path(os.environ.get("LOOP_ENGINEERING_HOME", str(Path.home() / ".loop-engineering")))
DEFAULT_CONFIG_PATH = LOOP_ENGINEERING_HOME / "connectors.json"

NATIVE = "native"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


class ConnectorConfigError(Exception):
    pass


def is_valid_id(account_id):
    """True for an id a native account may have (and loops' `notify` may list)."""
    return isinstance(account_id, str) and bool(_ID_RE.match(account_id))


def _str(value):
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _read_json(path, default):
    """default if the file is missing; ConnectorConfigError if unreadable."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise ConnectorConfigError(f"Could not read {path}: {exc}") from exc


def _read_external(path):
    """Best-effort: external files are owned by other pages, so a broken one
    just contributes no accounts rather than breaking the registry."""
    try:
        data = _read_json(path, {})
    except ConnectorConfigError:
        return {}
    return data if isinstance(data, dict) else {}


def _resolve(config_path, gitlab_config_path, slack_config_path, inbox_config_path):
    import inbox_config
    return (
        Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH,
        Path(gitlab_config_path) if gitlab_config_path is not None else Path.home() / ".gitlab" / "config.json",
        Path(slack_config_path) if slack_config_path is not None else Path.home() / ".slack" / "config.json",
        Path(inbox_config_path) if inbox_config_path is not None else inbox_config.DEFAULT_CONFIG_PATH,
    )


def _read_native_raw(config_path):
    data = _read_json(config_path, [])
    if not isinstance(data, list):
        raise ConnectorConfigError(f"{config_path} must contain a JSON list")
    return [e for e in data if isinstance(e, dict) and isinstance(e.get("id"), str)]


def _native_view(entry):
    settings = entry.get("settings")
    settings = settings if isinstance(settings, dict) else {}
    return {
        "id": entry["id"],
        "type": _str(entry.get("type")),
        "label": _str(entry.get("label")) or entry["id"],
        "enabled": entry.get("enabled", True) is not False,
        "settings": {_str(k): _str(v) for k, v in settings.items()},
        "managed_by": NATIVE,
    }


def _external(gitlab_path, slack_path, inbox_path):
    accounts = []
    instances = _read_external(gitlab_path).get("instances")
    for name, inst in (instances.items() if isinstance(instances, dict) else []):
        if isinstance(inst, dict):
            accounts.append({"id": _str(name), "type": "gitlab", "label": _str(name), "enabled": True,
                             "settings": {"url": _str(inst.get("url"))}, "managed_by": "gitlab-config"})
    slack = _read_external(slack_path)
    if slack.get("webhook_url"):
        accounts.append({"id": "slack-default", "type": "slack", "label": "Slack", "enabled": True,
                         "settings": {}, "managed_by": "slack-config"})
    bundles = slack.get("bundle_webhooks")
    for bundle in (bundles if isinstance(bundles, dict) else {}):
        accounts.append({"id": f"slack-{bundle}", "type": "slack", "label": f"Slack ({bundle})", "enabled": True,
                         "settings": {}, "managed_by": "slack-config"})
    inboxes = _read_external(inbox_path).get("inboxes")
    for box in (inboxes if isinstance(inboxes, list) else []):
        if isinstance(box, dict) and isinstance(box.get("name"), str):
            accounts.append({"id": box["name"], "type": "mailbox", "label": _str(box.get("label")) or box["name"],
                             "enabled": True,
                             "settings": {"provider": _str(box.get("provider")), "account": _str(box.get("account"))},
                             "managed_by": "inboxes"})
    return accounts


def list_accounts(config_path=None, gitlab_config_path=None, slack_config_path=None, inbox_config_path=None):
    config_path, gl, sl, ib = _resolve(config_path, gitlab_config_path, slack_config_path, inbox_config_path)
    native = [_native_view(e) for e in _read_native_raw(config_path)]
    return native + _external(gl, sl, ib)


def get_account(account_id, **paths):
    for account in list_accounts(**paths):
        if account["id"] == account_id:
            return account
    raise KeyError(account_id)


def accounts_with_capability(capability, **paths):
    found = []
    for account in list_accounts(**paths):
        if not account["enabled"]:
            continue
        try:
            caps = connectors.get_type(account["type"]).capabilities
        except KeyError:
            continue
        if capability in caps:
            found.append(account)
    return found


def _store(store):
    if store is None:
        import secret_store
        return secret_store
    return store


def _write(entries, config_path):
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_name(f".{config_path.name}.tmp")
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2)
    tmp.replace(config_path)


def upsert_account(fields, secret, original_id="", config_path=None, store=None, **paths):
    config_path, gl, sl, ib = _resolve(config_path, paths.get("gitlab_config_path"),
                                       paths.get("slack_config_path"), paths.get("inbox_config_path"))
    if any(v is not None and not isinstance(v, str) and not (k == "enabled" and isinstance(v, bool)) for k, v in fields.items()):
        return False, i18n.t("All connector fields must be text")
    new_id = (fields.get("id") or "").strip()
    type_name = (fields.get("type") or "").strip()
    label = (fields.get("label") or "").strip()
    original_id = (original_id or "").strip()
    if not _ID_RE.match(new_id):
        return False, i18n.t("Connector id must be lowercase letters, digits and dashes")
    try:
        cls = connectors.get_type(type_name)
    except KeyError:
        return False, i18n.t("Unknown connector type {type}", type=type_name)
    if cls.external:
        return False, i18n.t("{type} accounts are managed on their own page", type=type_name)
    if not label:
        return False, i18n.t("{field} is required", field=i18n.t("Label"))
    try:
        entries = _read_native_raw(config_path)
    except ConnectorConfigError as exc:
        return False, i18n.t("Could not read connectors: {detail}", detail=exc)
    existing = next((e for e in entries if e["id"] == original_id), None) if original_id else None
    if original_id and existing is None:
        return False, i18n.t("No connector with id {id}", id=original_id)
    if existing is not None and _str(existing.get("type")) != type_name:
        # The stored secret belongs to the old type's service; reusing it for
        # another type would send e.g. a GitHub token to a Jira site.
        return False, i18n.t("Connector {id} is a {type} account; its type cannot be changed",
                             id=original_id, type=_str(existing.get("type")))
    external_ids = {a["id"] for a in _external(gl, sl, ib)}
    taken = {e["id"] for e in entries if e is not existing} | external_ids
    if new_id in taken:
        return False, i18n.t("Connector id {id} is already used by another account", id=new_id)
    settings = {f.key: (fields.get(f.key) or "").strip() for f in cls.fields}
    secret = (secret or "").strip()
    # A control character would end up inside an HTTP header value, where
    # http.client raises ValueError quoting the whole header - secret and all.
    if any(ord(c) < 32 or ord(c) == 127 for c in secret):
        return False, i18n.t("The secret must not contain control characters or line breaks")
    errors = cls.validate(settings, secret, existing is None)
    if errors:
        return False, "; ".join(errors)
    enabled = fields["enabled"] if isinstance(fields.get("enabled"), bool) else (
        existing.get("enabled", True) is not False if existing else True)
    entry = {"id": new_id, "type": type_name, "label": label, "enabled": enabled, "settings": settings,
             "created_at": (existing or {}).get("created_at")
             or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    secrets = _store(store)
    try:
        if secret:
            secrets.put(new_id, secret)
            if existing and original_id != new_id:
                secrets.delete(original_id)
        elif existing and original_id != new_id:
            old = secrets.get(original_id)
            if old:
                secrets.put(new_id, old)
            secrets.delete(original_id)
    except Exception:
        # No exception detail: Keychain stderr can echo the secret.
        return False, i18n.t("Could not store the secret in the Keychain")
    if existing is not None:
        entries = [entry if e is existing else e for e in entries]
    else:
        entries.append(entry)
    try:
        _write(entries, config_path)
    except OSError as exc:
        return False, i18n.t("Could not save connectors: {detail}", detail=exc)
    return True, i18n.t("Saved connector {id}", id=new_id)


def delete_account(account_id, config_path=None, store=None):
    config_path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    try:
        entries = _read_native_raw(config_path)
    except ConnectorConfigError as exc:
        return False, i18n.t("Could not read connectors: {detail}", detail=exc)
    if not any(e["id"] == account_id for e in entries):
        return False, i18n.t("Only connectors created here can be deleted; manage {id} on its own page", id=account_id)
    try:
        _store(store).delete(account_id)
    except Exception as exc:
        return False, i18n.t("Could not remove the stored secret: {detail}", detail=exc)
    try:
        _write([e for e in entries if e["id"] != account_id], config_path)
    except OSError as exc:
        return False, i18n.t("Could not save connectors: {detail}", detail=exc)
    return True, i18n.t("Deleted connector {id}", id=account_id)


def load_connector(account_id, store=None, **paths):
    account = get_account(account_id, **paths)
    cls = connectors.get_type(account["type"])
    managed = account["managed_by"]
    secret = None
    _, gl, sl, _ = _resolve(None, paths.get("gitlab_config_path"), paths.get("slack_config_path"), None)
    if managed == NATIVE:
        secret = _store(store).get(account_id)
    elif managed == "gitlab-config":
        inst = _read_external(gl).get("instances", {}).get(account_id)
        secret = inst.get("token") if isinstance(inst, dict) else None
    elif managed == "slack-config":
        slack = _read_external(sl)
        if account_id == "slack-default":
            secret = slack.get("webhook_url")
        else:
            secret = (slack.get("bundle_webhooks") or {}).get(account_id[len("slack-"):])
    return cls(account, secret)
