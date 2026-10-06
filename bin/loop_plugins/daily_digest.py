#!/usr/bin/env python3
"""Daily Digest loop on LoopKit: gathers todos / assigned issues / MRs /
today's meetings / yesterday's Loop X outcomes into one payload, asks the
model for a four-section brief and sends it through the loop's notifier.
Run by run-loop-now.sh as a script with the run id as argv[1] (--force
skips the once-a-day seen check)."""
import json
import re
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import loopkit  # noqa: E402

MAX_PAYLOAD_BYTES = 60_000
_SECTIONS = (("needs_you", "Needs you"), ("waiting_on_others", "Waiting on others"),
             ("meetings", "Today's meetings"), ("loop_x_did", "What Loop X did"), ("fyi", "FYI"))
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PER_PAGE = "per_page=50"


def _err(exc):
    # Class name only: str(exc) can carry URLs or tokens.
    return type(exc).__name__


def _gitlab_item(raw):
    target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
    project = raw.get("project") if isinstance(raw.get("project"), dict) else {}
    refs = raw.get("references") if isinstance(raw.get("references"), dict) else {}
    full = str(refs.get("full") or "")
    item = {
        "title": str(raw.get("title") or target.get("title") or ""),
        "url": str(raw.get("web_url") or target.get("web_url") or target_url(raw)),
        "project": str(project.get("path_with_namespace") or re.split(r"[#!]", full)[0]),
        "updated_at": str(raw.get("updated_at") or ""),
        "labels": list(raw.get("labels") or []),
    }
    if raw.get("action_name"):
        item["action"] = str(raw["action_name"])
    return item


def target_url(raw):
    return str(raw.get("target_url") or "")


def _list(rows):
    return [_gitlab_item(r) for r in (rows or []) if isinstance(r, dict)]


def _collect_gitlab(conn):
    user = conn.api("GET", "/user").get("username", "")
    return {
        "todos": _list(conn.api("GET", f"/todos?state=pending&{_PER_PAGE}")),
        "assigned": _list(conn.api("GET", f"/issues?scope=assigned_to_me&state=opened&{_PER_PAGE}")),
        "review_requests": _list(conn.api(
            "GET", f"/merge_requests?reviewer_username={user}&state=opened&scope=all&{_PER_PAGE}")),
        "my_merge_requests": _list(conn.api(
            "GET", f"/merge_requests?author_username={user}&state=opened&scope=all&{_PER_PAGE}")),
    }


def _collect_github(conn, yesterday):
    user = conn.api("GET", "/user").get("login", "")
    found = conn.api("GET", f"/search/issues?q=involves:{user}+state:open+updated:>={yesterday}&{_PER_PAGE}")
    rows = []
    for r in (found.get("items") if isinstance(found, dict) else None) or []:
        rows.append({"title": str(r.get("title") or ""), "url": str(r.get("html_url") or ""),
                     "project": str(r.get("repository_url") or "").split("/repos/")[-1],
                     "updated_at": str(r.get("updated_at") or ""), "labels": [
                         str(l.get("name")) for l in r.get("labels") or [] if isinstance(l, dict)]})
    return {"involved": rows}


def _default_loop_x(ctx, events_dir=None):
    import events
    day = str(ctx.now.date() - timedelta(days=1))
    out = {"completed": [], "escalated": []}
    for ev in events.iter_events(events_dir=events_dir, since_date=day, until_date=day):
        key = {"issue.completed": "completed", "issue.escalated": "escalated"}.get(ev.get("event_type"))
        if key:
            out[key].append({"project": ev.get("project"), "issue_iid": ev.get("issue_iid")})
    return out


def _default_inbox():
    import inbox_status
    inboxes = inbox_status.read().get("inboxes", {})
    rows = {name: len(e.get("urgent") or []) for name, e in inboxes.items() if isinstance(e, dict)}
    return {"urgent_by_inbox": rows, "urgent_total": sum(rows.values())}


def _default_topics(history_dir=None):
    """Latest briefing headline per topic (files are <date>-<topic>.md)."""
    history_dir = Path(history_dir) if history_dir is not None else _REPO_ROOT / "outputs" / "topic-monitor" / "history"
    latest = {}
    for path in sorted(history_dir.glob("*.md")) if history_dir.is_dir() else []:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})-(.+)\.md$", path.name)
        if m:
            latest[m.group(2)] = path
    out = {}
    for topic, path in latest.items():
        try:
            for line in path.read_text().splitlines():
                if line.strip() and not line.lstrip().startswith("#"):
                    out[topic] = line.strip()[:240]
                    break
        except OSError:
            continue
    return out


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - one failing source never fails the digest
        return {"error": _err(exc)}


