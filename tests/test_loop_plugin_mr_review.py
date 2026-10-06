import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import mail_http
from loop_plugins import mr_review as mr

REPO = Path(__file__).resolve().parent.parent
DIFF = [{"new_path": "app/a.rb", "diff": "@@ -1,2 +1,3 @@\n a\n+b = eval(params[:x])\n c\n"}]
MR = {"project_id": 9, "iid": 7, "web_url": "https://gl/g/p/-/merge_requests/7",
      "diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"}}


class Conn:
    def __init__(self, fail_position=False):
        self.calls = []; self.fail_position = fail_position
    def api(self, method, path, json_body=None, **kw):
        self.calls.append((method, path, json_body))
        if self.fail_position and json_body and "position" in json_body:
            raise mail_http.MailHTTPError(400, "line_code invalid", "https://gl" + path)
        return {}


def test_new_head_sha_is_new_item():
    assert mr.item_key("work", 9, 7, "aaa") != mr.item_key("work", 9, 7, "bbb")
    assert mr.item_key("work", 9, 7, "h") == "mr:work:9!7@h"


def test_diff_over_cap_is_truncated_with_marker():
    big = [{"new_path": "x", "diff": "+" + "y" * 200_000}]
    text, truncated = mr.build_diff_text(big, cap=1000)
    assert truncated and len(text) <= 1200 and "[diff truncated" in text


def test_small_diff_not_truncated():
    text, truncated = mr.build_diff_text(DIFF)
    assert not truncated and "eval(params" in text


def test_findings_only_posted_as_draft_notes():
    conn = Conn()
    mr.post_review(conn, MR, {"summary": "s", "findings": [{"path": "app/a.rb", "line": 2, "severity": "blocker", "body": "eval on params"}]})
    paths = [p for _, p, _ in conn.calls]
    assert all(p.endswith("/draft_notes") for p in paths)
    assert not any("bulk_publish" in p or "approve" in p or p.endswith("/notes") for p in paths)
    assert all(m == "POST" for m, _, _ in conn.calls)
    first = conn.calls[0][2]
    assert first["position"] == {"position_type": "text", "base_sha": "b", "start_sha": "s", "head_sha": "h", "new_path": "app/a.rb", "new_line": 2}
    assert first["note"].startswith("**[blocker]** eval on params")


def test_position_rejected_falls_back_to_general_draft():
    conn = Conn(fail_position=True)
    out = mr.post_review(conn, MR, {"summary": "s", "findings": [{"path": "app/a.rb", "line": 2, "severity": "major", "body": "x"}]})
    assert out["fallback_general"] == 1
    assert conn.calls[1][2]["note"].startswith("`app/a.rb:2`")
    assert "position" not in conn.calls[1][2]


def test_other_http_errors_propagate():
    class Boom(Conn):
        def api(self, *a, **k):
            raise mail_http.MailHTTPError(500, "x", "u")
    with pytest.raises(mail_http.MailHTTPError):
        mr.post_review(Boom(), MR, {"summary": "s", "findings": [{"path": "a", "line": 1, "severity": "major", "body": "x"}]})


def test_mentions_defused_in_posted_text():
    conn = Conn()
    mr.post_review(conn, MR, {"summary": "cc @all", "findings": [{"path": "app/a.rb", "line": 2, "severity": "major", "body": "ping @group/team and a@b.com"}]})
    notes = [c[2]["note"] for c in conn.calls]
    assert "@​all" in notes[1] and "@​group" in notes[0]
    assert "a@b.com" in notes[0]
    assert not any(" @all" in n or " @group" in n for n in notes)


def _plugin(posted):
    return mr.MRReview(loader=lambda i: Conn(), poster=lambda conn, m, a: posted.append(a) or {"drafts": len(a["findings"]), "fallback_general": 0})


class C:
    settings = {"min_severity": "minor"}; log = staticmethod(lambda m: None)


def _item():
    return mr.loopkit.WorkItem("k", "t", payload={**MR, "account": "work", "diff": mr.build_diff_text(DIFF)[0], "changed_paths": ["app/a.rb"]})


def test_after_item_filters_severity_unknown_paths_and_caps():
    findings = [{"path": "app/a.rb", "line": 2, "severity": "nit", "body": "n"},
                {"path": "ghost.rb", "line": 1, "severity": "blocker", "body": "g"}] + \
               [{"path": "app/a.rb", "line": 2, "severity": "major", "body": str(i)} for i in range(30)]
    posted = []
    _plugin(posted).after_item(_item(), {"summary": "s", "findings": findings}, C())
    kept = posted[0]["findings"]
    assert len(kept) == mr.MAX_FINDINGS and all(f["path"] == "app/a.rb" and f["severity"] != "nit" for f in kept)


