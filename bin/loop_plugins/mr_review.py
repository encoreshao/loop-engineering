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

DIFF_CAP_BYTES = 150_000
MAX_FINDINGS = 15
MAX_DESCRIPTION = 4096
MAX_BODY = 2000
MAX_SUMMARY = 600
_MAX_DIFF_PAGES = 20
_SEVERITIES = ("nit", "minor", "major", "blocker")
_SIGNATURE = "\n\n_— Loop X pre-review_"
_MENTION = re.compile(r"(?<![\w@])@")


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
        text += f"\n[diff truncated at {cap} bytes]"
    return text, truncated


def _defuse(text):
    """Draft notes are Markdown on GitLab: break word-start @mentions so a
    model-echoed @all / @group cannot notify anyone."""
    return _MENTION.sub("@​", str(text))


def _post_draft(conn, mr, note, position=None):
    path = f"/projects/{mr['project_id']}/merge_requests/{mr['iid']}/draft_notes"
    body = {"note": note}
    if position is not None:
        body["position"] = position
    return conn.api("POST", path, json_body=body)


def post_review(conn, mr, answer):
    refs = mr["diff_refs"]
    drafts = fallback = 0
    for f in answer.get("findings") or []:
        note = f"**[{f['severity']}]** {_defuse(f['body'])}{_SIGNATURE}"
        position = {"position_type": "text", "base_sha": refs["base_sha"], "start_sha": refs["start_sha"],
                    "head_sha": refs["head_sha"], "new_path": f["path"], "new_line": f["line"]}
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
    return {"drafts": drafts, "fallback_general": fallback}


def _sev_rank(name):
    return _SEVERITIES.index(name) if name in _SEVERITIES else _SEVERITIES.index("minor")


class MRReview(loopkit.LoopPlugin):
    loop_name = "mr-review-loop"
    definition_dir = "mr-review"
    output_keys = ("summary", "findings")
    max_items_per_run = 8

    def __init__(self, accounts_fn=None, loader=None, poster=None):
        self._accounts_fn = accounts_fn
        self._loader = loader
        self._poster = poster

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
                     "changed_paths": sorted({str(c.get("new_path")) for c in changes if c.get("new_path")})})

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
        result = poster(loader(payload["account"]), payload, cleaned)
        blockers = sum(1 for f in kept if f["severity"] == "blocker")
        return loopkit.Outcome(item.key, "done", f"{result['drafts']} draft notes ({blockers} blocker)",
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
