#!/usr/bin/env python3
"""MR Review loop on LoopKit: pre-reviews GitLab merge requests where the
user is a reviewer and leaves the findings as *draft* review notes only. The
user publishes them from the MR (Review -> Submit). This plugin never calls
bulk_publish, approve, or the published /notes endpoint. Run by
run-loop-now.sh with the run id as argv[1] (--force re-reviews)."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import loopkit  # noqa: E402
import mail_http  # noqa: E402
import seen_store  # noqa: E402

DIFF_CAP_BYTES = 150_000
MAX_FINDINGS = 15
MAX_DESCRIPTION = 4096
MAX_BODY = 2000
MAX_SUMMARY = 600
_MAX_DIFF_PAGES = 20
_MAX_DRAFT_PAGES = 20
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_SEVERITIES = ("nit", "minor", "major", "blocker")
_SIGNATURE = "\n\n_— Loop X pre-review_"
_MENTION = re.compile(r"(?<![\w@])@")
_DENIED_MESSAGE = "token needs the api scope to write draft notes"


class DraftWriteDenied(Exception):
    """GitLab refused a draft-note write with 401/403 (read-only token)."""


def item_key(account_id, project_id, iid, head_sha):
    return f"mr:{account_id}:{project_id}!{iid}@{head_sha}"


def _err(exc):
    # Class name only: str(exc) can carry URLs or tokens.
    return type(exc).__name__


def build_diff_text(changes, cap=DIFF_CAP_BYTES):
    parts, size, truncated = [], 0, False
    for ch in changes:
        path = str(ch.get("new_path") or ch.get("old_path") or "")
        chunk = f"--- a/{path}\n+++ b/{path}\n{ch.get('diff') or ''}\n"
        if size + len(chunk) > cap:
            parts.append(chunk[:max(0, cap - size)])
            truncated = True
            break
        parts.append(chunk)
        size += len(chunk)
    text = "".join(parts)
    if truncated:
        text += f"\n[diff truncated at {cap} characters]"
    return text, truncated


def _defuse(text):
    """Draft notes are Markdown on GitLab: break word-start @mentions so a
    model-echoed @all / @group cannot notify anyone."""
    return _MENTION.sub("@\u200b", str(text)).replace("![", "!\u200b[")


def _drafts_path(mr):
    return f"/projects/{mr['project_id']}/merge_requests/{mr['iid']}/draft_notes"


def _post_draft(conn, mr, note, position=None):
    body = {"note": note}
    if position is not None:
        body["position"] = position
    # Non-idempotent: never auto-retry (a timeout after creation would duplicate it).
    return conn.api("POST", _drafts_path(mr), json_body=body, max_attempts=1)


def _clear_old_drafts(conn, mr):
    """Delete this loop's own earlier drafts (partial runs, older SHAs),
    across every page of drafts. Drafts without the Loop X signature are
    never touched. All pages are listed before deleting, so a deletion never
    shifts a later page."""
    ours = []
    for page in range(1, _MAX_DRAFT_PAGES + 1):
        rows = conn.api("GET", _drafts_path(mr) + f"?per_page=100&page={page}")
        rows = rows if isinstance(rows, list) else []
        for row in rows:
            if isinstance(row, dict) and row.get("id") is not None and str(row.get("note") or "").rstrip().endswith(_SIGNATURE.strip()):
                ours.append(int(row["id"]))
        if len(rows) < 100:
            break
    for draft_id in ours:
        conn.api("DELETE", f"{_drafts_path(mr)}/{draft_id}", max_attempts=1)


def diff_line_map(diff_text):
    """{path: {new_line: old_line}} for the unchanged context lines of each
    file in build_diff_text's output. Added lines are absent (they have no
    old line); GitLab needs old_line as well as new_line to anchor a note on
    a context line."""
    out, path, old, new = {}, None, 0, 0
    for line in str(diff_text or "").splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            out.setdefault(path, {})
            continue
        if line.startswith("--- a/"):
            continue
        m = _HUNK.match(line)
        if m:
            old, new = int(m.group(1)), int(m.group(2))
            continue
        if path is None or not old and not new:
            continue
        if line.startswith("+"):
            new += 1
        elif line.startswith("-"):
            old += 1
        elif line.startswith(" "):  # GitLab sends an empty context line as " "
            out[path][new] = old
            old += 1
            new += 1
    return out


def post_review(conn, mr, answer):
    """Returns {"drafts", "fallback_general"} (+ "partial": ExcClass when a
    failure hit after at least one draft was posted). Raises DraftWriteDenied
    on 401/403, and the original error when nothing was posted."""
    refs = mr["diff_refs"]
    context_lines = diff_line_map(mr.get("diff"))
    old_paths = mr.get("old_paths") or {}
    drafts = fallback = 0
    try:
        _clear_old_drafts(conn, mr)
        for f in answer.get("findings") or []:
            note = f"**[{f['severity']}]** {_defuse(f['body'])}{_SIGNATURE}"
            position = {"position_type": "text", "base_sha": refs["base_sha"], "start_sha": refs["start_sha"],
                        "head_sha": refs["head_sha"], "old_path": old_paths.get(f["path"], f["path"]),
                        "new_path": f["path"], "new_line": f["line"]}
            old_line = context_lines.get(f["path"], {}).get(f["line"])
            if old_line is not None:
                position["old_line"] = old_line
            try:
                _post_draft(conn, mr, note, position)
            except mail_http.MailHTTPError as exc:
                if exc.status not in (400, 422):
                    raise
                _post_draft(conn, mr, f"`{f['path']}:{f['line']}` {note}")
                fallback += 1
            drafts += 1
        summary = _defuse(str(answer.get("summary") or "").strip())
        if summary:
            _post_draft(conn, mr, f"**Summary:** {summary}{_SIGNATURE}")
            drafts += 1
    except Exception as exc:  # noqa: BLE001
        if drafts:
            return {"drafts": drafts, "fallback_general": fallback, "partial": type(exc).__name__}
        if isinstance(exc, mail_http.MailHTTPError) and exc.status in (401, 403):
            raise DraftWriteDenied(_DENIED_MESSAGE) from None
        raise
    return {"drafts": drafts, "fallback_general": fallback}


def _sev_rank(name):
    return _SEVERITIES.index(name) if name in _SEVERITIES else _SEVERITIES.index("minor")


class MRReview(loopkit.LoopPlugin):
    loop_name = "mr-review-loop"
    definition_dir = "mr-review"
    output_keys = ("summary", "findings")
    max_items_per_run = 8

    def __init__(self, accounts_fn=None, loader=None, poster=None, seen=None):
        self._accounts_fn = accounts_fn
        self._loader = loader
        self._poster = poster
        self._seen = seen

    def _seen_store(self):
        # Read-only here: LoopKit itself marks items seen after acting.
        if self._seen is None:
            self._seen = seen_store.SeenStore(self.loop_name)
        return self._seen

    def _resolve(self):
        import connectors_config
        return (self._accounts_fn or connectors_config.accounts_with_capability,
                self._loader or connectors_config.load_connector,
                self._poster or post_review)

    def discover(self, ctx):
        accounts_fn, loader, _ = self._resolve()
        items = []
        for account in accounts_fn("merge_requests"):
            if account.get("type") != "gitlab":
                continue
            try:
                conn = loader(account["id"])
                me = conn.api("GET", "/user")
                rows = conn.api("GET", f"/merge_requests?reviewer_id={me['id']}&state=opened"
                                       "&scope=all&draft=no&per_page=50")
            except Exception as exc:  # noqa: BLE001 - one account failing never stops the rest
                ctx.log(f"mr-review: account {account.get('id')} failed: {_err(exc)}")
                continue
            for row in rows or []:
                # The list already carries the head SHA: skip a reviewed one
                # before paying for its detail and diff requests.
                if (row.get("sha") and not getattr(ctx, "force", False)
                        and self._seen_store().has(item_key(account["id"], row.get("project_id"), row.get("iid"), row["sha"]))):
                    continue
                try:
                    item = self._item(conn, account["id"], me, row)
                except Exception as exc:  # noqa: BLE001
                    ctx.log(f"mr-review: MR !{row.get('iid')} failed to load: {_err(exc)}")
                    continue
                if item:
                    items.append(item)
        return items

    def _item(self, conn, account_id, me, row):
        if (row.get("author") or {}).get("id") == me.get("id"):
            return None
        pid, iid = row["project_id"], row["iid"]
        detail = conn.api("GET", f"/projects/{pid}/merge_requests/{iid}")
        refs = detail.get("diff_refs") or {}
        head = detail.get("sha") or refs.get("head_sha")
        if not head or not all(refs.get(k) for k in ("base_sha", "start_sha", "head_sha")):
            return None
        changes, size = [], 0
        for page in range(1, _MAX_DIFF_PAGES + 1):
            batch = conn.api("GET", f"/projects/{pid}/merge_requests/{iid}/diffs?per_page=100&page={page}")
            if not batch:
                break
            changes.extend(batch)
            size += sum(len(c.get("diff") or "") for c in batch)
            if size > DIFF_CAP_BYTES:
                break
        diff, truncated = build_diff_text(changes)
        title = str(detail.get("title") or row.get("title") or "")
        url = str(detail.get("web_url") or row.get("web_url") or "")
        return loopkit.WorkItem(
            key=item_key(account_id, pid, iid, head), title=title, url=url,
            payload={"account": account_id, "project_id": pid, "iid": iid, "title": title,
                     "description": str(detail.get("description") or "")[:MAX_DESCRIPTION],
                     "web_url": url, "diff_refs": {k: refs[k] for k in ("base_sha", "start_sha", "head_sha")},
                     "diff": diff, "truncated": truncated,
                     "changed_paths": sorted({str(c.get("new_path")) for c in changes if c.get("new_path")}),
                     "old_paths": {str(c["new_path"]): str(c["old_path"]) for c in changes
                                   if c.get("old_path") and c.get("new_path") and c["old_path"] != c["new_path"]}})

    def after_item(self, item, answer, ctx):
        _, loader, poster = self._resolve()
        payload = item.payload
        allowed = set(payload.get("changed_paths") or [])
        floor = _sev_rank(str((ctx.settings or {}).get("min_severity") or "minor"))
        kept = []
        for f in answer.get("findings") or []:
            if not isinstance(f, dict):
                continue
            line, sev, body = f.get("line"), f.get("severity"), f.get("body")
            if (f.get("path") not in allowed or isinstance(line, bool) or not isinstance(line, int)
                    or line < 1 or sev not in _SEVERITIES or _sev_rank(sev) < floor
                    or not isinstance(body, str) or not body.strip()):
                continue
            kept.append({"path": f["path"], "line": line, "severity": sev, "body": body.strip()[:MAX_BODY]})
        kept = kept[:MAX_FINDINGS]
        summary = answer.get("summary")
        cleaned = {"summary": summary.strip()[:MAX_SUMMARY] if isinstance(summary, str) else "",
                   "findings": kept}
        try:
            result = poster(loader(payload["account"]), payload, cleaned)
        except DraftWriteDenied as exc:
            # "skipped" is marked seen for this MR SHA, so a read-only token
            # does not pay for the same model call on every run.
            return loopkit.Outcome(item.key, "skipped", str(exc), url=payload.get("web_url", ""))
        blockers = sum(1 for f in kept if f["severity"] == "blocker")
        if result.get("partial"):
            summary_text = f"{result['drafts']} draft notes (partial: {result['partial']})"
        else:
            summary_text = f"{len(kept)} findings + summary ({blockers} blocker)"
        return loopkit.Outcome(item.key, "done", summary_text,
                               url=payload.get("web_url", ""),
                               data={"title": item.title, "drafts": result["drafts"], "blockers": blockers})

    def digest(self, outcomes, ctx):
        done = [o for o in outcomes if o.status == "done"]
        if not done:
            return None
        drafts = sum(o.data.get("drafts", 0) for o in done)
        blockers = sum(o.data.get("blockers", 0) for o in done)
        lines = [f"MR pre-review: {len(done)} MRs, {drafts} draft notes ({blockers} blockers). "
                 "Open each MR → Review → Submit to publish."]
        lines += ["- " + loopkit.chat_link(o.data.get("title") or o.item_key, o.url) for o in done]
        return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(loopkit.main(MRReview()))
