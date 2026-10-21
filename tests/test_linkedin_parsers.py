import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import linkedin_browser as lb

NOTIFICATIONS = """
<main>
 <article class="nt-card nt-card--unread">
  <a href="/feed/update/urn:li:activity:111/?trk=x">Ada Lovelace commented on your post:
     <span>  "Great   write-up" </span></a>
 </article>
 <article class="nt-card"><a href="https://www.linkedin.com/in/grace-hopper/?miniProfileUrn=1">Grace Hopper viewed your profile</a></article>
 <article class="other"><a href="/x">not a notification</a></article>
 <article class="nt-card"></article>
</main>"""

FEED = """
<div class="scaffold-finite-scroll">
 <div class="feed-shared-update-v2" data-urn="urn:li:activity:7001">
   <a href="/in/ada-lovelace/?trk=feed">Ada Lovelace</a>
   <div>We shipped <b>Rails 9</b> today. <br> Details inside.</div>
   <div data-urn="urn:li:activity:7001"><a href="/in/dup/">dup nested</a></div>
 </div>
 <div data-urn="urn:li:comment:(1,2)">a comment, not a post</div>
 <div class="feed-shared-update-v2" data-urn="urn:li:activity:7002"><span>Hiring a staff engineer</span></div>
</div>"""

MESSAGES = """
<ul>
 <li class="msg-conversation-listitem"><a href="/messaging/thread/2-abc/">Linus: are you free <b>Friday</b>?</a></li>
 <li class="msg-conversation-listitem"></li>
 <li class="unrelated">x</li>
</ul>"""


def test_parse_notifications():
    rows = lb.parse_notifications(NOTIFICATIONS)
    assert [r["text"] for r in rows] == ['Ada Lovelace commented on your post: "Great write-up"',
                                         "Grace Hopper viewed your profile"]
    assert rows[0]["url"] == "https://www.linkedin.com/feed/update/urn:li:activity:111/"
    assert rows[1]["url"] == "https://www.linkedin.com/in/grace-hopper/"
    assert all(r["id"].startswith("notification:") for r in rows) and rows[0]["id"] != rows[1]["id"]


def test_parse_feed_dedupes_and_extracts_profile():
    rows = lb.parse_feed(FEED)
    assert [r["id"] for r in rows] == ["urn:li:activity:7001", "urn:li:activity:7002"]
    assert rows[0]["text"].startswith("Ada Lovelace We shipped Rails 9 today.")
    assert rows[0]["url"] == "https://www.linkedin.com/feed/update/urn:li:activity:7001/"
    assert rows[0]["profile"] == "https://www.linkedin.com/in/ada-lovelace/"
    assert rows[1]["profile"] == ""


def test_parse_messages():
    rows = lb.parse_messages(MESSAGES)
    assert len(rows) == 1 and rows[0]["text"] == "Linus: are you free Friday?"
    assert rows[0]["url"] == "https://www.linkedin.com/messaging/thread/2-abc/"


def test_text_is_capped():
    html = f'<article class="nt-card"><a href="/x">{"z" * 5000}</a></article>'
    assert len(lb.parse_notifications(html)[0]["text"]) == lb.MAX_TEXT


def test_unrecognised_or_broken_markup_gives_no_rows_not_a_crash():
    for html in ("", "<html><body><p>LinkedIn changed everything</p></body></html>",
                 "<article class='nt-card'><a href='/x'>unclosed", "<<<>>>", "<div></p></li></article>"):
        assert isinstance(lb.parse_notifications(html), list)
        assert isinstance(lb.parse_feed(html), list) and isinstance(lb.parse_messages(html), list)
    assert lb.parse_feed("<p>nothing here</p>") == []


def test_non_linkedin_links_are_dropped():
    html = '<article class="nt-card"><a href="https://evil.example/x">click</a> hi</article>'
    assert lb.parse_notifications(html)[0]["url"] == ""
    html = '<div data-urn="urn:li:activity:9"><a href="https://evil.example/in/x/">p</a>t</div>'
    assert lb.parse_feed(html)[0]["profile"] == ""


def test_detect_challenge_by_url():
    assert lb.detect_challenge("https://www.linkedin.com/checkpoint/challenge/abc") == "challenge"
    assert lb.detect_challenge("https://www.linkedin.com/login?session_redirect=x") == "login"
    assert lb.detect_challenge("https://www.linkedin.com/uas/login") == "login"
    assert lb.detect_challenge("https://www.linkedin.com/authwall?trk=x") == "login"
    assert lb.detect_challenge("https://www.linkedin.com/feed/") is None


def test_detect_challenge_by_headings_only():
    url = "https://www.linkedin.com/feed/"
    assert lb.detect_challenge(url, "<h1>Let's do a quick security check</h1>") == "challenge"
    assert lb.detect_challenge(url, "<html><title>Security Verification | LinkedIn</title></html>") == "challenge"
    assert lb.detect_challenge(url, "<h2>Please solve this CAPTCHA</h2>") == "challenge"
    script_only = "<script>var captcha = 'unusual activity';</script><style>.captcha{}</style><p>ok</p>"
    assert lb.detect_challenge(url, script_only) is None
    post = '<div data-urn="urn:li:activity:1"><p>Someone asked me to solve a CAPTCHA, unusual activity indeed</p></div>'
    assert lb.detect_challenge(url, post) is None
    assert lb.detect_challenge("https://www.linkedin.com/company/challenge-corp/") is None
    assert lb.detect_challenge("https://www.linkedin.com/challenge/abc") == "challenge"


def test_detect_challenge_is_fast_on_hostile_input():
    start = time.perf_counter()
    assert lb.detect_challenge("https://www.linkedin.com/feed/", "<script>" * 40000) is None
    assert time.perf_counter() - start < 1.0


def test_stray_end_tag_does_not_close_a_card_early():
    rows = lb.parse_notifications('<article class="nt-card"><a>one</a></p> tail text</article>')
    assert [r["text"] for r in rows] == ["one tail text"]


def test_unclosed_inline_tag_does_not_swallow_next_row():
    html = '<li class="msg-conversation-listitem"><b>A</li><li class="msg-conversation-listitem"><b>B</li>'
    assert [r["text"] for r in lb.parse_messages(html)] == ["A", "B"]


def test_top_level_stray_end_tag_is_ignored():
    rows = lb.parse_notifications('</div></p><article class="nt-card">hi</article>')
    assert [r["text"] for r in rows] == ["hi"]


def test_lookalike_host_is_not_linkedin():
    assert lb._clean_url("https://www.linkedin.com.evil.example/x") == ""


def test_end_tag_handling_is_linear_on_hostile_nesting():
    for html in ("<div>" * 50000 + "</span>" * 50000, "<div>" * 30000 + "<b></b>" * 30000,
                 "<div>" * 50000 + "</div>" * 50000):
        start = time.perf_counter()
        lb.parse_notifications(html)
        assert time.perf_counter() - start < 1.0
