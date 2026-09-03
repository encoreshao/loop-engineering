#!/usr/bin/env python3
"""Read and record durable, cross-run lessons learned about a GitLab project
(fix patterns, gotchas, what not to repeat), stored in the project's GitLab
cache entry (~/.gitlab/cache/<instance>/projects/<project>/project.json,
under _memory.fix_learnings) so later runs start smarter than the last."""
import json
import sys
from pathlib import Path

GITLAB_CONFIG_SCRIPTS = Path.home() / ".encore-skills" / "skills" / "gitlab-config" / "scripts"
sys.path.insert(0, str(GITLAB_CONFIG_SCRIPTS))
import gitlab_cache  # noqa: E402

MEMORY_KEY = "fix_learnings"


def get_learnings(instance, project_id):
    """Return the list of learning entries recorded for this project, oldest
    first. Empty list if none have been recorded yet."""
    project = gitlab_cache.get_project(instance, project_id)
    return project.get("_memory", {}).get(MEMORY_KEY, [])


def add_learning(instance, project_id, lesson, issue_iid=None, tags=None):
    """Append one new learning entry and persist the full updated list."""
    learnings = get_learnings(instance, project_id)
    entry = {"lesson": lesson}
    if issue_iid is not None:
        entry["issue_iid"] = issue_iid
    if tags:
        entry["tags"] = tags
    learnings.append(entry)
    gitlab_cache.annotate_project(instance, project_id, MEMORY_KEY, learnings)
    return learnings


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: project_memory.py get <instance> <project_id>\n"
            "       project_memory.py add <instance> <project_id> <lesson> [issue_iid] [tags_comma_separated]",
            file=sys.stderr,
        )
        sys.exit(1)
    command = sys.argv[1]
    if command == "get":
        instance, project_id = sys.argv[2], sys.argv[3]
        print(json.dumps(get_learnings(instance, project_id), indent=2))
    elif command == "add":
        instance, project_id, lesson = sys.argv[2], sys.argv[3], sys.argv[4]
        issue_iid = int(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] else None
        tags = sys.argv[6].split(",") if len(sys.argv) > 6 and sys.argv[6] else None
        add_learning(instance, project_id, lesson, issue_iid, tags)
        print("OK")
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
