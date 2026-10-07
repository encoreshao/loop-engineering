#!/usr/bin/env python3
"""Release Notes loop on LoopKit: when a tracked GitLab project gets a new
tag, collects the MRs merged into the default branch since the previous tag,
has the model group them into highlights / fixes / internal, writes a
markdown file under outputs/loops/release-notes-loop/ and sends a short
summary through the loop's notifier. Read-only on GitLab. The first time a
project is seen its newest tag only becomes the baseline, so enabling the
loop never writes notes for old releases. MR text is untrusted: trimmed
before the prompt, the model runs sealed, output is sanitised and links are
always built from the offered MRs. See docs/tasks/release-notes-loop.md.
Run by run-loop-now.sh with the run id as argv[1]."""
import json
import os
import re
import sys
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import loopkit  # noqa: E402
from connectors.base import Field  # noqa: E402

MAX_MRS = 100
MAX_DESCRIPTION = 1000
MAX_ENTRY = 200
MAX_ENTRIES = 10
SECTIONS = (("highlights", "Highlights"), ("fixes", "Fixes"), ("internal", "Internal"))

SETTINGS_FIELDS = (
    Field("projects", "Projects", required=False,
          help="Comma-separated project aliases to watch; empty watches every tracked project",
          placeholder="web, api"),
)


def _err(exc):
    # Class name only: str(exc) can carry URLs or tokens.
    return type(exc).__name__


def _quote(value):
    return urllib.parse.quote(str(value), safe="")


