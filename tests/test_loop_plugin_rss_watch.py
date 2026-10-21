import json, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import seen_store
import loopkit
from loop_plugins import rss_watch as rw

NOW = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)


class Feed:
    def __init__(self, n): self.n = n; self.settings = {"feeds": "https://a/feed"}
    def entries(self, url):
        return [{"title": f"t{i}", "link": f"https://a/{i}", "id": f"https://a/{i}", "published": ""} for i in range(self.n)]


def plugin(tmp_path, n=3):
    return rw.RSSWatch(accounts_fn=lambda cap: [{"id": "news", "type": "rss"}], loader=lambda i: Feed(n),
                       seen=seen_store.SeenStore("rss-watch-loop-entries", state_dir=tmp_path, now_fn=lambda: NOW))


class C:
    now = NOW; settings = {"interests": "rails"}; log = staticmethod(lambda m: None)


def test_entry_cap_40(tmp_path):
    items = plugin(tmp_path, n=100).discover(C())
    assert len(items) == 1 and len(items[0].payload["entries"]) == 40
    assert items[0].key == "rss:news:2026-10-06" and items[0].payload["interests"] == "rails"


def test_entries_marked_seen_after_item(tmp_path):
    p = plugin(tmp_path)
    item = p.discover(C())[0]
    p.after_item(item, {"highlights": [{"title": "t0", "link": "https://a/0", "why": "w", "score": 5}]}, C())
    assert plugin(tmp_path).discover(C()) == []          # all 3 offered entries now seen


def test_no_item_when_nothing_new(tmp_path):
    assert plugin(tmp_path, n=0).discover(C()) == []


def test_unknown_links_dropped(tmp_path):
    p = plugin(tmp_path)
    item = p.discover(C())[0]
    out = p.after_item(item, {"highlights": [{"title": "x", "link": "https://evil/1", "why": "w", "score": 5},
                                             {"title": "t1", "link": "https://a/1", "why": "w", "score": 3}]}, C())
    assert [h["link"] for h in out.data["highlights"]] == ["https://a/1"]


def test_untrusted_entries_trimmed(tmp_path):
    class Bad(Feed):
        def entries(self, url):
            return [{"title": "x" * 500, "link": "https://a/ok", "id": "1", "published": ""},
                    {"title": "bad", "link": "javascript:alert(1)", "id": "2", "published": ""}]
    p = rw.RSSWatch(accounts_fn=lambda cap: [{"id": "news"}], loader=lambda i: Bad(1),
                    seen=seen_store.SeenStore("rss-watch-loop-entries", state_dir=tmp_path, now_fn=lambda: NOW))
    entries = p.discover(C())[0].payload["entries"]
    assert len(entries) == 1 and len(entries[0]["title"]) <= 200


def test_highlights_capped_sorted_and_digest(tmp_path):
    p = plugin(tmp_path, n=15)
    item = p.discover(C())[0]
    hl = [{"title": f"t{i}", "link": f"https://a/{i}", "why": "w" * 300, "score": 1 + i % 5} for i in range(15)]
    out = p.after_item(item, {"highlights": hl}, C())
    got = out.data["highlights"]
    assert len(got) == 10 and len(got[0]["summary"]) <= 500
    assert [h["score"] for h in got] == sorted((h["score"] for h in got), reverse=True)
    text = p.digest([out], C())
    assert "news" in text and "https://a/" in text


def test_digest_numbered_brief_layout(tmp_path):
    p = plugin(tmp_path, n=3)
    item = p.discover(C())[0]
    hl = [{"title": "Voxtral 3", "link": "https://a/0", "links": ["https://a/1", "https://evil/x"],
           "source": "Mistral AI", "summary": "Voice stack.", "do_next": "Pilot it.", "kill_if": "No edge.", "score": 5},
          {"title": "Second", "link": "https://a/2", "summary": "S2.", "score": 3}]
    out = p.after_item(item, {"highlights": hl}, C())
    lines = p.digest([out], C()).splitlines()
    assert lines[0].startswith("RSS Watch Brief \u00b7 2026-10-06")
    assert ":one: Voxtral 3" in lines and ":two: Second" in lines
    assert "Do next: Pilot it." in lines and "Kill if: No edge." in lines
    assert "\u2022 https://a/0" in lines and "\u2022 https://a/1" in lines
    assert not any("evil" in l for l in lines)
    assert lines[-1].startswith("Run timestamp 2026-10-06")


def test_settings_fields_declared():
    assert [f.key for f in rw.SETTINGS_FIELDS] == ["interests"]
    f = rw.SETTINGS_FIELDS[0]
    assert f.kind == "textarea" and not f.required
    assert rw.RSSWatch.settings_fields == rw.SETTINGS_FIELDS
    assert loopkit.LoopPlugin.settings_fields == ()


def test_persistent_feed_entries_never_expire_from_seen(tmp_path):
    from datetime import timedelta
    clock = {"now": NOW}

    class Growing:
        settings = {"feeds": "https://a/feed"}
        def __init__(self, n): self.n = n
        def entries(self, url):
            return [{"title": f"t{i}", "link": f"https://a/{i}", "published": ""} for i in range(self.n)]

    def mk(n):
        return rw.RSSWatch(accounts_fn=lambda cap: [{"id": "news", "type": "rss"}], loader=lambda i: Growing(n),
                           seen=seen_store.SeenStore("rss-watch-loop-entries", state_dir=tmp_path,
                                                     now_fn=lambda: clock["now"]))

    class Ctx(C):
        pass
    p = mk(3)
    p.after_item(p.discover(Ctx())[0], {"highlights": []}, Ctx())
    clock["now"] += timedelta(days=31)           # entries 0-2 still in the feed, plus a new one
    Ctx.now = clock["now"]
    p = mk(4)
    items = p.discover(Ctx())
    assert [e["link"] for e in items[0].payload["entries"]] == ["https://a/3"]
    p.after_item(items[0], {"highlights": []}, Ctx())
    clock["now"] += timedelta(days=1)
    Ctx.now = clock["now"]
    assert mk(4).discover(Ctx()) == []
