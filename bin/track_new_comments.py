#!/usr/bin/env python3
"""Detect which notes on a cached GitLab issue are new since the loop last looked,
using an annotation stored in the existing local GitLab cache
(~/.gitlab/cache/<instance>/projects/<project>/issues/<iid>.json)."""
import json
import sys
from pathlib import Path

GITLAB_CONFIG_SCRIPTS = Path.home() / ".encore-skills" / "skills" / "gitlab-config" / "scripts"
sys.path.insert(0, str(GITLAB_CONFIG_SCRIPTS))
import gitlab_cache  # noqa: E402

LAST_SEEN_KEY = "loop_last_seen_note_id"


def get_new_notes(instance, project_id, issue_iid, exclude_author=None):
    """Return notes whose id is greater than the last-seen note id recorded for
    this issue. If there is no cached issue or no prior annotation, every note
    currently in the cache counts as new.

    Pass `exclude_author` (a GitLab username) to drop notes written by that
    user. The loop passes its own assignee username here so it never treats
    its own escalation comments from a previous run as new input to react to.
    """
    cache = gitlab_cache.get_issue_cache(instance, project_id, issue_iid)
    if not cache:
        return []
    last_seen = int(cache.get("_notes", {}).get(LAST_SEEN_KEY, 0))
    notes = cache.get("notes", [])
    new_notes = [n for n in notes if int(n["id"]) > last_seen]
    if exclude_author:
        new_notes = [n for n in new_notes if n.get("author", {}).get("username") != exclude_author]
    return new_notes


def mark_notes_seen(instance, project_id, issue_iid, notes):
    """Advance the last-seen note id annotation past the given notes. No-op if
    `notes` is empty."""
    if not notes:
        return
    max_id = max(int(n["id"]) for n in notes)
    gitlab_cache.annotate_issue(instance, project_id, issue_iid, LAST_SEEN_KEY, str(max_id))


def main():
    if len(sys.argv) < 4:
        print(
            "Usage: track_new_comments.py <instance> <project_id> <issue_iid> [exclude_author]\n"
            "       track_new_comments.py mark-seen <instance> <project_id> <issue_iid>",
            file=sys.stderr,
        )
        sys.exit(1)
    # `mark-seen` advances the watermark past everything currently unseen.
    # get_new_notes already returns exactly "everything not yet marked seen",
    # so marking all of it seen is the whole operation. Deliberately no
    # exclude_author here: notes the loop wrote itself still need their ids
    # consumed, otherwise the watermark would stall behind them forever.
    if sys.argv[1] == "mark-seen":
        if len(sys.argv) < 5:
            print(
                "Usage: track_new_comments.py mark-seen <instance> <project_id> <issue_iid>",
                file=sys.stderr,
            )
            sys.exit(1)
        instance, project_id, issue_iid = sys.argv[2], sys.argv[3], sys.argv[4]
        notes = get_new_notes(instance, project_id, issue_iid)
        mark_notes_seen(instance, project_id, issue_iid, notes)
        print("OK")
        return
    instance, project_id, issue_iid = sys.argv[1], sys.argv[2], sys.argv[3]
    exclude_author = sys.argv[4] if len(sys.argv) > 4 else None
    print(json.dumps(get_new_notes(instance, project_id, issue_iid, exclude_author), indent=2))


if __name__ == "__main__":
    main()
