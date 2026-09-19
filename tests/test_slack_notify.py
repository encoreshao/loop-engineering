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


def test_post_message_includes_blocks_when_given(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req):
        captured["body"] = json.loads(req.data.decode())
        return FakeResponse()

    monkeypatch.setattr(slack_notify.urllib.request, "urlopen", fake_urlopen)

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "AI news briefing"}},
        {"type": "divider"},
    ]
    slack_notify.post_message(
        "AI news briefing", webhook_url="https://hooks.slack.com/services/FAKE", blocks=blocks,
    )

    assert captured["body"] == {"text": "AI news briefing", "blocks": blocks}


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

    def fake_post_message(text, bundle=None, blocks=None):
        captured["text"] = text
        captured["bundle"] = bundle
        captured["blocks"] = blocks
        return 200

    monkeypatch.setattr(slack_notify, "post_message", fake_post_message)
    monkeypatch.setattr(sys, "argv", ["slack_notify.py", "--bundle=vertex-limited", "hello"])

    slack_notify.main()

    assert captured == {"text": "hello", "bundle": "vertex-limited", "blocks": None}
    assert "200" in capsys.readouterr().out


def test_main_without_bundle_flag_passes_none(monkeypatch, capsys):
    captured = {}

    def fake_post_message(text, bundle=None, blocks=None):
        captured["text"] = text
        captured["bundle"] = bundle
        captured["blocks"] = blocks
        return 200

    monkeypatch.setattr(slack_notify, "post_message", fake_post_message)
    monkeypatch.setattr(sys, "argv", ["slack_notify.py", "hello"])

    slack_notify.main()

    assert captured == {"text": "hello", "bundle": None, "blocks": None}


def test_main_parses_blocks_flag(monkeypatch, capsys):
    captured = {}
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "AI news briefing"}}]

    def fake_post_message(text, bundle=None, blocks=None):
        captured["text"] = text
        captured["bundle"] = bundle
        captured["blocks"] = blocks
        return 200

    monkeypatch.setattr(slack_notify, "post_message", fake_post_message)
    monkeypatch.setattr(
        sys, "argv",
        ["slack_notify.py", "--blocks=" + json.dumps(blocks), "AI news briefing"],
    )

    slack_notify.main()

    assert captured == {"text": "AI news briefing", "bundle": None, "blocks": blocks}


def test_main_blocks_flag_combines_with_bundle_flag(monkeypatch):
    captured = {}
    blocks = [{"type": "divider"}]

    def fake_post_message(text, bundle=None, blocks=None):
        captured["bundle"] = bundle
        captured["blocks"] = blocks
        return 200

    monkeypatch.setattr(slack_notify, "post_message", fake_post_message)
    monkeypatch.setattr(
        sys, "argv",
        ["slack_notify.py", "--bundle=vertex-limited", "--blocks=" + json.dumps(blocks), "hello"],
    )

    slack_notify.main()

    assert captured == {"bundle": "vertex-limited", "blocks": blocks}


def test_main_blocks_flag_with_invalid_json_reports_error_without_posting(monkeypatch, capsys):
    def fake_post_message(text, bundle=None, blocks=None):
        raise AssertionError("invalid --blocks JSON must never be posted")

    monkeypatch.setattr(slack_notify, "post_message", fake_post_message)
    monkeypatch.setattr(sys, "argv", ["slack_notify.py", "--blocks=not-json", "hello"])

    exit_code = slack_notify.main()

    assert exit_code == 1
    assert "Invalid --blocks JSON" in capsys.readouterr().err


def test_main_help_flag_prints_usage_without_posting(monkeypatch, capsys):
    def fake_post_message(text, bundle=None):
        raise AssertionError("--help must never be posted as a live message")

    monkeypatch.setattr(slack_notify, "post_message", fake_post_message)
    monkeypatch.setattr(sys, "argv", ["slack_notify.py", "--help"])

    exit_code = slack_notify.main()

    assert exit_code == 0
    assert "Usage" in capsys.readouterr().out


def test_main_short_help_flag_prints_usage_without_posting(monkeypatch, capsys):
    def fake_post_message(text, bundle=None):
        raise AssertionError("-h must never be posted as a live message")

    monkeypatch.setattr(slack_notify, "post_message", fake_post_message)
    monkeypatch.setattr(sys, "argv", ["slack_notify.py", "-h"])

    exit_code = slack_notify.main()

    assert exit_code == 0
    assert "Usage" in capsys.readouterr().out
