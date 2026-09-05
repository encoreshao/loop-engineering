#!/usr/bin/env python3
"""Read and record durable, per-issue task memory for a GitLab project as
real markdown files on disk: one frontmatter+body file per issue, plus a
per-project MEMORY.md index — see
docs/superpowers/specs/2026-09-02-memory-page-design.md for the format.
Unlike bin/project_memory.py (which stores learnings inline in the GitLab
cache JSON, keyed by instance/project_id), this module is keyed by project
alias and writes real files under <LOOP_ENGINEERING_HOME>/memory/<alias>/."""
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# LOOP_ENGINEERING_HOME lets dev/verification work (see CLAUDE.md's
# "Development mode") happen against a disposable sandbox instead of the
# real ~/.loop-engineering.
LOOP_ENGINEERING_HOME = Path(os.environ.get("LOOP_ENGINEERING_HOME", str(Path.home() / ".loop-engineering")))
DEFAULT_MEMORY_ROOT = LOOP_ENGINEERING_HOME / "memory"

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


def _slugify(text, max_words=6, max_len=50):
    """Kebab-case slug from the first max_words alphanumeric tokens of
    text, truncated to max_len and never ending in a hyphen. Only ASCII
    letters/digits survive (a non-ASCII word is dropped rather than
    transliterated) - the goal is a filesystem-safe filename fragment,
    not an accurate transcription."""
    words = re.findall(r"[A-Za-z0-9]+", text.lower())[:max_words]
    slug = "-".join(words)[:max_len].rstrip("-")
    return slug or "note"


def _project_dir(alias, root=None):
    if root is None:
        root = DEFAULT_MEMORY_ROOT
    return Path(root) / alias


def _issue_file(alias, issue_iid, root=None):
    """The existing task-memory file for this issue (matched by its
    <issue_iid>- filename prefix), or None if nothing's recorded yet."""
    project_dir = _project_dir(alias, root)
    if not project_dir.is_dir():
        return None
    matches = sorted(
        p for p in project_dir.glob(f"{issue_iid}-*.md") if p.name != "MEMORY.md"
    )
    return matches[0] if matches else None


def _parse_scalar(value):
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [v.strip() for v in inner.split(",") if v.strip()] if inner else []
    return value


def _parse_task_memory(text):
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    header, body = match.groups()
    fields = {"metadata": {}}
    for line in header.splitlines():
        if not line.strip():
            continue
        if line.startswith("  "):
            key, _, value = line.strip().partition(": ")
            fields["metadata"][key] = _parse_scalar(value)
        else:
            key, _, value = line.partition(": ")
            if key != "metadata":
                fields[key] = _parse_scalar(value)
    metadata = fields["metadata"]
    try:
        issue_iid = int(metadata["issue_iid"])
    except (KeyError, ValueError, TypeError):
        return None
    return {
        "name": fields.get("name", ""),
        "description": fields.get("description", ""),
        "issue_iid": issue_iid,
        "tags": metadata.get("tags") or [],
        "lesson_id": metadata.get("lesson_id") or None,
        "category": metadata.get("category") or None,
        "created_at": metadata.get("created_at") or None,
        "modified": metadata.get("modified", ""),
        "body": body.strip("\n"),
    }


def _render_task_memory(name, description, issue_iid, tags, modified, body, lesson_id, created_at, category):
    tags_literal = "[" + ", ".join(tags) + "]" if tags else "[]"
    lesson_id_str = lesson_id if lesson_id is not None else ""
    created_at_str = created_at if created_at is not None else ""
    category_str = category or ""
    header = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        "  type: project\n"
        f"  issue_iid: {issue_iid}\n"
        f"  lesson_id: {lesson_id_str}\n"
        f"  category: {category_str}\n"
        f"  tags: {tags_literal}\n"
        f"  created_at: {created_at_str}\n"
        f"  modified: {modified}\n"
        "---\n"
    )
    return f"{header}\n{body}\n"


def _summary(lesson, max_len=120):
    first_line = lesson.strip().splitlines()[0] if lesson.strip() else ""
    return first_line[:max_len].rstrip()


def _rewrite_index_line(alias, filename, description, issue_iid, root=None):
    project_dir = _project_dir(alias, root)
    index_path = project_dir / "MEMORY.md"
    slug = filename[:-3].split("-", 1)[1] if "-" in filename[:-3] else filename[:-3]
    title = f"#{issue_iid} {slug.replace('-', ' ')}"
    new_line = f"- [{title}]({filename}) — {description}\n"
    link_marker = f"]({filename})"
    lines = index_path.read_text().splitlines(keepends=True) if index_path.exists() else []
    for i, existing in enumerate(lines):
        if link_marker in existing:
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    index_path.write_text("".join(lines))