def test_after_item_drops_malformed_and_trims():
    findings = [{"path": "app/a.rb", "line": "2", "severity": "major", "body": "str line"},
                {"path": "app/a.rb", "line": True, "severity": "major", "body": "bool line"},
                {"path": "app/a.rb", "line": 2, "severity": "catastrophic", "body": "bad sev"},
                {"path": "app/a.rb", "line": 2, "severity": "major", "body": 5},
                "junk",
                {"path": "app/a.rb", "line": 2, "severity": "major", "body": "z" * 5000}]
    posted = []
    _plugin(posted).after_item(_item(), {"summary": "s" * 900, "findings": findings}, C())
    kept = posted[0]["findings"]
    assert len(kept) == 1 and len(kept[0]["body"]) == 2000
    assert len(posted[0]["summary"]) <= 600


def test_after_item_outcome_counts_blockers():
    posted = []
    out = _plugin(posted).after_item(_item(), {"summary": "s", "findings": [
        {"path": "app/a.rb", "line": 2, "severity": "blocker", "body": "b"}]}, C())
    assert out.status == "done" and out.summary == "1 draft notes (1 blocker)" and out.url == MR["web_url"]


def test_digest_text_is_chat_safe():
    o = mr.loopkit.Outcome("k", "done", "x", url="https://gl/mr/1", data={"title": "T <!channel>", "drafts": 2, "blockers": 1})
    text = mr.MRReview().digest([o], C())
    assert text.startswith("MR pre-review: 1 MRs, 2 draft notes (1 blockers). Open each MR")
    assert "<" not in text and "(https://gl/mr/1)" in text
    assert mr.MRReview().digest([], C()) is None


class D:
    def api(self, method, path, **kw):
        if path == "/user": return {"id": 1, "username": "me"}
        if path.startswith("/merge_requests?"): return [
            {"project_id": 9, "iid": 7, "author": {"id": 2}, "title": "T", "web_url": "u"},
            {"project_id": 9, "iid": 8, "author": {"id": 1}, "title": "mine", "web_url": "u"}]
        if path.endswith("/merge_requests/7"): return {"sha": "h", "diff_refs": MR["diff_refs"], "description": "d" * 9000}
        if "/diffs" in path: return DIFF if "page=1" in path else []
        raise AssertionError(path)


def test_discover_skips_own_mrs_and_builds_keys():
    plugin = mr.MRReview(accounts_fn=lambda cap: [{"id": "work", "type": "gitlab"}], loader=lambda i: D())
    items = plugin.discover(C())
    assert [i.key for i in items] == ["mr:work:9!7@h"]
    p = items[0].payload
    assert p["changed_paths"] == ["app/a.rb"] and len(p["description"]) <= 4096 and p["truncated"] is False


def test_discover_isolates_accounts_and_mrs():
    logs = []
    class Ctx(C):
        log = staticmethod(logs.append)
    class Bad:
        def api(self, *a, **k): raise RuntimeError("secret https://x/token")
    def loader(i): return Bad() if i == "bad" else D()
    accounts = [{"id": "bad", "type": "gitlab"}, {"id": "gh", "type": "github"}, {"id": "work", "type": "gitlab"}]
    items = mr.MRReview(accounts_fn=lambda cap: accounts, loader=loader).discover(Ctx())
    assert [i.key for i in items] == ["mr:work:9!7@h"]
    assert logs and all("secret" not in l and "token" not in l for l in logs)


def test_definition_and_template():
    from loop_definition import LoopDefinition
    from loop_policy import PolicyEngine
    d = LoopDefinition.from_yaml(REPO / "loops" / "mr-review" / "loop.yaml")
    assert "create_draft_review_note" in d.actions and PolicyEngine().validate_definition(d) == []
    tpl = json.loads((REPO / "config" / "loops.json.template").read_text())
    e = next(x for x in tpl if x["name"] == "mr-review-loop")
    assert e["requires"] == ["merge_requests"] and e["routes_notifications"] is True
    assert e["schedule"] == {"frequency": "hourly", "interval_hours": 2}
    assert e["entry_point"] == "bin.loop_plugins.mr_review" and e["enabled"] is False
    prompt = (REPO / "loops" / "mr-review" / "prompt.md").read_text()
    assert "untrusted" in prompt and "{{item_json}}" in prompt
