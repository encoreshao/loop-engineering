#!/usr/bin/env python3
"""List open GitLab issues assigned to a given username across configured project aliases.
With no CLI arguments, both the username and the project list default to
~/.loop-engineering/projects.json (see loop_config.py)."""
import json
import subprocess
import sys
from pathlib import Path

import loop_config

GITLAB_API = Path.home() / ".encore-skills" / "skills" / "gitlab-config" / "scripts" / "gitlab_api.py"


def fetch_open_issues(project_alias, gitlab_api_path=GITLAB_API):
    """Call gitlab_api.py list-issues <project_alias> opened and return the parsed JSON array."""
    result = subprocess.run(
        [sys.executable, str(gitlab_api_path), "list-issues", project_alias, "opened"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def filter_assigned_to(issues, username):
    """Keep only issues where `username` appears in the issue's assignees list."""
    return [
        issue for issue in issues
        if any(a.get("username") == username for a in issue.get("assignees", []))
    ]


def list_assigned_issues(project_aliases, username, gitlab_api_path=GITLAB_API):
    """Return {project_alias: [issue, ...]} for open issues assigned to `username`."""
    result = {}
    for alias in project_aliases:
        issues = fetch_open_issues(alias, gitlab_api_path)
        result[alias] = filter_assigned_to(issues, username)
    return result


def main():
    if len(sys.argv) >= 3:
        username = sys.argv[1]
        project_aliases = sys.argv[2:]
    elif len(sys.argv) == 1:
        username = loop_config.get_assignee_username()
        project_aliases = loop_config.list_aliases()
    else:
        print(
            "Usage: list_assigned_issues.py [username] [project_alias...] "
            "(with no arguments, both default from ~/.loop-engineering/projects.json)",
            file=sys.stderr,
        )
        sys.exit(1)
    result = list_assigned_issues(project_aliases, username)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
