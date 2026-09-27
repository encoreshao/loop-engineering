import json
import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import mail_http  # noqa: E402
from mail_stub import StubServer  # noqa: E402


def test_get_with_bearer_token_returns_json():
    with StubServer() as stub:
        stub.add("GET", "/x", body={"ok": True})
        assert mail_http.request_json("GET", stub.base_url + "/x", token="tok") == {"ok": True}
        assert stub.requests[0]["headers"]["Authorization"] == "Bearer tok"


def test_json_body_and_form_body_encoding():
    with StubServer() as stub:
        stub.add("POST", "/j", body={}).add("POST", "/f", body={})
        mail_http.request_json("POST", stub.base_url + "/j", json_body={"a": 1})
        mail_http.request_json("POST", stub.base_url + "/f", form={"b": "2 3"})
        assert json.loads(stub.bodies("POST", "/j")[0]) == {"a": 1}
        assert urllib.parse.parse_qs(stub.bodies("POST", "/f")[0]) == {"b": ["2 3"]}


def test_empty_response_body_is_empty_dict():
    with StubServer() as stub:
        stub.add("POST", "/e", status=204, body=None)
        assert mail_http.request_json("POST", stub.base_url + "/e") == {}


def test_retries_429_and_5xx_with_backoff_then_succeeds():
    sleeps = []
    with StubServer() as stub:
        stub.add("GET", "/r", status=503, body={}).add("GET", "/r", status=429, body={}, headers={"Retry-After": "2"})
        stub.add("GET", "/r", body={"done": 1})
        assert mail_http.request_json("GET", stub.base_url + "/r", sleep=sleeps.append) == {"done": 1}
    assert sleeps == [1, 2]


def test_gives_up_after_max_attempts():
    sleeps = []
    with StubServer() as stub:
        stub.add("GET", "/r", status=500, body={"e": 1})
        with pytest.raises(mail_http.MailHTTPError) as info:
            mail_http.request_json("GET", stub.base_url + "/r", sleep=sleeps.append)
    assert info.value.status == 500
    assert sleeps == [1, 4]


def test_401_raises_auth_expired_without_retry():
    sleeps = []
    with StubServer() as stub:
        stub.add("GET", "/a", status=401, body={})
        with pytest.raises(mail_http.AuthExpired):
            mail_http.request_json("GET", stub.base_url + "/a", sleep=sleeps.append)
    assert sleeps == []


def test_400_is_not_retried_and_keeps_body():
    with StubServer() as stub:
        stub.add("POST", "/t", status=400, body={"error": "invalid_grant"})
        with pytest.raises(mail_http.MailHTTPError) as info:
            mail_http.request_json("POST", stub.base_url + "/t", sleep=lambda s: None)
    assert info.value.status == 400
    assert "invalid_grant" in info.value.body


def test_connection_refused_is_retried_then_raises():
    sleeps = []
    with pytest.raises(mail_http.MailHTTPError) as info:
        mail_http.request_json("GET", "http://127.0.0.1:9/nothing", sleep=sleeps.append, timeout=2)
    assert info.value.status is None
    assert sleeps == [1, 4]