def _meetings(ctx, accounts, loader):
    start = ctx.now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    out = []
    for account in accounts:
        try:
            events = loader(account["id"]).list_events(start.isoformat(), end.isoformat())
            out.append({"account": account["id"], "events": [
                {"title": e.get("summary", ""), "start": e.get("start", ""), "end": e.get("end", ""),
                 "url": e.get("html_link", ""), "attendees": e.get("attendees_count", 0)} for e in events]})
        except Exception as exc:  # noqa: BLE001
            out.append({"account": account["id"], "error": _err(exc)})
    return out


def _lists(payload):
    for acc in payload.get("accounts", []):
        for v in acc.values():
            if isinstance(v, list):
                yield v
    for m in payload.get("meetings", []):
        if isinstance(m.get("events"), list):
            yield m["events"]
    for v in (payload.get("loop_x") or {}).values():
        if isinstance(v, list):
            yield v


def _cap(payload):
    def size():
        return len(json.dumps(payload, default=str))
    payload["truncated"] = False
    while size() > MAX_PAYLOAD_BYTES:
        biggest = max(_lists(payload), key=lambda l: len(json.dumps(l, default=str)), default=None)
        if not biggest:
            payload["topics"] = {}
            break
        del biggest[len(biggest) // 2:]
        payload["truncated"] = True
    return payload


def collect(ctx, accounts_fn=None, loader=None, loop_x_fn=None, inbox_fn=None, topics_fn=None,
            calendar_accounts_fn=None):
    if accounts_fn is None:
        import connectors_config
        accounts_fn = connectors_config.accounts_with_capability
    if loader is None:
        import connectors_config
        loader = connectors_config.load_connector
    loop_x_fn = loop_x_fn or _default_loop_x
    inbox_fn = inbox_fn or _default_inbox
    topics_fn = topics_fn or _default_topics
    if calendar_accounts_fn is None:
        calendar_accounts_fn = lambda: accounts_fn("calendar")  # noqa: E731
    yesterday = str(ctx.now.date() - timedelta(days=1))

    accounts = []
    for account in accounts_fn("issues"):
        entry = {"account": account["id"], "type": account.get("type")}
        try:
            conn = loader(account["id"])
            if account.get("type") == "github":
                entry.update(_collect_github(conn, yesterday))
            else:
                entry.update(_collect_gitlab(conn))
        except Exception as exc:  # noqa: BLE001
            entry = {"account": account["id"], "error": _err(exc)}
        accounts.append(entry)
    try:
        cal_accounts = calendar_accounts_fn()
    except Exception:  # noqa: BLE001
        cal_accounts = []
    payload = {
        "date": str(ctx.now.date()),
        "accounts": accounts,
        "meetings": _meetings(ctx, cal_accounts, loader),
        "loop_x": _safe(loop_x_fn, ctx),
        "inbox": _safe(inbox_fn),
        "topics": _safe(topics_fn),
    }
    return _cap(payload)


def _known_urls(payload):
    urls = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "url" and isinstance(v, str) and v:
                    urls.add(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(payload)
    return urls


def format_digest(answer, date):
    lines = [f"*Daily digest - {date}*"]
    any_bullets = False
    for key, title in _SECTIONS:
        bullets = answer.get(key) or []
        if not bullets:
            continue
        any_bullets = True
        lines += ["", f"*{title}*"]
        for b in bullets:
            text = str(b.get("text", "")).replace("\n", " ")
            lines.append(f"- <{b['url']}|{text}>" if b.get("url") else f"- {text}")
    if not any_bullets:
        lines += ["", "Nothing needs your attention today."]
    return "\n".join(lines)


class DailyDigest(loopkit.LoopPlugin):
    loop_name = "daily-digest-loop"
    definition_dir = "daily-digest"
    output_keys = ("needs_you", "waiting_on_others", "loop_x_did", "fyi", "meetings")

    def discover(self, ctx):
        return [loopkit.WorkItem(key=f"digest:{ctx.now.date()}", title="Daily digest", payload=collect(ctx))]

    def after_item(self, item, answer, ctx):
        known = _known_urls(item.payload)
        cleaned = {}
        count = 0
        for key in self.output_keys:
            bullets = []
            for b in answer.get(key) or []:
                if not isinstance(b, dict):
                    continue
                url = b.get("url") or ""
                bullets.append({"text": str(b.get("text", "")), "url": url if url in known else ""})
            cleaned[key] = bullets
            count += len(bullets)
        return loopkit.Outcome(item.key, "done", f"{count} items", data=cleaned)

    def digest(self, outcomes, ctx):
        for o in outcomes:
            if o.status == "done":
                return format_digest(o.data, str(ctx.now.date()))
        return None


if __name__ == "__main__":
    sys.exit(loopkit.main(DailyDigest()))
