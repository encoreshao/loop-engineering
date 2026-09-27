import base64
import hashlib
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import mail_auth  # noqa: E402
from mail_stub import StubServer  # noqa: E402

GOOGLE = {"client_id": "gid", "client_secret": "gsecret"}


class _FakeRun:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.calls = []
        self.result = subprocess.CompletedProcess([], returncode, stdout, stderr)

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return self.result


@pytest.fixture(autouse=True)
def _clear_state():
    mail_auth._PENDING.clear()
    mail_auth._DEVICE_FLOWS.clear()


def test_keychain_service_is_sandboxed_when_home_env_set(monkeypatch, tmp_path):
    monkeypatch.delenv("LOOP_ENGINEERING_HOME", raising=False)
    assert mail_auth.keychain_service() == "loop-engineering.mail"
    monkeypatch.setenv("LOOP_ENGINEERING_HOME", str(tmp_path))
    service = mail_auth.keychain_service()
    assert service.startswith("loop-engineering.mail.sandbox-") and len(service.split("-")[-1]) == 10


def test_keychain_get_found_missing_and_error(monkeypatch):
    fake = _FakeRun(0, "refresh-tok\n")
    monkeypatch.setattr(mail_auth.subprocess, "run", fake)
    assert mail_auth.keychain_get("work") == "refresh-tok"
    cmd, kwargs = fake.calls[0]
    assert cmd[:2] == ["/usr/bin/security", "find-generic-password"]
    assert ["-a", "work"] == cmd[cmd.index("-a"):cmd.index("-a") + 2]
    assert cmd[cmd.index("-s") + 1] == mail_auth.keychain_service()
    assert "-w" in cmd and kwargs["timeout"] == 10

    monkeypatch.setattr(mail_auth.subprocess, "run", _FakeRun(44, "", "could not be found"))
    assert mail_auth.keychain_get("work") is None

    monkeypatch.setattr(mail_auth.subprocess, "run", _FakeRun(51, "", "user interaction is not allowed"))
    with pytest.raises(mail_auth.KeychainError):
        mail_auth.keychain_get("work")


def test_keychain_get_timeout_is_keychain_error(monkeypatch):
    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 10)
    monkeypatch.setattr(mail_auth.subprocess, "run", boom)
    with pytest.raises(mail_auth.KeychainError, match="locked"):
        mail_auth.keychain_get("work")


def test_keychain_set_passes_the_token_on_stdin_never_argv(monkeypatch):
    fake = _FakeRun(0)
    monkeypatch.setattr(mail_auth.subprocess, "run", fake)
    mail_auth.keychain_set("work", "s3cret-refresh-token")
    cmd, kwargs = fake.calls[0]
    assert cmd == ["/usr/bin/security", "-i"]
    assert "s3cret-refresh-token" not in " ".join(cmd)
    line = kwargs["input"]
    assert line.endswith("\n") and line.count("\n") == 1
    assert line.startswith("add-generic-password -U ")
    assert f'-s "{mail_auth.keychain_service()}"' in line
    assert '-a "work"' in line
    assert '-w "s3cret-refresh-token"' in line
    assert line.rstrip("\n").endswith("-T /usr/bin/security")


@pytest.mark.parametrize("token, quoted", [
    ('a"b', '"a\\"b"'),
    ("a\\b", '"a\\\\b"'),
    ("1//0g-A_b.c*d/e", '"1//0g-A_b.c*d/e"'),
    ('\\"', '"\\\\\\""'),
])
def test_keychain_set_quotes_tokens_for_security_interactive_mode(monkeypatch, token, quoted):
    fake = _FakeRun(0)
    monkeypatch.setattr(mail_auth.subprocess, "run", fake)
    mail_auth.keychain_set("work", token)
    assert f"-w {quoted} -T" in fake.calls[0][1]["input"]


def test_keychain_set_rejects_a_token_with_a_line_break(monkeypatch):
    fake = _FakeRun(0)
    monkeypatch.setattr(mail_auth.subprocess, "run", fake)
    with pytest.raises(mail_auth.KeychainError):
        mail_auth.keychain_set("work", "abc\ndelete-generic-password -s x")
    assert fake.calls == []


def test_keychain_set_failure_is_keychain_error_without_the_token(monkeypatch):
    monkeypatch.setattr(mail_auth.subprocess, "run", _FakeRun(1, stderr="add-generic-password: returned 1"))
    with pytest.raises(mail_auth.KeychainError) as info:
        mail_auth.keychain_set("work", "s3cret")
    assert "s3cret" not in str(info.value)


def test_keychain_delete_ignores_missing(monkeypatch):
    monkeypatch.setattr(mail_auth.subprocess, "run", _FakeRun(44))
    mail_auth.keychain_delete("work")


