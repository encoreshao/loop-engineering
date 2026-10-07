#!/usr/bin/env python3
"""Stale Work Sweeper loop on LoopKit: once a week, lists GitLab work that
has gone quiet - open issues assigned to you and open MRs you opened, idle
for `stale_days`, and open MRs waiting on your review, idle for
`review_days` - and sends the list through the loop's notifier. No model
call: build_prompt renders the list and call_model returns it unchanged at
$0, so the run still goes through LoopRuntime (history, ledger, retries).
Read-only. See docs/tasks/stale-sweeper-loop.md.
Run by run-loop-now.sh with the run id as argv[1]."""
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import loopkit  # noqa: E402
from connectors.base import Field  # noqa: E402

DEFAULT_STALE_DAYS = 14
DEFAULT_REVIEW_DAYS = 3
MAX_ROWS = 20
MAX_TITLE = 200

SETTINGS_FIELDS = (
    Field("stale_days", "Stale after (days)", required=False, default=str(DEFAULT_STALE_DAYS),
          help="Your issues and merge requests idle this many days are listed (1-365)", placeholder="14"),
    Field("review_days", "Review stale after (days)", required=False, default=str(DEFAULT_REVIEW_DAYS),
          help="Merge requests waiting on your review this many days are listed (1-365)", placeholder="3"),
)


def _err(exc):
    # Class name only: str(exc) can carry URLs or tokens.
    return type(exc).__name__


def _days(settings, key, default):
    try:
        value = int(str((settings or {}).get(key, default)).strip())
    except ValueError:
        return default
    return min(365, max(1, value))


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _idle_days(updated, now):
    try:
        when = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, (now - when).days)


class StaleSweeper(loopkit.LoopPlugin):
    loop_name = "stale-sweeper-loop"
    definition_dir = "stale-sweeper"
    output_keys = ()
    settings_fields = SETTINGS_FIELDS

    def __init__(self, accounts_fn=None, loader=None):
        self._accounts_fn = accounts_fn
        self._loader = loader

    def _resolve(self):
        import connectors_config
        return (self._accounts_fn or connectors_config.accounts_with_capability,
                self._loader or connectors_config.load_connector)

    def discover(self, ctx):
        accounts_fn, loader = self._resolve()
        stale = _days(ctx.settings, "stale_days", DEFAULT_STALE_DAYS)
        review = _days(ctx.settings, "review_days", DEFAULT_REVIEW_DAYS)
        items = []
        for account in accounts_fn("issues"):
            if account.get("type") != "gitlab":
                continue
            aid = account["id"]
            try:
                conn = loader(aid)
                me = conn.api("GET", "/user").get("username", "")
            except Exception as exc:  # noqa: BLE001 - one account failing never stops the rest
                ctx.log(f"stale-sweeper: account {aid} failed: {_err(exc)}")
                continue
            queries = (
                ("issues", f"Assigned issues idle {stale}+ days", "/issues",
                 {"scope": "assigned_to_me"}, stale),
                ("my_mrs", f"My merge requests idle {stale}+ days", "/merge_requests",
                 {"scope": "created_by_me"}, stale),
                ("reviews", f"Reviews waiting on you {review}+ days", "/merge_requests",
                 {"scope": "all", "reviewer_username": me}, review),
            )
            sections = [self._section(conn, key, label, path, extra, days, ctx, aid)
                        for key, label, path, extra, days in queries]
            if any(s["rows"] for s in sections):
                items.append(loopkit.WorkItem(key=f"stale:{aid}:{ctx.now.date().isoformat()}", title=aid,
                                              payload={"date": ctx.now.date().isoformat(), "sections": sections}))
        return items

    def _section(self, conn, key, label, path, extra, days, ctx, aid):
        query = {**extra, "state": "opened", "updated_before": _iso(ctx.now - timedelta(days=days)),
                 "order_by": "updated_at", "sort": "asc", "per_page": MAX_ROWS}
        section = {"key": key, "label": label, "rows": [], "error": False}
        try:
            rows = conn.api("GET", f"{path}?{urllib.parse.urlencode(query)}") or []
        except Exception as exc:  # noqa: BLE001 - one query failing never hides the others
            ctx.log(f"stale-sweeper: {key} for {aid} failed: {_err(exc)}")
            section["error"] = True
            return section
        for r in rows[:MAX_ROWS]:
            if not isinstance(r, dict):
                continue
            ref = (r.get("references") or {}).get("full") or f"#{r.get('iid', '?')}"
            section["rows"].append({"ref": loopkit.chat_text(ref, 120),
                                    "title": loopkit.chat_text(r.get("title") or "", MAX_TITLE),
                                    "idle_days": _idle_days(r.get("updated_at"), ctx.now),
                                    "url": loopkit.chat_url(r.get("web_url") or "")})
        return section

    def build_prompt(self, item, ctx):
        """The rendered list itself - there is no model to prompt."""
        lines = [loopkit.chat_text(item.title, 60)]
        for s in item.payload["sections"]:
            if s["error"]:
                lines.append(f"{s['label']}: unavailable")
                continue
            if not s["rows"]:
                continue
            lines.append(f"{s['label']} ({len(s['rows'])}):")
            for r in s["rows"]:
                idle = f" (idle {r['idle_days']}d)" if r["idle_days"] is not None else ""
                url = f" {r['url']}" if r["url"] else ""
                lines.append(f"- {r['ref']} {r['title']}{idle}{url}")
        return "\n".join(lines)

    def call_model(self, prompt, ctx):
        return {"text": prompt, "cost_usd": 0}

    def after_item(self, item, answer, ctx):
        count = sum(len(s["rows"]) for s in item.payload["sections"])
        return loopkit.Outcome(item.key, "done", f"{count} stale", data={"text": answer})

    def digest(self, outcomes, ctx):
        blocks = [o.data["text"] for o in outcomes if o.status == "done" and o.data.get("text")]
        if not blocks:
            return None
        return "\n\n".join([f"Stale work - {ctx.now.date().isoformat()}"] + blocks)


if __name__ == "__main__":
    sys.exit(loopkit.main(StaleSweeper()))
