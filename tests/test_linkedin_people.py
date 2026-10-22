import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import linkedin_people as lp

NOW = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
ROW = {"name": "Ada", "url": "https://www.linkedin.com/in/ada", "avatar": "", "company": "Acme", "position": "Eng"}


def test_merge_adds_new_people_and_reports_how_many_were_new(tmp_path):
    p = tmp_path / "people.json"
    assert lp.merge([ROW, dict(ROW, url="https://www.linkedin.com/in/bob", name="Bob")], "AI builder", NOW, path=p) == 2
    assert lp.merge([ROW], "AI builder", NOW, path=p) == 0
    assert [r["name"] for r in lp.load(path=p)] == ["Bob", "Ada"] or len(lp.load(path=p)) == 2


def test_known_urls_and_update_keeps_first_seen(tmp_path):
    p = tmp_path / "people.json"
    lp.merge([ROW], "q", NOW, path=p)
    later = datetime(2026, 10, 23, tzinfo=timezone.utc)
    lp.merge([dict(ROW, position="CTO")], "q", later, path=p)
    [r] = lp.load(path=p)
    assert lp.known_urls(path=p) == {ROW["url"]}
    assert r["position"] == "CTO" and r["first_seen"].startswith("2026-10-22") and r["last_seen"].startswith("2026-10-23")


def test_load_survives_a_corrupt_file(tmp_path):
    p = tmp_path / "people.json"
    p.write_text("{nope")
    assert lp.load(path=p) == [] and lp.known_urls(path=p) == set()


def test_target_is_clamped_to_20_50():
    assert (lp.target(None), lp.target("5"), lp.target("99"), lp.target("35"), lp.target("x")) == (30, 20, 50, 35, 30)