def add_task_memory(alias, issue_iid, lesson, tags=None, category=None, root=None):
    """Create or append to <issue_iid>'s task-memory file for this alias,
    and create/update that project's MEMORY.md index line for it. Returns
    {"path": Path, "lesson_id": str, "created": bool}. lesson_id and
    created_at are generated ONLY the first time a lesson_id doesn't
    already exist for this issue - either a brand-new file, or an
    existing file whose frontmatter has no lesson_id at all (a
    pre-Sprint-6 entry). Either way "created" is True and a fresh
    lesson_id/created_at is assigned. On every later append where a
    lesson_id already exists, it and created_at are preserved
    unchanged, and category is likewise preserved (a category passed
    on an append call is silently ignored - a lesson's category
    doesn't drift because a follow-up note got appended). A
    pre-Sprint-6 entry with no lesson_id stays that way forever even
    across later appends - no backfill."""
    tags = list(tags or [])
    project_dir = _project_dir(alias, root)
    project_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now[:10]
    description = _summary(lesson)
    existing_path = _issue_file(alias, issue_iid, root)
    created = False
    if existing_path is None:
        slug = _slugify(lesson)
        path = project_dir / f"{issue_iid}-{slug}.md"
        name = f"{issue_iid}-{slug}"
        body = f"Recorded {today}: {lesson.strip()}"
        lesson_id = f"lesson_{uuid.uuid4().hex}"
        created_at = now
        created = True
    else:
        path = existing_path
        parsed = _parse_task_memory(path.read_text())
        if parsed is None:
            name = path.stem
            body = path.read_text().rstrip("\n") + f"\n\nRecorded {today}: {lesson.strip()}"
            lesson_id = f"lesson_{uuid.uuid4().hex}"
            created_at = now
            created = True
        else:
            name = parsed["name"]
            tags = sorted(set(parsed["tags"]) | set(tags))
            body = parsed["body"] + f"\n\nRecorded {today}: {lesson.strip()}"
            lesson_id = parsed["lesson_id"]
            created_at = parsed["created_at"]
            category = parsed["category"]
    path.write_text(_render_task_memory(name, description, issue_iid, tags, now, body, lesson_id, created_at, category))
    _rewrite_index_line(alias, path.name, description, issue_iid, root)
    return {"path": path, "lesson_id": lesson_id, "created": created}


def get_task_memory(alias, issue_iid, root=None):
    path = _issue_file(alias, issue_iid, root)
    if path is None:
        return None
    parsed = _parse_task_memory(path.read_text())
    if parsed is None:
        return None
    parsed["path"] = str(path)
    return parsed


def list_task_memories(alias, root=None):
    project_dir = _project_dir(alias, root)
    if not project_dir.is_dir():
        return []
    entries = []
    for path in project_dir.glob("*.md"):
        if path.name == "MEMORY.md":
            continue
        parsed = _parse_task_memory(path.read_text())
        if parsed is None:
            continue
        parsed["path"] = str(path)
        entries.append(parsed)
    entries.sort(key=lambda e: e["modified"], reverse=True)
    return entries


def read_index(alias, root=None):
    index_path = _project_dir(alias, root) / "MEMORY.md"
    if not index_path.exists():
        return None
    return index_path.read_text()


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: memory_store.py add <alias> <issue_iid> <lesson> [tags_comma_separated] [category]\n"
            "       memory_store.py get <alias> <issue_iid>\n"
            "       memory_store.py list <alias>",
            file=sys.stderr,
        )
        sys.exit(1)
    command = sys.argv[1]
    if command == "add":
        alias, issue_iid, lesson = sys.argv[2], int(sys.argv[3]), sys.argv[4]
        tags = sys.argv[5].split(",") if len(sys.argv) > 5 and sys.argv[5] else None
        category = sys.argv[6] if len(sys.argv) > 6 and sys.argv[6] else None
        result = add_task_memory(alias, issue_iid, lesson, tags, category)
        print(json.dumps({
            "action": "created" if result["created"] else "updated",
            "lesson_id": result["lesson_id"],
            "path": str(result["path"]),
        }))
    elif command == "get":
        alias, issue_iid = sys.argv[2], int(sys.argv[3])
        print(json.dumps(get_task_memory(alias, issue_iid), indent=2))
    elif command == "list":
        alias = sys.argv[2]
        print(json.dumps(list_task_memories(alias), indent=2))
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
