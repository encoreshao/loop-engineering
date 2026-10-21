#!/usr/bin/env python3
"""RSS Watch loop on LoopKit: collects unseen entries from each RSS/Atom
connector account's feeds, has the model rank them against the user's
interests (settings.interests) and sends a short digest through the loop's
notifier. Feed content is untrusted: entries are trimmed before the prompt,
the model runs sealed and every highlight link must be one that was offered.
Run by run-loop-now.sh with the run id as argv[1]."""
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import loopkit  # noqa: E402
import seen_store  # noqa: E402
from connectors.base import Field  # noqa: E402

MAX_ENTRIES = 40
MAX_HIGHLIGHTS = 10
MAX_TITLE = 200
MAX_WHY = 140
MAX_SUMMARY = 500
MAX_ACTION = 200
MAX_LINKS = 4
NUMBER_EMOJI = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "keycap_ten")
SEPARATOR = "\u2500" * 16

SETTINGS_FIELDS = (
    Field("interests", "Interests", kind="textarea", required=False,
          help="Comma-separated topics you care about",
          placeholder="Rails security, Postgres performance, AI coding agents"),
)


def _err(exc):
    # Class name only: str(exc) can carry URLs or tokens.
    return type(exc).__name__


def _score(value):
    try:
        return min(5, max(1, int(value)))
    except (TypeError, ValueError):
        return 1


class RSSWatch(loopkit.LoopPlugin):
    loop_name = "rss-watch-loop"
    definition_dir = "rss-watch"
    output_keys = ("highlights",)
    settings_fields = SETTINGS_FIELDS

    def __init__(self, accounts_fn=None, loader=None, seen=None):
        self._accounts_fn = accounts_fn
        self._loader = loader
        self._seen = seen

    def _resolve(self):
        import connectors_config
        return (self._accounts_fn or connectors_config.accounts_with_capability,
                self._loader or connectors_config.load_connector)

    def _seen_store(self):
        if self._seen is None:
            self._seen = seen_store.SeenStore("rss-watch-loop-entries")
        return self._seen

    def discover(self, ctx):
        accounts_fn, loader = self._resolve()
        seen = self._seen_store()
        refreshed = False
        interests = str((ctx.settings or {}).get("interests") or "")[:1000]
        items = []
        for account in accounts_fn("feed"):
            aid = account["id"]
            try:
                conn = loader(aid)
                urls = [u.strip() for u in str(conn.settings.get("feeds", "")).splitlines() if u.strip()]
            except Exception as exc:  # noqa: BLE001
                ctx.log(f"rss-watch: account {aid} failed: {_err(exc)}")
                continue
            entries, links = [], set()
            for url in urls:
                try:
                    rows = conn.entries(url)
                except Exception as exc:  # noqa: BLE001 - one feed failing never stops the rest
                    ctx.log(f"rss-watch: a feed of {aid} failed: {_err(exc)}")
                    continue
                for row in rows or []:
                    link = loopkit.chat_url(row.get("link")) if isinstance(row, dict) else ""
                    if not link or link in links:
                        continue
                    if seen.has(link):
                        # Still in the feed: refresh its timestamp so a persistent
                        # entry never ages out of the seen window and gets re-sent.
                        seen.add(link)
                        links.add(link)
                        refreshed = True
                        continue
                    links.add(link)
                    entries.append({"title": loopkit.chat_text(row.get("title", ""), MAX_TITLE),
                                    "link": link, "published": loopkit.chat_text(row.get("published", ""), 40),
                                    "summary": loopkit.chat_text(re.sub(r"<[^>]*>", " ", str(row.get("summary", ""))), MAX_SUMMARY)})
            if entries:
                items.append(loopkit.WorkItem(
                    key=f"rss:{aid}:{ctx.now.astimezone().date()}", title=aid,
                    payload={"interests": interests, "entries": entries[:MAX_ENTRIES]}))
        if refreshed:
            seen.save()
        return items

    def after_item(self, item, answer, ctx):
        offered = {e["link"]: e for e in item.payload["entries"]}
        highlights, used = [], set()
        for h in answer.get("highlights") or []:
            if not isinstance(h, dict) or h.get("link") not in offered or h["link"] in used:
                continue
            used.add(h["link"])
            extra = []
            for link in h.get("links") or []:
                if link in offered and link != h["link"] and link not in extra:
                    extra.append(link)
            highlights.append({
                "title": loopkit.chat_text(h.get("title", ""), MAX_TITLE), "link": h["link"],
                "links": [h["link"]] + extra[:MAX_LINKS - 1],
                "source": loopkit.chat_text(h.get("source") or item.title, 80),
                "published": offered[h["link"]].get("published", ""),
                "summary": loopkit.chat_text(h.get("summary") or h.get("why", ""), MAX_SUMMARY),
                "do_next": loopkit.chat_text(h.get("do_next", ""), MAX_ACTION),
                "kill_if": loopkit.chat_text(h.get("kill_if", ""), MAX_ACTION),
                "score": _score(h.get("score"))})
        highlights.sort(key=lambda h: -h["score"])
        highlights = highlights[:MAX_HIGHLIGHTS]
        seen = self._seen_store()
        for link in offered:
            seen.add(link)
        seen.save()
        return loopkit.Outcome(item.key, "done", f"{len(highlights)} highlights",
                               data={"account": item.title, "highlights": highlights})

    @staticmethod
    def _clock(published):
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(published.replace("GMT", "+0000"), fmt).astimezone().strftime("%H:%M")
            except ValueError:
                continue
        return ""

    def digest(self, outcomes, ctx):
        highlights = [h for o in outcomes if o.status == "done" for h in o.data.get("highlights") or []]
        highlights.sort(key=lambda h: -h["score"])
        highlights = highlights[:MAX_HIGHLIGHTS]
        if not highlights:
            return None
        now = ctx.now.astimezone()
        out = [f"RSS Watch Brief \u00b7 {now.strftime('%Y-%m-%d %H:%M %Z')}"]
        for i, h in enumerate(highlights):
            out += ["", SEPARATOR, f":{NUMBER_EMOJI[i]}: {h['title']}"]
            meta = " \u00b7 ".join(x for x in (self._clock(h["published"]), h["source"]) if x)
            if meta:
                out.append(meta)
            if h["summary"]:
                out += ["", h["summary"]]
            if h["do_next"] or h["kill_if"]:
                out.append("")
            if h["do_next"]:
                out.append(f"Do next: {h['do_next']}")
            if h["kill_if"]:
                out.append(f"Kill if: {h['kill_if']}")
            out += [f"\u2022 {loopkit.chat_url(link)}" for link in h["links"] if loopkit.chat_url(link)]
        out += ["", SEPARATOR, f"Run timestamp {now.strftime('%Y-%m-%d %H:%M %Z')}"]
        return "\n".join(out)


if __name__ == "__main__":
    sys.exit(loopkit.main(RSSWatch()))
