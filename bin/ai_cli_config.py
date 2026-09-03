#!/usr/bin/env python3
"""Load/save which AI CLI (claude or codex) this loop should use, from
~/.loop-engineering/ai_cli.json (see config/ai_cli.json.template in this
repo). run-loop.sh and run-topic-monitor-loop.sh both shell out to this
module (`python3 bin/ai_cli_config.py get`) to decide which binary and
flags to invoke - see docs/superpowers/specs/2026-08-28-ai-cli-switcher-design.md
for why the two CLIs' safety models differ and what that means for the
scheduled loops."""
import json
import os
import sys
from pathlib import Path

# LOOP_ENGINEERING_HOME lets dev/verification work (see CLAUDE.md's
# "Development mode" section) point this at a sandbox directory instead of
# the real, possibly-live ~/.loop-engineering.
LOOP_ENGINEERING_HOME = Path(os.environ.get("LOOP_ENGINEERING_HOME", str(Path.home() / ".loop-engineering")))
DEFAULT_CONFIG_PATH = LOOP_ENGINEERING_HOME / "ai_cli.json"

VALID_CLIS = ("claude", "codex")


def load_config(config_path=DEFAULT_CONFIG_PATH):
    path = Path(config_path)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def get_selected_cli(config_path=DEFAULT_CONFIG_PATH):
    """The stored "cli" value if it's a recognized one, else "claude" -
    the CLI whose guardrails are harness-enforced rather than prose-only,
    so a missing/corrupt/unrecognized config file always fails safe."""
    cli = load_config(config_path).get("cli")
    return cli if cli in VALID_CLIS else "claude"


def set_selected_cli(cli, config_path=DEFAULT_CONFIG_PATH):
    if cli not in VALID_CLIS:
        return False, f"Unknown AI CLI: {cli}"
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"cli": cli}, f, indent=2)
    return True, f"Switched to {cli}"


def main():
    if len(sys.argv) < 2:
        print("Usage: ai_cli_config.py get\n       ai_cli_config.py set <claude|codex>", file=sys.stderr)
        sys.exit(1)
    command = sys.argv[1]
    if command == "get":
        print(get_selected_cli())
    elif command == "set":
        if len(sys.argv) < 3:
            print("Usage: ai_cli_config.py set <claude|codex>", file=sys.stderr)
            sys.exit(1)
        ok, message = set_selected_cli(sys.argv[2])
        print(message, file=sys.stderr if not ok else sys.stdout)
        if not ok:
            sys.exit(1)
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
