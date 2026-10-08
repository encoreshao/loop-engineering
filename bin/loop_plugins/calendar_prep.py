#!/usr/bin/env python3
"""Meeting Prep loop on LoopKit: shortly before each meeting, gathers the
invite (agenda, attendees, join link), the GitLab issues/MRs it links to,
recent mail with the attendees and what the last brief for the same series
said, has the model write a short prep brief and sends it through the
loop's notifier. Read-only everywhere. Invite text, GitLab titles and mail
snippets are untrusted: they are trimmed before the prompt, the model runs
sealed, every output string is sanitised and a brief may only link to the
GitLab URLs it was offered. See docs/tasks/meeting-prep-loop.md.
Run by run-loop-now.sh with the run id as argv[1]."""
import json
import os
import re
import sys
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import loopkit  # noqa: E402
from connectors.base import Field  # noqa: E402

DEFAULT_LEAD_MINUTES = 45
LEAD_RANGE = (15, 120)
DEFAULT_MAIL_DAYS = 30
MAX_MAIL_DAYS = 365
MAIL_LIMIT = 10
MAX_GITLAB_LINKS = 5
MAX_DESCRIPTION = 4000
MAX_SNIPPET = 300
MAX_SUMMARY = 400
MAX_ENTRY = 200
MAX_ENTRIES = 5
MAX_BRIEFS = 100  # briefs kept for the Live page, newest first
NO_CLAUDE = "mail skipped: reading mail requires the Claude CLI"

SETTINGS_FIELDS = (
    Field("calendar_account", "Calendar account", required=False,
          help="Connector id of the calendar to watch; empty watches every calendar",
          placeholder="google-calendar"),
    Field("lead_minutes", "Minutes ahead", required=False, default=str(DEFAULT_LEAD_MINUTES),
          help="Brief meetings starting within this many minutes (15-120)", placeholder="45"),
    Field("mail_days", "Mail lookback (days)", required=False, default=str(DEFAULT_MAIL_DAYS),
          help="Search recent mail with the attendees; 0 turns mail off", placeholder="30"),
    Field("include_solo", "Include solo events", required=False, default="no",
          help="yes also briefs events with no other attendees", placeholder="no"),
)

_URL = re.compile(r"https?://[^\s<>\"'()\[\]]+")
_GITLAB_PATH = re.compile(r"^(.+?)/-/(issues|merge_requests)/(\d+)(?:[/?#].*)?$")


def _err(exc):
    # Class name only: str(exc) can carry URLs or tokens.
    return type(exc).__name__


def _int_setting(settings, key, default, lo, hi):
    try:
        value = int(str((settings or {}).get(key, default)).strip())
    except ValueError:
        return default
    return min(hi, max(lo, value))


def _lead_minutes(settings):
    return _int_setting(settings, "lead_minutes", DEFAULT_LEAD_MINUTES, *LEAD_RANGE)


def _mail_days(settings):
    return _int_setting(settings, "mail_days", DEFAULT_MAIL_DAYS, 0, MAX_MAIL_DAYS)


def _parse(value):
    """A timed event's start/end as an aware datetime; None for all-day
    ('YYYY-MM-DD') or unparsable values."""
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _default_mail(addresses, days, limit):
    """Recent mail with any of `addresses` across every connected mailbox,
    newest first. Raises only when every mailbox failed."""
    import inbox_config
    import mail_auth
    import mail_providers
    rows, failure = [], None
    for inbox in inbox_config.load_config_or_empty().get("inboxes") or []:
        own = str(inbox.get("account") or "").strip().lower()
        try:
            token = mail_auth.get_access_token(inbox)
            provider = mail_providers.get_provider(inbox, token, lambda inbox=inbox: mail_auth.get_access_token(inbox))
            rows += provider.search_recent([a for a in addresses if a != own], days, limit)
        except Exception as exc:  # noqa: BLE001 - one mailbox failing never stops the others
            failure = exc
    if failure is not None and not rows:
        raise failure
    rows.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    return rows[:limit]


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _usernames(value):
    return [str(u.get("username")) for u in value or [] if isinstance(u, dict) and u.get("username")]


