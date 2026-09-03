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


def post_message(text, webhook_url=None, config_path=DEFAULT_CONFIG_PATH, bundle=None):
    if webhook_url is None:
        webhook_url = load_webhook_url(config_path, bundle)
    payload = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        webhook_url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status


def main():
    args = sys.argv[1:]
    bundle = None
    if args and args[0].startswith("--bundle="):
        bundle = args[0].split("=", 1)[1]
        args = args[1:]
    if len(args) < 1:
        print("Usage: slack_notify.py [--bundle=<name>] <message text>", file=sys.stderr)
        sys.exit(1)
    status = post_message(args[0], bundle=bundle)
    print(f"Slack response status: {status}")


if __name__ == "__main__":
    main()
