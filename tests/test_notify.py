import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import notify
import connectors_config
import mail_http


class FakeConn:
    def __init__(self, caps, fail=False, exc=None):
        self.capabilities = frozenset(caps); self.sent = []; self.fail = fail; self.exc = exc
    def send(self, text, blocks=None):
        if self.exc: raise self.exc
        if self.fail: raise RuntimeError("down")
        self.sent.append(text)


def test_default_routing_when_no_notify_field():
    sent = []
    out = notify.notify("gitlab-loop", "hi", loop_lookup=lambda n: {"name": n},
                        default_sender=lambda t, b: sent.append(t))
    assert sent == ["hi"] and out == [("slack-default", True, "sent")]


def test_routes_to_each_listed_connector():
    conns = {"feishu-team": FakeConn({"notify"}), "slack-ops": FakeConn({"notify"})}
    out = notify.notify("x", "hi", loop_lookup=lambda n: {"notify": ["feishu-team", "slack-ops"]},
                        loader=conns.__getitem__, default_sender=lambda t, b: None)
    assert [o[1] for o in out] == [True, True]
    assert conns["feishu-team"].sent == ["hi"]


def test_non_notify_connector_skipped():
    out = notify.notify("x", "hi", loop_lookup=lambda n: {"notify": ["gh"]},
                        loader=lambda i: FakeConn({"issues"}), default_sender=None)
    assert out == [("gh", False, "not a notify connector")]


def test_one_failure_does_not_stop_others():
    conns = {"a": FakeConn({"notify"}, fail=True), "b": FakeConn({"notify"})}
    out = notify.notify("x", "hi", loop_lookup=lambda n: {"notify": ["a", "b"]}, loader=conns.__getitem__)
    assert out[0][1] is False and out[1][1] is True


def test_unknown_connector_reported():
    def loader(i): raise KeyError(i)
    out = notify.notify("x", "hi", loop_lookup=lambda n: {"notify": ["ghost"]}, loader=loader)
    assert out == [("ghost", False, "unknown connector")]


def test_send_failure_message_has_no_secret():
    secret = "https://hooks.example.com/services/SECRET-TOKEN"
    conn = FakeConn({"notify"}, exc=mail_http.MailHTTPError(500, "x", "<redacted>"))
    conn.secret = secret
    out = notify.notify("x", "hi", loop_lookup=lambda n: {"notify": ["w"]}, loader=lambda i: conn)
    assert out[0][0] == "w" and out[0][1] is False
    assert out[0][2].startswith("MailHTTPError:")
    assert "SECRET-TOKEN" not in out[0][2] and secret not in out[0][2]


def test_config_error_message_is_generic():
    def loader(i): raise connectors_config.ConnectorConfigError("bad json with SECRET")
    out = notify.notify("x", "hi", loop_lookup=lambda n: {"notify": ["a"]}, loader=loader)
    assert out == [("a", False, "connector config error")]


def test_loop_lookup_failure_falls_back_to_default():
    def lookup(n): raise KeyError(n)
    sent = []
    out = notify.notify("ghost", "hi", loop_lookup=lookup, default_sender=lambda t, b: sent.append(t))
    assert out == [("slack-default", True, "sent")] and sent == ["hi"]


def test_default_sender_failure_never_raises():
    def boom(t, b): raise OSError("net")
    out = notify.notify("x", "hi", loop_lookup=lambda n: {}, default_sender=boom)
    assert out[0][0] == "slack-default" and out[0][1] is False


def test_non_list_notify_value_uses_default():
    sent = []
    out = notify.notify("x", "hi", loop_lookup=lambda n: {"notify": "oops"},
                        default_sender=lambda t, b: sent.append(t))
    assert out == [("slack-default", True, "sent")]


def test_main_exit_codes_and_output(capsys):
    assert notify.main(["l", "hello"], notify_fn=lambda n, t: [("a", True, "sent"), ("b", False, "unknown connector")]) == 0
    assert capsys.readouterr().out.splitlines() == ["a: ok: sent", "b: FAILED: unknown connector"]
    assert notify.main(["l", "hello"], notify_fn=lambda n, t: [("b", False, "x")]) == 1


def test_main_usage_error(capsys):
    assert notify.main(["only-one"], notify_fn=lambda n, t: []) == 1


def test_routes_blocks_to_a_real_webhook_connector():
    """notify passes blocks= to every connector's send(); the real
    WebhookConnector must accept (and ignore) it rather than TypeError."""
    import connectors
    connectors._load_all()
    posted = []

    def fake_http(method, url, json_body=None, timeout=10, **kw):
        posted.append((method, url, json_body))

    conn = connectors.get_type("webhook")(
        {"id": "feishu-team", "type": "webhook", "settings": {"format": "feishu"}},
        secret="https://hook.example/x", http=fake_http)
    out = notify.notify("x", "hi", blocks=[{"type": "section"}],
                        loop_lookup=lambda n: {"notify": ["feishu-team"]},
                        loader=lambda i: conn, default_sender=lambda t, b: None)
    assert out == [("feishu-team", True, "sent")]
    assert posted == [("POST", "https://hook.example/x", {"msg_type": "text", "content": {"text": "hi"}})]
