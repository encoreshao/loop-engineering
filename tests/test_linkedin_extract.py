import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import linkedin_extract as le

DUMP = {"text": "Ada Lovelace\nAI builder at Acme\nGrace Hopper", "links": [],
        "cards": [{"url": "https://www.linkedin.com/in/ada", "avatar": "https://media.licdn.com/a.jpg", "text": "Ada Lovelace AI builder at Acme"},
                  {"url": "https://www.linkedin.com/in/grace", "avatar": "", "text": "Grace Hopper"}]}


def call_returning(obj):
    prompts = []
    def call(prompt):
        prompts.append(prompt)
        return {"text": json.dumps(obj) if not isinstance(obj, str) else obj, "cost_usd": 0.01}
    call.prompts = prompts
    return call


def test_people_are_validated_against_the_offered_cards():
    call = call_returning({"items": [
        {"name": "Ada <@U1> Lovelace", "url": "https://www.linkedin.com/in/ada?x=1", "avatar": "https://media.licdn.com/a.jpg",
         "company": "Acme", "position": "AI builder"},
        {"name": "Grace", "url": "https://www.linkedin.com/in/grace", "avatar": "https://evil.example/x.png", "company": "", "position": "Admiral"},
        {"name": "Ghost", "url": "https://www.linkedin.com/in/ghost", "avatar": "", "company": "", "position": ""},
        {"name": "", "url": "https://www.linkedin.com/in/ada"}, "junk"]})
    rows = le.extract("people", DUMP, call=call)
    assert [r["url"] for r in rows] == ["https://www.linkedin.com/in/ada", "https://www.linkedin.com/in/grace"]
    assert "<" not in rows[0]["name"] and rows[0]["avatar"] == "https://media.licdn.com/a.jpg"
    assert rows[1]["avatar"] == "" and rows[1]["position"] == "Admiral"


def test_the_page_dump_goes_into_the_prompt_as_untrusted_data():
    call = call_returning({"items": []})
    le.extract("people", DUMP, call=call)
    assert "untrusted" in call.prompts[0].lower() and "Ada Lovelace" in call.prompts[0]


def test_feed_rows_get_stable_ids_and_only_linkedin_urls():
    call = call_returning({"items": [
        {"text": "Shipping agents", "author": "Ada", "author_url": "https://www.linkedin.com/in/ada/", "url": "https://www.linkedin.com/feed/update/urn:li:activity:9/"},
        {"text": "Other", "author": "X", "author_url": "javascript:1", "url": "https://evil.example/"}, {"text": ""}]})
    rows = le.extract("feed", DUMP, call=call)
    again = le.extract("feed", DUMP, call=call)
    assert [r["id"] for r in rows] == [r["id"] for r in again] and len(rows) == 2
    assert rows[0]["profile"] == "https://www.linkedin.com/in/ada" and rows[0]["url"].endswith("urn:li:activity:9/")
    assert rows[1]["profile"] == "" and rows[1]["url"] == ""


def test_unparseable_or_failing_model_returns_nothing_instead_of_raising():
    assert le.extract("feed", DUMP, call=call_returning("not json")) == []
    def boom(prompt): raise RuntimeError("x")
    assert le.extract("feed", DUMP, call=boom) == []


def test_an_empty_page_never_calls_the_model():
    call = call_returning({"items": []})
    assert le.extract("feed", {"text": "  ", "cards": [], "links": []}, call=call) == []
    assert call.prompts == []