class CalendarPrep(loopkit.LoopPlugin):
    loop_name = "meeting-prep-loop"
    definition_dir = "calendar-prep"
    output_keys = ("summary", "agenda", "open_items", "talking_points", "follow_ups")
    settings_fields = SETTINGS_FIELDS

    def __init__(self, accounts_fn=None, loader=None, mail_fn=None, cli_fn=None, series_path=None,
                 today_path=None, briefs_path=None):
        self._accounts_fn = accounts_fn
        self._loader = loader
        self._mail_fn = mail_fn
        self._cli_fn = cli_fn
        self._series_path = series_path
        self._today_path = today_path
        self._briefs_path = briefs_path

    def _resolve(self):
        import connectors_config
        return (self._accounts_fn or connectors_config.accounts_with_capability,
                self._loader or connectors_config.load_connector)

    def _series_file(self, ctx):
        if self._series_path is not None:
            return Path(self._series_path)
        return Path(ctx.repo_root) / "outputs" / "loops" / self.loop_name / "series.json"

    def _today_file(self, ctx):
        if self._today_path is not None:
            return Path(self._today_path)
        return Path(ctx.repo_root) / "outputs" / "loops" / self.loop_name / "today.json"

    def _briefs_file(self, ctx):
        if self._briefs_path is not None:
            return Path(self._briefs_path)
        return Path(ctx.repo_root) / "outputs" / "loops" / self.loop_name / "briefs.json"

    def _record_brief(self, ctx, key, brief):
        """Keep the brief by meeting key so the Live page can show each
        meeting's summary; only the newest MAX_BRIEFS are kept."""
        path = self._briefs_file(ctx)
        briefs = _read_json(path)
        briefs[key] = {"summary": brief["summary"], "agenda": brief["agenda"],
                       "prepared_at": ctx.now.isoformat()}
        newest = sorted(briefs, key=lambda k: str((briefs[k] or {}).get("prepared_at") or ""), reverse=True)
        try:
            _write_json_atomic(path, {k: briefs[k] for k in newest[:MAX_BRIEFS]})
        except OSError as exc:
            ctx.log(f"calendar-prep: could not record the brief: {_err(exc)}")

    def _cli(self):
        if self._cli_fn is not None:
            return self._cli_fn()
        import ai_cli_config
        return ai_cli_config.get_selected_cli()

    # --- discovery ---------------------------------------------------------

    def discover(self, ctx):
        accounts_fn, loader = self._resolve()
        settings = ctx.settings or {}
        lead = _lead_minutes(settings)
        mail_days = _mail_days(settings)
        include_solo = str(settings.get("include_solo") or "").strip().lower() == "yes"
        only = str(settings.get("calendar_account") or "").strip()
        window_end = ctx.now + timedelta(minutes=lead)
        # One call covers the whole local day: it feeds both the briefs
        # (only meetings inside the lead window) and the Live page's agenda.
        day_start = ctx.now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        fetch_end = max(day_end, window_end)
        today, fetched = [], False
        series = _read_json(self._series_file(ctx))
        gitlabs = None
        items = []
        for account in accounts_fn("calendar"):
            aid = account["id"]
            if only and aid != only:
                continue
            try:
                events = loader(aid).list_events(day_start.isoformat(), fetch_end.isoformat())
            except Exception as exc:  # noqa: BLE001 - one calendar failing never stops the rest
                ctx.log(f"calendar-prep: calendar {aid} failed: {_err(exc)}")
                continue
            fetched = True
            for ev in events or []:
                today.append(self._today_row(aid, ev))
                if not self._wanted(ev, ctx.now, window_end, include_solo):
                    continue
                if gitlabs is None:
                    gitlabs = self._gitlab_connectors(accounts_fn, loader, ctx)
                items.append(self._item(aid, ev, gitlabs, mail_days, series, ctx))
        if fetched:
            self._write_today(ctx, day_start, today)
        return items

    @staticmethod
    def _today_row(aid, ev):
        """The Live page's view of one event, or None for all-day/declined."""
        if _parse(ev.get("start")) is None or ev.get("self_response") == "declined":
            return None
        return {"key": f"cal:{aid}:{ev.get('id', '')}:{ev.get('start', '')}",
                "title": loopkit.chat_text(ev.get("summary") or "", MAX_ENTRY),
                "start": ev.get("start", ""), "end": ev.get("end", ""),
                "join_url": loopkit.chat_url(ev.get("join_url") or ""),
                "html_link": loopkit.chat_url(ev.get("html_link") or "")}

    def _write_today(self, ctx, day_start, rows):
        meetings = sorted((r for r in rows if r), key=lambda r: r["start"])
        try:
            _write_json_atomic(self._today_file(ctx), {
                "date": day_start.date().isoformat(), "generated_at": ctx.now.isoformat(),
                "meetings": meetings})
        except OSError as exc:
            ctx.log(f"calendar-prep: could not write today's meetings: {_err(exc)}")

    @staticmethod
    def _wanted(ev, now, window_end, include_solo):
        start = _parse(ev.get("start"))
        if start is None or start <= now or start > window_end:
            return False
        if ev.get("self_response") == "declined":
            return False
        others = [a for a in ev.get("attendees") or [] if not a.get("self")]
        return bool(others) or include_solo

    def _gitlab_connectors(self, accounts_fn, loader, ctx):
        found = []
        for account in accounts_fn("issues"):
            if account.get("type") != "gitlab":
                continue
            try:
                found.append(loader(account["id"]))
            except Exception as exc:  # noqa: BLE001
                ctx.log(f"calendar-prep: GitLab account {account['id']} failed: {_err(exc)}")
        return found

    def _item(self, aid, ev, gitlabs, mail_days, series, ctx):
        me = {str(a.get("email") or "").lower() for a in ev.get("attendees") or [] if a.get("self")}
        others = [a for a in ev.get("attendees") or [] if not a.get("self")]
        payload = {"event": {
            "title": loopkit.chat_text(ev.get("summary") or "", MAX_ENTRY),
            "start": ev.get("start", ""), "end": ev.get("end", ""),
            "organizer": loopkit.chat_text(ev.get("organizer") or "", 120),
            "attendees": [loopkit.chat_text(a.get("name") or a.get("email") or "", 80) for a in others],
            "location": loopkit.chat_text(ev.get("location") or "", MAX_ENTRY),
            "join_url": loopkit.chat_url(ev.get("join_url") or ""),
            "description": str(ev.get("description") or "")[:MAX_DESCRIPTION],
        }}
        payload["gitlab"], gitlab_error = self._linked_gitlab(str(ev.get("description") or ""), gitlabs, ctx)
        if gitlab_error:
            payload["gitlab_error"] = gitlab_error
        addresses = [str(x).lower() for x in [ev.get("organizer")] + [a.get("email") for a in others] if x]
        addresses = [a for i, a in enumerate(addresses) if a not in me and a not in addresses[:i]]
        payload["mail"], mail_error = self._mail(addresses, mail_days, ctx)
        if mail_error:
            payload["mail_error"] = mail_error
        series_id = str(ev.get("recurring_event_id") or ev.get("id") or "")
        payload["series_id"] = series_id
        if isinstance(series.get(series_id), dict):
            payload["last_time"] = series[series_id]
        return loopkit.WorkItem(key=f"cal:{aid}:{ev.get('id', '')}:{ev.get('start', '')}",
                                title=payload["event"]["title"], url=loopkit.chat_url(ev.get("html_link") or ""),
                                payload=payload)

    def _linked_gitlab(self, description, gitlabs, ctx):
        """(rows, error-or-None) for GitLab issue/MR URLs in the invite that
        belong to a configured GitLab account; at most MAX_GITLAB_LINKS
        fetches per meeting."""
        refs = []
        for url in _URL.findall(description):
            for conn in gitlabs:
                base = conn.base_url().rstrip("/")
                if not url.startswith(base + "/"):
                    continue
                m = _GITLAB_PATH.match(url[len(base) + 1:])
                if m:
                    ref = (conn, base, m.group(1), m.group(2), m.group(3))
                    if ref[1:] not in [r[1:] for r in refs]:
                        refs.append(ref)
                break
        rows, failed = [], False
        for conn, base, path, kind, iid in refs[:MAX_GITLAB_LINKS]:
            try:
                data = conn.api("GET", f"/projects/{urllib.parse.quote(path, safe='')}/{kind}/{iid}")
            except Exception as exc:  # noqa: BLE001 - one link failing never stops the brief
                ctx.log(f"calendar-prep: a GitLab link failed: {_err(exc)}")
                failed = True
                continue
            row = {"kind": "issue" if kind == "issues" else "merge_request",
                   "url": f"{base}/{path}/-/{kind}/{iid}",
                   "title": loopkit.chat_text(data.get("title") or "", MAX_ENTRY),
                   "state": loopkit.chat_text(data.get("state") or "", 40),
                   "updated_at": loopkit.chat_text(data.get("updated_at") or "", 40),
                   "assignees": _usernames(data.get("assignees"))}
            if kind == "merge_requests":
                row["reviewers"] = _usernames(data.get("reviewers"))
                row["merge_status"] = loopkit.chat_text(data.get("detailed_merge_status") or "", 40)
            rows.append(row)
        return rows, ("GitLab links unavailable" if failed else None)

    def _mail(self, addresses, days, ctx):
        if days <= 0 or not addresses:
            return [], None
        if self._cli() != "claude":
            return [], NO_CLAUDE
        try:
            raw = (self._mail_fn or _default_mail)(addresses, days, MAIL_LIMIT)
        except Exception as exc:  # noqa: BLE001 - mail failing never stops the brief
            ctx.log(f"calendar-prep: mail search failed: {_err(exc)}")
            return [], "mail unavailable"
        return [{"subject": loopkit.chat_text(r.get("subject") or "", MAX_ENTRY),
                 "from": loopkit.chat_text(r.get("from") or "", 120),
                 "date": loopkit.chat_text(r.get("date") or "", 40),
                 "snippet": loopkit.chat_text(r.get("snippet") or "", MAX_SNIPPET)}
                for r in (raw or [])[:MAIL_LIMIT] if isinstance(r, dict)], None

    # --- answer ------------------------------------------------------------

    def after_item(self, item, answer, ctx):
        offered = {r["url"] for r in item.payload.get("gitlab", [])}

        def entries(key):
            return [loopkit.chat_text(x, MAX_ENTRY) for x in answer.get(key) or [] if isinstance(x, str)][:MAX_ENTRIES]

        open_items = []
        for o in answer.get("open_items") or []:
            if isinstance(o, dict) and o.get("text"):
                link = o.get("link") if o.get("link") in offered else ""
                open_items.append({"text": loopkit.chat_text(o["text"], MAX_ENTRY), "link": link})
        brief = {"summary": loopkit.chat_text(answer.get("summary") or "", MAX_SUMMARY),
                 "agenda": entries("agenda"), "open_items": open_items[:MAX_ENTRIES],
                 "talking_points": entries("talking_points"), "follow_ups": entries("follow_ups")}
        series_id = item.payload.get("series_id")
        if series_id:
            path = self._series_file(ctx)
            series = _read_json(path)
            series[series_id] = {"date": ctx.now.date().isoformat(), "summary": brief["summary"],
                                 "follow_ups": brief["follow_ups"]}
            _write_json_atomic(path, series)
        self._record_brief(ctx, item.key, brief)
        ev = item.payload["event"]
        return loopkit.Outcome(item.key, "done", brief["summary"][:120] or "brief written",
                               url=ev["join_url"] or item.url,
                               data={"label": ev["title"],
                                     "event": {k: ev[k] for k in ("title", "start", "end", "join_url")},
                                     "brief": brief})

    def digest(self, outcomes, ctx):
        blocks = [self._render(o.data, ctx) for o in outcomes if o.status == "done" and o.data.get("brief")]
        return "\n\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n".join(blocks) if blocks else None

    @staticmethod
    def _render(data, ctx):
        """Slack mrkdwn that still reads fine as plain text elsewhere: bold
        title, one line of time and join link, a quoted summary and bulleted
        sections."""
        ev, brief = data["event"], data["brief"]
        start, end = _parse(ev.get("start")), _parse(ev.get("end"))
        title = loopkit.chat_text(ev.get("title") or "", MAX_ENTRY).replace("*", "")
        lines = [f"*Meeting Prep: {title}*", ""]
        when = []
        if start is not None:
            span = start.astimezone().strftime("%H:%M")
            if end is not None:
                span += "\u2013" + end.astimezone().strftime("%H:%M")
            minutes = max(0, round((start - ctx.now).total_seconds() / 60))
            when.append(f"\U0001F550 `{span}`  \u00b7  in {minutes} min")
        if ev.get("join_url"):
            when.append("\U0001F3A5 " + loopkit.slack_link(ev["join_url"], "Join meeting"))
        if when:
            lines.append("   ".join(when))
        if brief["summary"]:
            lines += ["", f"> {brief['summary']}"]

        def section(heading, entries, numbered=False):
            if entries:
                bullets = [f"{n}. {e}" if numbered else f"\u2022 {e}" for n, e in enumerate(entries, 1)]
                lines.extend(["", f"*{heading}*"] + bullets)

        section("\U0001F4CB Agenda", brief["agenda"], numbered=True)
        section("\U0001F513 Open items", [loopkit.slack_link(o["link"], o["text"]) if o["link"] else o["text"]
                                         for o in brief["open_items"]])
        section("\U0001F4AC Raise", brief["talking_points"])
        section("\u21A9\uFE0F From last time", brief["follow_ups"])
        return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(loopkit.main(CalendarPrep()))
