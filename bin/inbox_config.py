#!/usr/bin/env python3
"""Load/save the Inbox Triage loop's per-machine config:
~/.loop-engineering/inboxes.json (which mailboxes to triage, and how) and
~/.loop-engineering/mail_oauth.json (the one-time OAuth app credentials the
setup wizard stores). See
docs/superpowers/specs/2026-09-27-inbox-triage-design.md. Refresh tokens are
NOT here - they live in the macOS Keychain (bin/mail_auth.py)."""
import email.utils
import json
import os
import re
from pathlib import Path

# LOOP_ENGINEERING_HOME lets dev/verification work (see CLAUDE.md's
# "Development mode" section) point this at a sandbox directory instead of
# the real, possibly-live ~/.loop-engineering.
LOOP_ENGINEERING_HOME = Path(os.environ.get("LOOP_ENGINEERING_HOME", str(Path.home() / ".loop-engineering")))
DEFAULT_CONFIG_PATH = LOOP_ENGINEERING_HOME / "inboxes.json"
DEFAULT_OAUTH_PATH = LOOP_ENGINEERING_HOME / "mail_oauth.json"

PROVIDERS = ("gmail", "outlook")
OAUTH_PROVIDERS = ("google", "microsoft")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

DEFAULT_CATEGORIES = [
    {"key": "urgent", "label": "Loop/Urgent", "draft": True,
     "description": "Needs a response today: a direct ask from a real person with a deadline, a blocker, or a client/boss escalation."},
    {"key": "action", "label": "Loop/Action", "draft": False,
     "description": "Needs a reply or task from the user, but not today."},
    {"key": "fyi", "label": "Loop/FYI", "draft": False,
     "description": "Worth reading, no action: updates, CCs, announcements."},
    {"key": "notifications", "label": "Loop/Notifications", "draft": False,
     "description": "Automated mail: GitLab, CI, calendar, SaaS alerts."},
    {"key": "newsletters", "label": "Loop/Newsletters", "draft": False,
     "description": "Marketing, digests, subscriptions."},
]


def parse_address(header_value):
    return email.utils.parseaddr(header_value or "")[1].strip().lower()


def sender_matches(address, patterns):
    address = (address or "").strip().lower()
    if not address:
        return False
    for pattern in patterns or []:
        pattern = pattern.strip().lower()
        if not pattern:
            continue
        if pattern.startswith("@"):
            if address.endswith(pattern):
                return True
        elif address == pattern:
            return True
    return False


def validate_inbox(inbox):
    errors = []
    name = inbox.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        errors.append("name must be a lowercase slug (a-z, 0-9, -)")
    if inbox.get("provider") not in PROVIDERS:
        errors.append(f"provider must be one of {', '.join(PROVIDERS)}")
    if not isinstance(inbox.get("account"), str) or not _EMAIL_RE.match(inbox.get("account", "")):
        errors.append("account must be an email address")
    if not isinstance(inbox.get("label"), str) or not inbox.get("label", "").strip():
        errors.append("label is required")
    for key in ("vip_senders", "exclude_senders"):
        if not isinstance(inbox.get(key, []), list):
            errors.append(f"{key} must be a list")
    categories = inbox.get("categories")
    if categories is not None:
        if not isinstance(categories, list):
            errors.append("categories must be null or a list")
        elif not any(isinstance(c, dict) and c.get("key") == "urgent" for c in categories):
            errors.append("categories must include an 'urgent' category - VIP senders are always marked urgent")
    return errors


def _validate_config(data, path):
    if not isinstance(data, dict) or not isinstance(data.get("inboxes"), list):
        raise ValueError(f"{path} must be a JSON object with an 'inboxes' array")
    if "default_categories" not in data:
        data["default_categories"] = [dict(c) for c in DEFAULT_CATEGORIES]
    seen = set()
    for inbox in data["inboxes"]:
        errors = validate_inbox(inbox)
        if errors:
            raise ValueError(f"{path}: inbox {inbox.get('name')!r}: {'; '.join(errors)}")
        if inbox["name"] in seen:
            raise ValueError(f"{path}: duplicate inbox name {inbox['name']!r}")
        seen.add(inbox["name"])
    return data