def _parse(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _file_name(alias, tag):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{alias}-{tag}")
    return f"{safe}.md"


def _tag_date(tag):
    return _parse((tag.get("commit") or {}).get("committed_date"))


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text)
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class ReleaseNotes(loopkit.LoopPlugin):
    loop_name = "release-notes-loop"
    definition_dir = "release-notes"
    output_keys = tuple(key for key, _ in SECTIONS)
    settings_fields = SETTINGS_FIELDS

    def __init__(self, projects_fn=None, accounts_fn=None, loader=None, state_dir=None):
        self._projects_fn = projects_fn
        self._accounts_fn = accounts_fn
        self._loader = loader
        self._state_dir = state_dir

    def _resolve(self):
        import connectors_config
        from loop_plugins.pipeline_doctor import default_projects
        return (self._projects_fn or default_projects,
                self._accounts_fn or connectors_config.accounts_with_capability,
                self._loader or connectors_config.load_connector)

    def _dir(self, ctx):
        if self._state_dir is not None:
            return Path(self._state_dir)
        return Path(ctx.repo_root) / "outputs" / "loops" / self.loop_name

    def _baseline_path(self, ctx):
        return self._dir(ctx) / "tags.json"

    # --- discovery ---------------------------------------------------------

    def discover(self, ctx):
        projects_fn, accounts_fn, loader = self._resolve()
        try:
            projects = projects_fn()
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"release-notes: project config failed: {_err(exc)}")
            return []
        wanted = {a.strip() for a in str((ctx.settings or {}).get("projects") or "").split(",") if a.strip()}
        if wanted:
            projects = [p for p in projects if p.get("alias") in wanted]
        baseline = _read_json(self._baseline_path(ctx))
        new_baselines = {}
        items = []
        for account in accounts_fn("merge_requests"):
            if account.get("type") != "gitlab":
                continue
            aid = account["id"]
            mine = [p for p in projects if p.get("gitlab_instance") == aid]
            if not mine:
                continue
            try:
                conn = loader(aid)
            except Exception as exc:  # noqa: BLE001
                ctx.log(f"release-notes: account {aid} failed: {_err(exc)}")
                continue
            for project in mine:
                try:
                    item = self._project_item(conn, aid, project, baseline, new_baselines)
                except Exception as exc:  # noqa: BLE001 - one project failing never stops the rest
                    ctx.log(f"release-notes: project {project.get('alias')} failed: {_err(exc)}")
                    continue
                if item is not None:
                    items.append(item)
        if new_baselines:
            baseline.update(new_baselines)
            _write_atomic(self._baseline_path(ctx), json.dumps(baseline, indent=2) + "\n")
        return items

    def _project_item(self, conn, aid, project, baseline, new_baselines):
        pid = project["project_id"]
        tags = [t for t in conn.api("GET", f"/projects/{_quote(pid)}/repository/tags?per_page=2") or []
                if isinstance(t, dict) and t.get("name")]
        if not tags:
            return None
        newest = tags[0]
        state_key = f"{aid}:{pid}"
        if state_key not in baseline:
            new_baselines[state_key] = newest["name"]
            return None
        if baseline[state_key] == newest["name"]:
            return None
        previous = tags[1] if len(tags) > 1 else None
        info = conn.api("GET", f"/projects/{_quote(pid)}")
        branch = project.get("default_branch") or info.get("default_branch") or "main"
        new_date, prev_date = _tag_date(newest), _tag_date(previous) if previous else None
        query = {"state": "merged", "target_branch": branch, "per_page": MAX_MRS}
        if previous is not None and prev_date is not None:
            query["updated_after"] = previous["commit"]["committed_date"]
        rows = conn.api("GET", f"/projects/{_quote(pid)}/merge_requests?{urllib.parse.urlencode(query)}") or []
        mrs = []
        for row in rows:
            merged = _parse(row.get("merged_at")) if isinstance(row, dict) else None
            if merged is None or (new_date is not None and merged > new_date):
                continue
            if prev_date is not None and merged <= prev_date:
                continue
            mrs.append((merged, {
                "iid": row.get("iid"),
                "title": loopkit.chat_text(row.get("title") or "", MAX_ENTRY),
                "labels": [loopkit.chat_text(l, 60) for l in row.get("labels") or [] if isinstance(l, str)][:10],
                "author": loopkit.chat_text((row.get("author") or {}).get("username") or "", 60),
                "web_url": loopkit.chat_url(row.get("web_url") or ""),
                "description": str(row.get("description") or "")[:MAX_DESCRIPTION],
            }))
        mrs.sort(key=lambda pair: pair[0])
        alias = project["alias"]
        return loopkit.WorkItem(
            key=f"rel:{aid}:{pid}:{newest['name']}", title=f"{alias} {newest['name']}",
            url=loopkit.chat_url(f"{info.get('web_url', '')}/-/tags/{_quote(newest['name'])}"),
            payload={"project": alias, "state_key": f"{aid}:{pid}", "tag": newest["name"],
                     "tag_message": str(newest.get("message") or "")[:MAX_DESCRIPTION],
                     "previous_tag": previous["name"] if previous else "",
                     "mrs": [m for _, m in mrs[:MAX_MRS]]})

    # --- answer ------------------------------------------------------------

    def after_item(self, item, answer, ctx):
        urls = {m["iid"]: m["web_url"] for m in item.payload["mrs"]}
        notes = {}
        for key, _ in SECTIONS:
            entries = []
            for entry in answer.get(key) or []:
                if not isinstance(entry, dict) or not entry.get("text"):
                    continue
                iid = entry.get("mr") if entry.get("mr") in urls else None
                entries.append({"text": loopkit.chat_text(entry["text"], MAX_ENTRY), "mr": iid,
                                "url": urls.get(iid, "") if iid is not None else ""})
            notes[key] = entries[:MAX_ENTRIES]
        project, tag = item.payload["project"], item.payload["tag"]
        lines = [f"# {project} {tag}"]
        for key, heading in SECTIONS:
            lines += ["", f"## {heading}", ""]
            for e in notes[key]:
                ref = f" ([!{e['mr']}]({e['url']}))" if e["url"] else ""
                lines.append(f"- {e['text']}{ref}")
        path = self._dir(ctx) / _file_name(project, tag)
        _write_atomic(path, "\n".join(lines) + "\n")
        baseline = _read_json(self._baseline_path(ctx))
        baseline[item.payload["state_key"]] = tag
        _write_atomic(self._baseline_path(ctx), json.dumps(baseline, indent=2) + "\n")
        counts = ", ".join(f"{len(notes[k])} {k}" for k, _ in SECTIONS)
        return loopkit.Outcome(item.key, "done", counts, url=item.url,
                               data={"title": item.title, "notes": notes, "path": str(path)})

    def digest(self, outcomes, ctx):
        blocks = []
        for o in outcomes:
            if o.status != "done" or "notes" not in o.data:
                continue
            notes = o.data["notes"]
            lines = [f"Release notes: {loopkit.chat_text(o.data['title'], MAX_ENTRY)} - "
                     f"{len(notes['highlights'])} highlights, {len(notes['fixes'])} fixes, "
                     f"{len(notes['internal'])} internal"]
            lines += [f"- {loopkit.chat_link(e['text'], e['url'])}" for e in notes["highlights"]]
            if o.url:
                lines.append(f"Tag: {o.url}")
            lines.append(f"Notes: {loopkit.chat_text(o.data['path'], 300)}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks) if blocks else None


if __name__ == "__main__":
    sys.exit(loopkit.main(ReleaseNotes()))
