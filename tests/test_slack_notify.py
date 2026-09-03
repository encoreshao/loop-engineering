import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import slack_notify


def test_load_webhook_url_reads_config(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"webhook_url": "https://hooks.slack.com/services/FAKE"}))

    assert slack_notify.load_webhook_url(config_path) == "https://hooks.slack.com/services/FAKE"


def test_post_message_sends_expected_payload(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        return FakeResponse()

    monkeypatch.setattr(slack_notify.urllib.request, "urlopen", fake_urlopen)

    status = slack_notify.post_message("hello", webhook_url="https://hooks.slack.com/services/FAKE")

    assert status == 200
    assert captured["url"] == "https://hooks.slack.com/services/FAKE"
    assert captured["body"] == {"text": "hello"}


def test_load_webhook_url_uses_bundle_override_when_present(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "webhook_url": "https://hooks.slack.com/services/DEFAULT",
        "bundle_webhooks": {"vertex-limited": "https://hooks.slack.com/services/VERTEX"},
    }))

    assert slack_notify.load_webhook_url(config_path, bundle="vertex-limited") == "https://hooks.slack.com/services/VERTEX"


def test_load_webhook_url_falls_back_to_default_without_bundle_override(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"webhook_url": "https://hooks.slack.com/services/DEFAULT"}))

    assert slack_notify.load_webhook_url(config_path, bundle="vertex-limited") == "https://hooks.slack.com/services/DEFAULT"


def test_load_webhook_url_falls_back_when_bundle_not_given(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"webhook_url": "https://hooks.slack.com/services/DEFAULT"}))

    assert slack_notify.load_webhook_url(config_path) == "https://hooks.slack.com/services/DEFAULT"


def test_main_parses_bundle_flag(monkeypatch, capsys):
    captured = {}

    def fake_post_message(text, bundle=None):
        captured["text"] = text
        captured["bundle"] = bundle
        return 200

    monkeypatch.setattr(slack_notify, "post_message", fake_post_message)
    monkeypatch.setattr(sys, "argv", ["slack_notify.py", "--bundle=vertex-limited", "hello"])

    slack_notify.main()

    assert captured == {"text": "hello", "bundle": "vertex-limited"}
    assert "200" in capsys.readouterr().out


def test_main_without_bundle_flag_passes_none(monkeypatch, capsys):
    captured = {}

    def fake_post_message(text, bundle=None):
        captured["text"] = text
        captured["bundle"] = bundle
        return 200

    monkeypatch.setattr(slack_notify, "post_message", fake_post_message)
    monkeypatch.setattr(sys, "argv", ["slack_notify.py", "hello"])

    slack_notify.main()

    assert captured == {"text": "hello", "bundle": None}