def load_config(config_path=None):
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"No inbox config at {path}. Copy config/inboxes.json.template there, "
            f"or add an inbox from the dashboard's Inbox Triage setup page."
        )
    with open(path) as f:
        return _validate_config(json.load(f), path)


def load_config_or_empty(config_path=None):
    try:
        return load_config(config_path)
    except FileNotFoundError:
        return {"default_categories": [dict(c) for c in DEFAULT_CATEGORIES], "inboxes": []}


def categories_for(inbox, config):
    result = inbox.get("categories") or config.get("default_categories") or DEFAULT_CATEGORIES
    if result is DEFAULT_CATEGORIES:
        return [dict(c) for c in DEFAULT_CATEGORIES]
    return result


def get_inbox(name, config_path=None):
    for inbox in load_config(config_path)["inboxes"]:
        if inbox["name"] == name:
            return inbox
    raise KeyError(f"No inbox named {name!r}")


def enabled_inboxes(config_path=None):
    return [i for i in load_config(config_path)["inboxes"] if i.get("enabled", True)]


def _write_json(data, path, mode=None):
    """Atomic write (temp file + os.replace), same as topic_config._write_topics."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    if mode is not None:
        os.chmod(tmp, mode)
    tmp.replace(path)


def upsert_inbox(fields, is_new, config_path=None):
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    inbox = {
        "name": (fields.get("name") or "").strip(),
        "label": (fields.get("label") or "").strip(),
        "provider": (fields.get("provider") or "").strip(),
        "account": (fields.get("account") or "").strip(),
        "enabled": bool(fields.get("enabled", True)),
        "urgent_brief": (fields.get("urgent_brief") or "").strip(),
        "vip_senders": list(fields.get("vip_senders") or []),
        "exclude_senders": list(fields.get("exclude_senders") or []),
        "categories": fields.get("categories"),
        "slack_bundle": (fields.get("slack_bundle") or "").strip() or None,
    }
    errors = validate_inbox(inbox)
    if errors:
        return False, "; ".join(errors)
    config = load_config_or_empty(config_path)
    existing = {i["name"]: i for i in config["inboxes"]}
    if is_new:
        if inbox["name"] in existing:
            return False, f"An inbox named {inbox['name']!r} already exists"
        config["inboxes"].append(inbox)
    else:
        if inbox["name"] not in existing:
            return False, "Inbox names cannot be renamed after creation - delete and re-add instead"
        inbox["enabled"] = existing[inbox["name"]].get("enabled", True)
        config["inboxes"] = [inbox if i["name"] == inbox["name"] else i for i in config["inboxes"]]
    _write_json(config, config_path)
    return True, f"Saved inbox {inbox['label']}"


def delete_inbox(name, config_path=None):
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    config = load_config_or_empty(config_path)
    remaining = [i for i in config["inboxes"] if i["name"] != name]
    if len(remaining) == len(config["inboxes"]):
        return False, f"No inbox named {name!r}"
    config["inboxes"] = remaining
    _write_json(config, config_path)
    return True, f"Deleted inbox {name}"


def set_enabled(name, enabled, config_path=None):
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    config = load_config_or_empty(config_path)
    for inbox in config["inboxes"]:
        if inbox["name"] == name:
            inbox["enabled"] = bool(enabled)
            _write_json(config, config_path)
            return True, f"{inbox['label']} {'resumed' if enabled else 'paused'}"
    return False, f"No inbox named {name!r}"


def load_oauth(oauth_path=None):
    if oauth_path is None:
        oauth_path = DEFAULT_OAUTH_PATH
    path = Path(oauth_path)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_oauth_client(provider, client_id, client_secret="", oauth_path=None):
    if oauth_path is None:
        oauth_path = DEFAULT_OAUTH_PATH
    if provider not in OAUTH_PROVIDERS:
        return False, f"Unknown provider {provider!r}"
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not client_id:
        return False, "Client ID is required"
    if provider == "google" and not client_secret:
        return False, "Google also needs its client secret"
    data = load_oauth(oauth_path)
    data[provider] = {"client_id": client_id}
    if provider == "google":
        data[provider]["client_secret"] = client_secret
    _write_json(data, oauth_path, mode=0o600)
    return True, f"Saved {provider.title()} OAuth client"
