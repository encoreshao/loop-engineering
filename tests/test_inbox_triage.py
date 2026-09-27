import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import inbox_config  # noqa: E402
import inbox_triage  # noqa: E402

CATS = inbox_config.DEFAULT_CATEGORIES
INBOX = {"name": "w", "account": "me@example.com", "urgent_brief": "Clients are urgent.",
         "vip_senders": ["@vip.com"], "exclude_senders": ["@spam.com"]}


def _msg(i, sender="Alice <alice@x.com>", **extra):
    base = {"id": f"m{i}", "thread_id": f"t{i}", "from": sender, "to": "me@example.com", "cc": "",
            "subject": f"Subject {i}", "date": "2026-09-27T08:00:00+00:00", "body_text": f"Body {i}",
            "prior_thread": [], "attachments": [], "_message_id_header": "<x@y>"}
    base.update(extra)
    return base


def test_html_to_text_strips_tags_scripts_and_unescapes():
    markup = "<html><style>p{}</style><script>alert(1)</script><p>Hello&nbsp;<b>there</b></p><br>Line&amp;2<div>Three</div></html>"
    text = inbox_triage.html_to_text(markup)
    assert "alert" not in text and "p{}" not in text
    assert "Hello\xa0there" in text or "Hello there" in text
    assert "Line&2" in text
    assert text.splitlines()[-1].strip() == "Three"


def test_trim():
    assert inbox_triage.trim("abc", 5) == "abc"
    assert inbox_triage.trim("abcdef", 4) == "abc…"
    assert len(inbox_triage.trim("x" * 10000, 4000)) == 4000


def test_filter_excluded():
    kept = inbox_triage.filter_excluded([_msg(1), _msg(2, sender="Bot <bot@spam.com>")], ["@spam.com"])
    assert [m["id"] for m in kept] == ["m1"]


def test_build_prompt_contains_payload_but_no_private_keys():
    prompt = inbox_triage.build_prompt("INSTRUCTIONS", INBOX, CATS, [_msg(1)])
    assert prompt.startswith("INSTRUCTIONS")
    payload = json.loads(prompt.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
    assert payload["account"] == "me@example.com"
    assert payload["urgent_brief"] == "Clients are urgent."
    assert [c["key"] for c in payload["categories"]] == [c["key"] for c in CATS]
    assert payload["messages"][0]["id"] == "m1"
    assert "_message_id_header" not in payload["messages"][0]


def test_extract_json_array_handles_fences_and_prose():
    """Review Focus 3."""
    raw = '[{"id": "m1"}]'
    assert inbox_triage.extract_json_array(raw) == [{"id": "m1"}]
    assert inbox_triage.extract_json_array(f"```json\n{raw}\n```") == [{"id": "m1"}]
    assert inbox_triage.extract_json_array(f"Here you go:\n{raw}\nDone.") == [{"id": "m1"}]
    for bad in ('[{"id": "m1"', "no json here", '{"id": "m1"}'):
        with pytest.raises(inbox_triage.TriageResponseError):
            inbox_triage.extract_json_array(bad)


def _resp(*entries):
    return json.dumps(list(entries))


def test_parse_response_valid_in_message_order():
    messages = [_msg(1), _msg(2)]
    text = _resp(
        {"id": "m2", "category": "fyi", "reason": "update", "draft_body": None},
        {"id": "m1", "category": "urgent", "reason": "deadline", "draft_body": "On it."},
    )
    decisions = inbox_triage.parse_response(text, messages, CATS)
    assert [d["id"] for d in decisions] == ["m1", "m2"]
    assert decisions[0]["draft_body"] == "On it."


@pytest.mark.parametrize("entries,fragment", [
    ([{"id": "m1", "category": "fyi", "reason": "r", "draft_body": None}], "missing"),
    ([{"id": "m1", "category": "fyi", "reason": "r", "draft_body": None},
      {"id": "m2", "category": "fyi", "reason": "r", "draft_body": None},
      {"id": "zzz", "category": "fyi", "reason": "r", "draft_body": None}], "unknown id"),
    ([{"id": "m1", "category": "fyi", "reason": "r", "draft_body": None},
      {"id": "m1", "category": "fyi", "reason": "r", "draft_body": None}], "duplicate"),
    ([{"id": "m1", "category": "spam", "reason": "r", "draft_body": None},
      {"id": "m2", "category": "fyi", "reason": "r", "draft_body": None}], "category"),
    ([{"id": "m1", "category": "fyi", "reason": "r", "draft_body": "hi"},
      {"id": "m2", "category": "fyi", "reason": "r", "draft_body": None}], "draft"),
    ([{"id": "m1", "category": "fyi", "reason": "r" * 201, "draft_body": None},
      {"id": "m2", "category": "fyi", "reason": "r", "draft_body": None}], "reason"),
    ([{"id": "m1", "category": "urgent", "reason": "r", "draft_body": "d" * 4001},
      {"id": "m2", "category": "fyi", "reason": "r", "draft_body": None}], "draft"),
])
def test_parse_response_rejects_invalid(entries, fragment):
    with pytest.raises(inbox_triage.TriageResponseError, match=fragment):
        inbox_triage.parse_response(_resp(*entries), [_msg(1), _msg(2)], CATS)


def test_parse_response_blank_draft_becomes_none():
    decisions = inbox_triage.parse_response(
        _resp({"id": "m1", "category": "urgent", "reason": "r", "draft_body": "   "}), [_msg(1)], CATS)
    assert decisions[0]["draft_body"] is None


def test_apply_rules_vip_forces_urgent_and_flags_manual_reply():
    messages = [_msg(1, sender="Boss <boss@vip.com>")]
    decisions = [{"id": "m1", "category": "fyi", "reason": "update", "draft_body": None}]
    out = inbox_triage.apply_rules(decisions, messages, INBOX)
    assert out[0]["category"] == "urgent"
    assert out[0]["reason"].startswith("VIP sender")
    assert out[0]["needs_manual_reply"] is True


def test_apply_rules_drops_draft_for_own_address():
    """Review Focus 4."""
    messages = [_msg(1, sender="Me <ME@example.com>")]
    decisions = [{"id": "m1", "category": "urgent", "reason": "r", "draft_body": "Reply to myself"}]
    out = inbox_triage.apply_rules(decisions, messages, INBOX)
    assert out[0]["draft_body"] is None
    assert out[0]["needs_manual_reply"] is False


def test_apply_rules_urgent_without_draft_needs_manual_reply():
    out = inbox_triage.apply_rules(
        [{"id": "m1", "category": "urgent", "reason": "r", "draft_body": None}], [_msg(1)], INBOX)
    assert out[0]["needs_manual_reply"] is True


def test_count_by_category():
    assert inbox_triage.count_by_category([{"category": "fyi"}, {"category": "fyi"}, {"category": "urgent"}]) == {"fyi": 2, "urgent": 1}
