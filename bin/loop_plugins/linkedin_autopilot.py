#!/usr/bin/env python3
"""LinkedIn Autopilot loop on LoopKit (stage 1, read-only): browses the user's
own signed-in LinkedIn session at a human pace, collects the feed,
notifications and messages not seen before, has the sealed model summarize them
and pick posts and people worth attention, and notifies a short overview.
Everything from LinkedIn is untrusted: it is sanitized before the prompt, the
model runs sealed and every returned id must be one that was offered. Account
problems (expired session, security check, no browser) become failed outcomes
and never call the model. Run by run-loop-now.sh with the run id as argv[1]."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import linkedin_browser  # noqa: E402
import linkedin_humanize  # noqa: E402
import loopkit  # noqa: E402
import seen_store  # noqa: E402
from connectors.base import Field  # noqa: E402

MAX_NOTIFICATIONS, MAX_FEED, MAX_MESSAGES = 30, 30, 20
MAX_TEXT = 300
MAX_WHY = 140
MAX_TOPICS = 10
PROBLEM_PROMPT = "__linkedin_problem__"
PROBLEMS = {
    "session_expired": "LinkedIn session expired - reconnect it on the Connectors page",
    "challenge": "LinkedIn showed a security check - open LinkedIn in a normal browser, then reconnect",
    "browser_unavailable": "Playwright or Chrome is not available - see the Connectors page",
    "error": "LinkedIn could not be read - check the loop log",
}

SETTINGS_FIELDS = (
    Field("headless", "Run Chrome headless", kind="select", required=False, default="no", options=("no", "yes"),
          help="Off opens a visible Chrome window while the loop runs (looks most like a real user). "
               "On is invisible but easier for LinkedIn to detect."),
    Field("active_hours", "Active hours", required=False, default="9-21", placeholder="9-21",
          help="Hours (24h clock) in which the loop may browse, e.g. 9-21"),
    Field("topics", "Topics you care about", kind="textarea", required=False,
          help="Comma-separated topics the loop looks for",
          placeholder="Rails, AI coding agents, hiring"),
)


def _err(exc):
    # Class name only: str(exc) can carry URLs or page text.
    return type(exc).__name__


def _as_list(value):
    return value if isinstance(value, list) else []


def _clean(rows, cap):
    return [{"id": str(r["id"]), "text": loopkit.chat_text(r.get("text", ""), MAX_TEXT),
             "url": loopkit.chat_url(r.get("url", "")), "profile": loopkit.chat_url(r.get("profile", ""))}
            for r in rows[:cap] if isinstance(r, dict) and r.get("id")]


class LinkedInAutopilot(loopkit.LoopPlugin):
    loop_name = "linkedin-autopilot-loop"
    definition_dir = "linkedin-autopilot"
    output_keys = ("summary", "topics", "worth_reading", "people")
    settings_fields = SETTINGS_FIELDS

    def __init__(self, accounts_fn=None, session_factory=None, human=None, seen=None):
        self._accounts_fn = accounts_fn
        self._session_factory = session_factory
        self._human = human
        self._seen = seen

    def _seen_store(self):
        if self._seen is None:
            self._seen = seen_store.SeenStore("linkedin-autopilot-loop-items")
        return self._seen

    def _accounts(self):
        if self._accounts_fn is None:
            import connectors_config
            self._accounts_fn = connectors_config.accounts_with_capability
        return [a for a in self._accounts_fn("social") if a.get("type") == "linkedin"]

    def _collect(self, aid, settings, human):
        headless = str(settings.get("headless") or "no") == "yes"
        factory = self._session_factory or linkedin_browser.Session
        out = {}
        with factory(aid, headless=headless, human=human) as session:
            out["feed"] = session.feed()
            rest = {"notifications": session.notifications, "messages": session.messages, "wander": session.wander}
            order = list(rest)
            human.rng.shuffle(order)
            for name in order:
                rows = rest[name]()
                if name != "wander":
                    out[name] = rows
        return out

    @staticmethod
    def _problem(key, aid, problem):
        return loopkit.WorkItem(key=key, title=aid, payload={"problem": problem})

    def discover(self, ctx):
        settings = ctx.settings or {}
        human = self._human or linkedin_humanize.Humanizer()
        if not ctx.force:
            window = linkedin_humanize.parse_active_hours(settings.get("active_hours"))
            if not linkedin_humanize.within_hours(ctx.now.astimezone().hour, window):
                ctx.log("linkedin-autopilot: outside active hours")
                return []
            if human.skip_today():
                ctx.log("linkedin-autopilot: skipping today")
                return []
        seen = self._seen_store()
        topics = str(settings.get("topics") or "")[:1000]
        items = []
        for account in self._accounts():
            aid = account["id"]
            key = f"linkedin:{aid}:{ctx.now.astimezone().date()}"
            if not ctx.force:
                human.sleep(human.start_delay_seconds())
            try:
                data = self._collect(aid, settings, human)
            except linkedin_browser.SessionExpired:
                items.append(self._problem(key, aid, "session_expired"))
                continue
            except linkedin_browser.ChallengeError:
                items.append(self._problem(key, aid, "challenge"))
                continue
            except linkedin_browser.BrowserUnavailable:
                items.append(self._problem(key, aid, "browser_unavailable"))
                continue
            except Exception as exc:  # noqa: BLE001 - one account failing never stops the others
                ctx.log(f"linkedin-autopilot: account {aid} failed: {_err(exc)}")
                items.append(self._problem(key, aid, "error"))
                continue
            fresh = {name: [r for r in rows if isinstance(r, dict) and r.get("id") and not seen.has(str(r["id"]))]
                     for name, rows in data.items()}
            if not any(fresh.values()):
                continue
            items.append(loopkit.WorkItem(key=key, title=aid, payload={
                "topics": topics,
                "notifications": _clean(fresh.get("notifications", []), MAX_NOTIFICATIONS),
                "feed": _clean(fresh.get("feed", []), MAX_FEED),
                "messages": _clean(fresh.get("messages", []), MAX_MESSAGES)}))
        return items

    def build_prompt(self, item, ctx):
        if item.payload.get("problem"):
            return PROBLEM_PROMPT
        return super().build_prompt(item, ctx)

    def call_model(self, prompt, ctx):
        if prompt == PROBLEM_PROMPT:
            empty = {"summary": "", "topics": [], "worth_reading": [], "people": []}
            return {"text": json.dumps(empty), "cost_usd": 0.0}
        return super().call_model(prompt, ctx)

    def after_item(self, item, answer, ctx):
        problem = item.payload.get("problem")
        if problem:
            return loopkit.Outcome(item.key, "failed", PROBLEMS.get(problem, PROBLEMS["error"]),
                                   data={"problem": problem})
        payload = item.payload
        # Mark the offered rows seen first: a malformed answer must never get the
        # same rows re-offered (and re-paid for) on every run.
        seen = self._seen_store()
        for rows in (payload["notifications"], payload["feed"], payload["messages"]):
            for row in rows:
                seen.add(row["id"])
        seen.save()
        feed = {r["id"]: r for r in payload["feed"]}
        worth, people, used_worth, used_people = [], [], set(), set()
        for w in _as_list(answer.get("worth_reading")):
            wid = w.get("id") if isinstance(w, dict) else None
            row = feed.get(wid) if isinstance(wid, str) else None
            if row is None or row["id"] in used_worth:
                continue
            used_worth.add(row["id"])
            worth.append({"id": row["id"], "title": loopkit.chat_text(row["text"], 100), "url": row["url"],
                          "why": loopkit.chat_text(w.get("why", ""), MAX_WHY)})
        for p in _as_list(answer.get("people")):
            pid = p.get("id") if isinstance(p, dict) else None
            row = feed.get(pid) if isinstance(pid, str) else None
            if row is None or not row["profile"] or row["id"] in used_people:
                continue
            used_people.add(row["id"])
            people.append({"id": row["id"], "title": loopkit.chat_text(row["text"], 60), "profile": row["profile"],
                           "url": row["url"], "why": loopkit.chat_text(p.get("why", ""), MAX_WHY)})
        topics = [loopkit.chat_text(t, 40) for t in _as_list(answer.get("topics")) if isinstance(t, str) and t.strip()]
        counts = {"notifications": len(payload["notifications"]), "messages": len(payload["messages"]),
                  "feed": len(payload["feed"])}
        return loopkit.Outcome(
            item.key, "done",
            f"{counts['notifications']} notifications, {counts['messages']} messages, {counts['feed']} posts",
            data={"account": item.title, "label": "LinkedIn",
                  "summary": loopkit.chat_text(answer.get("summary", ""), 600),
                  "topics": topics[:MAX_TOPICS], "worth_reading": worth, "people": people, "counts": counts})

    def digest(self, outcomes, ctx):
        lines = []
        for o in outcomes:
            if o.status == "failed":
                problem = isinstance(o.data, dict) and o.data.get("problem")
                text = o.summary if problem else "the run failed - check the loop log"
                lines += ["", f"LinkedIn needs attention: {text}"]
            elif o.status == "done":
                d, c = o.data, o.data["counts"]
                lines += ["", loopkit.chat_text(d.get("account", ""), 60)]
                if d.get("summary"):
                    lines.append(d["summary"])
                lines.append(f"{c['notifications']} notifications, {c['messages']} messages, {c['feed']} feed posts")
                if d["worth_reading"]:
                    lines.append("Worth reading:")
                    lines += [f"- {loopkit.chat_link(w['title'], w['url'])} - {w['why']}" for w in d["worth_reading"]]
                if d["people"]:
                    lines.append("People to know:")
                    lines += [f"- {loopkit.chat_link(p['title'], p['profile'])} - {p['why']}" for p in d["people"]]
        if not lines:
            return None
        return "\n".join([f"LinkedIn Autopilot - {ctx.now.astimezone().date()}"] + lines)


if __name__ == "__main__":
    sys.exit(loopkit.main(LinkedInAutopilot()))
