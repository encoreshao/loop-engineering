#!/usr/bin/env python3
"""Post a message to the loop's configured Slack incoming webhook."""
import json
import sys
import urllib.request
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".slack" / "config.json"


def load_webhook_url(config_path=DEFAULT_CONFIG_PATH, bundle=None):
    with open(config_path) as f:
        config = json.load(f)
    if bundle:
        override = config.get("bundle_webhooks", {}).get(bundle)
        if override:
            return override
    return config["webhook_url"]


def substitute_message(node, message):
    """Recursively replaces every literal "{{message}}" occurrence in any
    string value within `node` (a Block Kit blocks list, or any nested
    dict/list/string inside it) with `message`. Used both to render a
    template bound to a real alert and to preview/send a test message."""
    if isinstance(node, str):
        return node.replace("{{message}}", message)
    if isinstance(node, list):
        return [substitute_message(item, message) for item in node]
    if isinstance(node, dict):
        return {key: substitute_message(value, message) for key, value in node.items()}
    return node


def resolve_blocks(notification_key, message, config_path=DEFAULT_CONFIG_PATH):
    """Best-effort, same contract as dashboard_server.read_slack_config:
    returns None on a missing/malformed config file, a falsy
    notification_key, or no template bound to that key - never raises,
    since a broken saved template must not prevent the real alert text
    from going out as plain text."""
    if not notification_key:
        return None
    try:
        with open(config_path) as f:
            config = json.load(f)
    except (OSError, ValueError):
        return None
    for template in config.get("block_templates", {}).values():
        if template.get("notification_key") == notification_key:
            return substitute_message(template.get("blocks", []), message)
    return None


def post_message(text, webhook_url=None, config_path=DEFAULT_CONFIG_PATH, bundle=None, blocks=None):
    if webhook_url is None:
        webhook_url = load_webhook_url(config_path, bundle)
    body = {"text": text}
    if blocks:
        body["blocks"] = blocks
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        webhook_url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status


USAGE = "Usage: slack_notify.py [--bundle=<name>] [--blocks=<json blocks array>] <message text>"


def main():
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(USAGE)
        return 0
    bundle = None
    blocks = None
    positional = []
    for arg in args:
        if arg.startswith("--bundle="):
            bundle = arg.split("=", 1)[1]
        elif arg.startswith("--blocks="):
            try:
                blocks = json.loads(arg.split("=", 1)[1])
            except json.JSONDecodeError as exc:
                print(f"Invalid --blocks JSON: {exc}", file=sys.stderr)
                return 1
        else:
            positional.append(arg)
    if len(positional) < 1:
        print(USAGE, file=sys.stderr)
        return 1
    status = post_message(positional[0], bundle=bundle, blocks=blocks)
    print(f"Slack response status: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
