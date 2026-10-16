from datetime import datetime, timedelta

import auth_health
import mail_auth

NOW = datetime(2026, 10, 16, 12, 0)


def _probe(pid, result):
    def run():
        if isinstance(result, Exception):
            raise result
        return result
    return {"id": pid, "label": pid, "run": run}


def _run(tmp_path, probes, sent, now=NOW):
    return auth_health.run_checks(
        probes=probes, state_path=tmp_path / "auth.json", now=now, send=sent.append)


def _checks(tmp_path):
    return auth_health.read_state(tmp_path / "auth.json")["checks"]


def test_healthy_probe_sends_nothing_and_is_recorded(tmp_path):
    sent = []
    _run(tmp_path, [_probe("gmail", (True, "ok"))], sent)
    assert sent == []
    assert _checks(tmp_path)["gmail"]["ok"] is True


def test_reauth_required_alerts_immediately_once(tmp_path):
    sent = []
    probes = [_probe("gmail", mail_auth.ReauthRequired("Google refresh failed (invalid_grant)"))]
    _run(tmp_path, probes, sent)
    _run(tmp_path, probes, sent, now=NOW + timedelta(hours=6))
    assert len(sent) == 1
    assert "gmail" in sent[0] and "invalid_grant" in sent[0]


def test_failing_alert_repeats_after_a_day(tmp_path):
    sent = []
    probes = [_probe("gmail", mail_auth.ReauthRequired("expired"))]
    _run(tmp_path, probes, sent)
    _run(tmp_path, probes, sent, now=NOW + timedelta(hours=25))
    assert len(sent) == 2


def test_soft_failure_needs_two_consecutive_checks_to_alert(tmp_path):
    sent = []
    probes = [_probe("gitlab", (False, "HTTP 401"))]
    _run(tmp_path, probes, sent)
    assert sent == []
    _run(tmp_path, probes, sent, now=NOW + timedelta(hours=6))
    assert len(sent) == 1


def test_recovery_sends_one_message_and_clears_failure(tmp_path):
    sent = []
    _run(tmp_path, [_probe("gmail", mail_auth.ReauthRequired("expired"))], sent)
    _run(tmp_path, [_probe("gmail", (True, "ok"))], sent, now=NOW + timedelta(hours=6))
    assert len(sent) == 2 and "recovered" in sent[1].lower()
    _run(tmp_path, [_probe("gmail", (True, "ok"))], sent, now=NOW + timedelta(hours=12))
    assert len(sent) == 2


def test_run_if_due_is_throttled(tmp_path):
    sent, calls = [], []
    probes = [{"id": "g", "label": "g", "run": lambda: calls.append(1) or (True, "ok")}]
    kw = dict(probes=probes, state_path=tmp_path / "auth.json", send=sent.append)
    assert auth_health.run_if_due(now=NOW, **kw) is True
    assert auth_health.run_if_due(now=NOW + timedelta(hours=1), **kw) is False
    assert auth_health.run_if_due(now=NOW + timedelta(hours=7), **kw) is True
    assert len(calls) == 2


def test_probe_crash_is_a_soft_failure_not_an_exception(tmp_path):
    sent = []
    _run(tmp_path, [_probe("x", RuntimeError("boom"))], sent)
    assert _checks(tmp_path)["x"]["ok"] is False


def test_failed_send_does_not_lose_the_check(tmp_path):
    def boom(text):
        raise OSError("no network")
    auth_health.run_checks(
        probes=[_probe("gmail", mail_auth.ReauthRequired("expired"))],
        state_path=tmp_path / "auth.json", now=NOW, send=boom)
    assert _checks(tmp_path)["gmail"]["ok"] is False


def test_collect_probes_covers_mailboxes_and_auth_connectors():
    inboxes = [{"name": "work", "provider": "gmail"}]
    accounts = [
        {"id": "cal", "type": "google_calendar", "enabled": True},
        {"id": "gl", "type": "gitlab", "enabled": True},
        {"id": "off", "type": "gitlab", "enabled": False},
        {"id": "hook", "type": "slack", "enabled": True},
        {"id": "box", "type": "mailbox", "enabled": True},
    ]
    probes = auth_health.collect_probes(
        inboxes_fn=lambda: inboxes, accounts_fn=lambda: accounts,
        token_fn=lambda inbox: "tok", connector_fn=lambda aid: None)
    assert [p["id"] for p in probes] == ["mailbox:work", "connector:cal", "connector:gl"]
