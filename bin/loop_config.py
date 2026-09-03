#!/usr/bin/env python3
"""Load this loop's per-machine project configuration from
~/.loop-engineering/projects.json (see config/projects.json.template in
this repo). Local checkout paths, the GitLab username to track, and the
worktree scratch directory all live here instead of being hardcoded, since
every team member running this loop has their own local paths."""
import json
import os
import sys
from pathlib import Path

# LOOP_ENGINEERING_HOME lets dev/verification work (see CLAUDE.md's
# "Development mode" section) point this at a sandbox directory instead of
# the real, possibly-live ~/.loop-engineering.
LOOP_ENGINEERING_HOME = Path(os.environ.get("LOOP_ENGINEERING_HOME", str(Path.home() / ".loop-engineering")))
DEFAULT_CONFIG_PATH = LOOP_ENGINEERING_HOME / "projects.json"


def load_config(config_path=DEFAULT_CONFIG_PATH):
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"No project config at {path}. Copy config/projects.json.template "
            f"there and fill in your own local checkout paths, GitLab username, and worktree root."
        )
    with open(path) as f:
        return json.load(f)


def list_aliases(config_path=DEFAULT_CONFIG_PATH):
    return list(load_config(config_path)["projects"].keys())


def get_project(alias, config_path=DEFAULT_CONFIG_PATH):
    """A project's own config, with `instance` always resolved and present:
    the project's own `"instance"` override if it set one, else the config's
    top-level `gitlab_instance` default - so callers never need to
    separately fall back to the global value themselves."""
    config = load_config(config_path)
    project = dict(config["projects"][alias])
    project.setdefault("instance", config["gitlab_instance"])
    return project


def get_instance(config_path=DEFAULT_CONFIG_PATH):
    return load_config(config_path)["gitlab_instance"]


def get_assignee_username(config_path=DEFAULT_CONFIG_PATH):
    return load_config(config_path)["assignee_username"]


def get_worktree_root(config_path=DEFAULT_CONFIG_PATH):
    return load_config(config_path)["worktree_root"]


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: loop_config.py <aliases|local-paths|worktree-root|assignee|instance>\n"
            "       loop_config.py project <alias>",
            file=sys.stderr,
        )
        sys.exit(1)
    command = sys.argv[1]
    if command == "aliases":
        print("\n".join(list_aliases()))
    elif command == "local-paths":
        config = load_config()
        print("\n".join(p["local_path"] for p in config["projects"].values()))
    elif command == "worktree-root":
        print(get_worktree_root())
    elif command == "assignee":
        print(get_assignee_username())
    elif command == "instance":
        print(get_instance())
    elif command == "project":
        if len(sys.argv) < 3:
            print("Usage: loop_config.py project <alias>", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(get_project(sys.argv[2]), indent=2))
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