def test_pkce_is_s256():
    verifier, challenge = mail_auth.make_pkce()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_pending_state_single_use_and_expires():
    state = mail_auth.create_pending_state("work", "ver", "http://127.0.0.1:1/cb", now=1000)
    assert mail_auth.consume_pending_state(state, now=1100)["inbox"] == "work"
    assert mail_auth.consume_pending_state(state, now=1100) is None
    old = mail_auth.create_pending_state("work", "ver", "http://127.0.0.1:1/cb", now=1000)
    assert mail_auth.consume_pending_state(old, now=1000 + 601) is None
    assert mail_auth.consume_pending_state("unknown", now=1000) is None


def test_google_auth_url_params():
    url = mail_auth.google_auth_url("gid", "http://127.0.0.1:8420/oauth/google/callback", "st", "ch", "me@x.com")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert query["scope"] == ["https://www.googleapis.com/auth/gmail.modify"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
    assert query["state"] == ["st"] and query["login_hint"] == ["me@x.com"]


def test_google_exchange_and_refresh():
    with StubServer() as stub:
        stub.add("POST", "/token", body={"access_token": "at", "refresh_token": "rt"})
        tokens = mail_auth.google_exchange_code("code", "ver", "http://cb", GOOGLE, token_url=stub.base_url + "/token")
        assert tokens["refresh_token"] == "rt"
        sent = urllib.parse.parse_qs(stub.requests[0]["body"])
        assert sent["grant_type"] == ["authorization_code"] and sent["code_verifier"] == ["ver"]

    with StubServer() as stub:
        stub.add("POST", "/token", body={"access_token": "fresh"})
        assert mail_auth.google_refresh("rt", GOOGLE, token_url=stub.base_url + "/token") == "fresh"


def test_google_exchange_without_refresh_token_is_error():
    with StubServer() as stub:
        stub.add("POST", "/token", body={"access_token": "at"})
        with pytest.raises(mail_auth.AuthFlowError, match="refresh token"):
            mail_auth.google_exchange_code("c", "v", "http://cb", GOOGLE, token_url=stub.base_url + "/token")


def test_google_refresh_invalid_grant_requires_reauth():
    with StubServer() as stub:
        stub.add("POST", "/token", status=400, body={"error": "invalid_grant"})
        with pytest.raises(mail_auth.ReauthRequired):
            mail_auth.google_refresh("rt", GOOGLE, token_url=stub.base_url + "/token")


def test_ms_device_code_scope_never_includes_send():
    with StubServer() as stub:
        stub.add("POST", "/devicecode", body={"device_code": "dc", "user_code": "ABCD", "verification_uri": "https://microsoft.com/devicelogin", "interval": 5, "expires_in": 900})
        info = mail_auth.ms_start_device_code("mid", device_url=stub.base_url + "/devicecode")
        scope = urllib.parse.parse_qs(stub.requests[0]["body"])["scope"][0]
    assert info["user_code"] == "ABCD"
    assert "Mail.ReadWrite" in scope and "offline_access" in scope and "Send" not in scope


@pytest.mark.parametrize("error,expected", [
    ("authorization_pending", "pending"), ("slow_down", "slow_down"),
    ("expired_token", "expired"), ("bad_verification_code", "expired"),
    ("authorization_declined", "declined"),
])
def test_ms_poll_once_error_mapping(error, expected):
    with StubServer() as stub:
        stub.add("POST", "/token", status=400, body={"error": error})
        assert mail_auth.ms_poll_once("mid", "dc", token_url=stub.base_url + "/token") == (expected, None)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_ms_poll_once_transient_http_error_is_pending(status):
    with StubServer() as stub:
        stub.add("POST", "/token", status=status, body={"error": "server_error"})
        assert mail_auth.ms_poll_once("mid", "dc", token_url=stub.base_url + "/token") == ("pending", None)


def test_ms_poll_once_unmappable_error_raises_auth_flow_error():
    with StubServer() as stub:
        stub.add("POST", "/token", status=400, body={"error": "invalid_client"})
        with pytest.raises(mail_auth.AuthFlowError):
            mail_auth.ms_poll_once("mid", "dc", token_url=stub.base_url + "/token")


def test_ms_poll_once_success_and_refresh_rotation():
    with StubServer() as stub:
        stub.add("POST", "/token", body={"access_token": "at", "refresh_token": "rt"})
        assert mail_auth.ms_poll_once("mid", "dc", token_url=stub.base_url + "/token") == ("success", {"access_token": "at", "refresh_token": "rt"})
    with StubServer() as stub:
        stub.add("POST", "/token", body={"access_token": "at2", "refresh_token": "rt2"})
        assert mail_auth.ms_refresh("rt", "mid", token_url=stub.base_url + "/token") == ("at2", "rt2")


def test_start_device_flow_runs_to_success(monkeypatch):
    monkeypatch.setattr(mail_auth, "ms_start_device_code", lambda client_id: {
        "device_code": "dc", "user_code": "ABCD", "verification_uri": "https://microsoft.com/devicelogin",
        "interval": 5, "expires_in": 900})
    results = iter([("pending", None), ("slow_down", None), ("success", {"refresh_token": "rt", "access_token": "at"})])
    monkeypatch.setattr(mail_auth, "ms_poll_once", lambda client_id, device_code: next(results))
    sleeps, got = [], []
    info = mail_auth.start_device_flow("home", "mid", on_success=got.append,
                                       run_in_background=lambda fn: fn(), sleep=sleeps.append, now=lambda: 0)
    assert info["user_code"] == "ABCD"
    assert got == [{"refresh_token": "rt", "access_token": "at"}]
    assert sleeps == [5, 5, 10]
    assert mail_auth.device_flow_status("home")["state"] == "connected"


def test_start_device_flow_on_success_failure_is_reported(monkeypatch):
    monkeypatch.setattr(mail_auth, "ms_start_device_code", lambda client_id: {
        "device_code": "dc", "user_code": "X", "verification_uri": "u", "interval": 1, "expires_in": 900})
    monkeypatch.setattr(mail_auth, "ms_poll_once", lambda client_id, device_code: ("success", {"refresh_token": "rt"}))

    def fail(tokens):
        raise mail_auth.AuthFlowError("Signed in as other@x.com, expected me@x.com")
    mail_auth.start_device_flow("home", "mid", on_success=fail, run_in_background=lambda fn: fn(),
                                sleep=lambda s: None, now=lambda: 0)
    status = mail_auth.device_flow_status("home")
    assert status["state"] == "failed" and "other@x.com" in status["message"]


def test_start_device_flow_poll_unexpected_error_is_reported(monkeypatch):
    monkeypatch.setattr(mail_auth, "ms_start_device_code", lambda client_id: {
        "device_code": "dc", "user_code": "X", "verification_uri": "u", "interval": 1, "expires_in": 900})

    def boom(client_id, device_code):
        raise ValueError("network exploded")
    monkeypatch.setattr(mail_auth, "ms_poll_once", boom)
    mail_auth.start_device_flow("home", "mid", on_success=lambda tokens: None,
                                run_in_background=lambda fn: fn(), sleep=lambda s: None, now=lambda: 0)
    status = mail_auth.device_flow_status("home")
    assert status["state"] == "failed" and "network exploded" in status["message"]


def test_start_device_flow_on_success_unexpected_error_is_reported(monkeypatch):
    monkeypatch.setattr(mail_auth, "ms_start_device_code", lambda client_id: {
        "device_code": "dc", "user_code": "X", "verification_uri": "u", "interval": 1, "expires_in": 900})
    monkeypatch.setattr(mail_auth, "ms_poll_once", lambda client_id, device_code: ("success", {}))

    def fail(tokens):
        raise KeyError("refresh_token")
    mail_auth.start_device_flow("home", "mid", on_success=fail, run_in_background=lambda fn: fn(),
                                sleep=lambda s: None, now=lambda: 0)
    status = mail_auth.device_flow_status("home")
    assert status["state"] == "failed"


def test_device_flow_status_unknown():
    assert mail_auth.device_flow_status("nobody") == {"state": "none"}


def test_get_access_token_not_connected_requires_reauth(monkeypatch):
    monkeypatch.setattr(mail_auth, "keychain_get", lambda account: None)
    with pytest.raises(mail_auth.ReauthRequired, match="not connected"):
        mail_auth.get_access_token({"name": "w", "provider": "gmail"}, oauth={"google": GOOGLE})


def test_get_access_token_outlook_stores_rotated_refresh_token(monkeypatch):
    stored = {}
    monkeypatch.setattr(mail_auth, "keychain_get", lambda account: "rt-old")
    monkeypatch.setattr(mail_auth, "keychain_set", lambda account, secret: stored.update({account: secret}))
    monkeypatch.setattr(mail_auth, "ms_refresh", lambda rt, client_id: ("at", "rt-new"))
    token = mail_auth.get_access_token({"name": "home", "provider": "outlook"}, oauth={"microsoft": {"client_id": "mid"}})
    assert token == "at" and stored == {"home": "rt-new"}


def test_get_access_token_missing_client_config_requires_reauth(monkeypatch):
    monkeypatch.setattr(mail_auth, "keychain_get", lambda account: "rt")
    with pytest.raises(mail_auth.ReauthRequired, match="OAuth client"):
        mail_auth.get_access_token({"name": "w", "provider": "gmail"}, oauth={})
