import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import inbox_config  # noqa: E402
import inbox_triage_runner as runner  # noqa: E402

CATS = inbox_config.DEFAULT_CATEGORIES


@pytest.fixture(autouse=True)
def _no_real_ai_cli(sanitized_path, monkeypatch):
    # Never read the machine's real ai_cli.json; a test that needs codex
    # selected overrides this.
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "claude")


def test_claude_command_disables_tools_and_mcp():
    cmd = runner._cli_command()
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd
    assert json.loads(cmd[cmd.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "--no-session-persistence" in cmd
    assert json.loads(cmd[cmd.index("--settings") + 1]) == {"disableAllHooks": True}
    assert not any("Bash" in part or "WebFetch" in part for part in cmd)


def test_invoke_refuses_codex_without_running_anything(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "codex")
    fake = _Run("[]")
    monkeypatch.setattr(runner.subprocess, "run", fake)
    with pytest.raises(runner.TriageFailed, match="requires the Claude CLI"):
        runner.invoke_triage_agent("p", repo_root=tmp_path, unified_log_path=tmp_path / "l")
    assert fake.calls == []


class _Run:
    def __init__(self, stdout="", returncode=0, exc=None):
        self.stdout, self.returncode, self.exc, self.calls = stdout, returncode, exc, []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


def test_invoke_claude_passes_prompt_on_stdin_and_parses_cost(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "claude")
    fake = _Run(json.dumps({"result": "[]", "total_cost_usd": 0.02, "is_error": False}))
    monkeypatch.setattr(runner.subprocess, "run", fake)
    log = tmp_path / "log.txt"
    out = runner.invoke_triage_agent("SECRET BODY TEXT", repo_root=tmp_path, unified_log_path=log)
    assert out == {"text": "[]", "cost_usd": 0.02}
    cmd, kwargs = fake.calls[0]
    assert kwargs["input"] == "SECRET BODY TEXT"
    assert "SECRET BODY TEXT" not in " ".join(cmd)
    assert "SECRET BODY TEXT" not in log.read_text()


def test_invoke_runs_in_a_fresh_temp_dir_not_the_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "claude")
    fake = _Run(json.dumps({"result": "[]", "total_cost_usd": None, "is_error": False}))
    monkeypatch.setattr(runner.subprocess, "run", fake)
    runner.invoke_triage_agent("p", repo_root=tmp_path, unified_log_path=tmp_path / "l")
    cwd = fake.calls[0][1]["cwd"]
    assert cwd != str(tmp_path)
    assert not Path(cwd).is_relative_to(tmp_path)
    assert not Path(cwd).exists()  # cleaned up once invoke_triage_agent returns


def test_invoke_claude_is_error_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "claude")
    monkeypatch.setattr(runner.subprocess, "run", _Run(json.dumps({"result": "rate limited", "is_error": True})))
    with pytest.raises(runner.TriageFailed):
        runner.invoke_triage_agent("p", repo_root=tmp_path, unified_log_path=tmp_path / "l")


def test_invoke_claude_unparseable_envelope_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "claude")
    monkeypatch.setattr(runner.subprocess, "run", _Run("not json at all"))
    log = tmp_path / "log.txt"
    with pytest.raises(runner.TriageFailed, match="unparseable envelope"):
        runner.invoke_triage_agent("p", repo_root=tmp_path, unified_log_path=log)
    assert "unparseable CLI envelope" in log.read_text()


def test_invoke_timeout_logs_no_prompt_text(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "claude")
    exc = subprocess.TimeoutExpired(["claude"], 900)
    monkeypatch.setattr(runner.subprocess, "run", _Run(exc=exc))
    log = tmp_path / "log.txt"
    with pytest.raises(subprocess.TimeoutExpired):
        runner.invoke_triage_agent("SECRET PROMPT TEXT", repo_root=tmp_path, timeout_seconds=900, unified_log_path=log)
    text = log.read_text()
    assert "timed out after 900s" in text
    assert "SECRET PROMPT TEXT" not in text


def test_invoke_failure_logs_no_output_content(monkeypatch, tmp_path):
    exc = subprocess.CalledProcessError(1, ["claude"], output="LEAKED BODY", stderr="LEAKED BODY")
    monkeypatch.setattr(runner.subprocess, "run", _Run(exc=exc))
    log = tmp_path / "log.txt"
    with pytest.raises(subprocess.CalledProcessError):
        runner.invoke_triage_agent("p", repo_root=tmp_path, unified_log_path=log)
    text = log.read_text()
    assert "exited 1" in text and "LEAKED BODY" not in text


MSGS = [{"id": "m1", "from": "a@x.com"}]
GOOD = json.dumps([{"id": "m1", "category": "fyi", "reason": "r", "draft_body": None}])


def test_classify_retries_once_on_bad_json_then_succeeds():
    replies = iter([{"text": "garbage", "cost_usd": 0.01}, {"text": GOOD, "cost_usd": 0.02}])
    decisions, cost = runner.classify("p", MSGS, CATS, invoke=lambda prompt, **kw: next(replies))
    assert decisions[0]["category"] == "fyi"
    assert cost == pytest.approx(0.03)


def test_classify_fails_after_two_bad_replies():
    with pytest.raises(runner.TriageFailed, match="no JSON array"):
        runner.classify("p", MSGS, CATS, invoke=lambda prompt, **kw: {"text": "nope", "cost_usd": None})


def test_classify_retries_on_subprocess_failure():
    calls = []

    def invoke(prompt, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(["claude"], 900)
        return {"text": GOOD, "cost_usd": None}
    decisions, cost = runner.classify("p", MSGS, CATS, invoke=invoke)
    assert len(calls) == 2 and cost is None


def test_instructions_doc_exists_and_states_json_contract():
    text = runner.INSTRUCTIONS_PATH.read_text()
    for needle in ('"id"', '"category"', '"reason"', '"draft_body"', "JSON array", "never"):
        assert needle in text


from datetime import datetime, timezone  # noqa: E402

import inbox_seen  # noqa: E402
import loop_policy  # noqa: E402
import mail_auth  # noqa: E402
import mail_http  # noqa: E402

NOW = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)
INBOX = {"name": "w", "label": "Work", "provider": "gmail", "account": "me@example.com", "enabled": True,
         "urgent_brief": "", "vip_senders": [], "exclude_senders": [], "categories": None, "slack_bundle": None}
CONFIG = {"default_categories": CATS, "inboxes": [INBOX]}


def _m(i, sender="Alice <alice@x.com>", minute=0):
    return {"id": f"m{i}", "thread_id": f"t{i}", "from": sender, "to": "me@example.com", "cc": "",
            "subject": f"Subject {i}", "date": f"2026-09-27T08:{minute:02d}:00+00:00",
            "body_text": f"PRIVATE BODY {i}", "prior_thread": [], "attachments": []}


class FakeProvider:
    def __init__(self, messages, address="me@example.com", fail_label_on=None, fail_draft_on=None,
                 label_exception=None, draft_exception=None, ensure_labels_exception=None):
        self.messages, self.address = messages, address
        self.fail_label_on, self.fail_draft_on = fail_label_on, fail_draft_on
        self.label_exception = label_exception or mail_http.MailHTTPError(500, "", "http://x")
        self.draft_exception = draft_exception or mail_http.MailHTTPError(500, "", "http://x")
        self.ensure_labels_exception = ensure_labels_exception
        self.labelled, self.drafts, self.fetch_args = [], [], None

    def profile_address(self):
        return self.address

    def fetch_new(self, since, seen_ids, exclude, limit):
        self.fetch_args = (since, set(seen_ids), list(exclude), limit)
        return [m for m in self.messages if m["id"] not in seen_ids][:limit]

    def ensure_labels(self, labels):
        if self.ensure_labels_exception:
            raise self.ensure_labels_exception
        return {name: f"id-{name}" for name in labels}

    def apply_label(self, message_id, label_id):
        if message_id == self.fail_label_on:
            raise self.label_exception
        self.labelled.append((message_id, label_id))

    def create_reply_draft(self, message, body):
        if message["id"] == self.fail_draft_on:
            raise self.draft_exception
        self.drafts.append((message["id"], body))
        return f"https://mail/{message['id']}"


def _reply(*decisions):
    return lambda prompt, **kw: {"text": json.dumps(list(decisions)), "cost_usd": 0.01}


def _run(provider, invoke, tmp_path, inbox=INBOX, token_fn=None):
    return runner.triage_inbox(
        inbox, CONFIG, NOW, provider_factory=lambda inbox, token, refresh: provider,
        token_fn=token_fn or (lambda inbox: "tok"), invoke=invoke, state_dir=tmp_path, instructions="INSTR")


def test_happy_path_labels_drafts_and_records_seen(tmp_path):
    provider = FakeProvider([_m(1, minute=1), _m(2, minute=2)])
    outcome = _run(provider, _reply(
        {"id": "m1", "category": "urgent", "reason": "deadline", "draft_body": "On it"},
        {"id": "m2", "category": "fyi", "reason": "update", "draft_body": None}), tmp_path)
    assert outcome["status"] == "ok"
    assert outcome["counts"] == {"urgent": 1, "fyi": 1}
    assert provider.labelled == [("m1", "id-Loop/Urgent"), ("m2", "id-Loop/FYI")]
    assert provider.drafts == [("m1", "On it")]
    assert outcome["urgent"] == [{"from": "Alice <alice@x.com>", "subject": "Subject 1", "draft_link": "https://mail/m1",
                                  "needs_manual_reply": False, "draft_failed": False}]
    state = inbox_seen.load("w", state_dir=tmp_path)
    assert set(state["seen"]) == {"m1", "m2"} and state["high_water"] == "2026-09-27T08:02:00+00:00"


def test_codex_selected_fails_every_inbox_before_reading_any_mail(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "codex")
    provider = FakeProvider([_m(1)])
    tokens = []

    def boom(prompt):
        raise AssertionError("no AI call under codex")
    outcome = _run(provider, boom, tmp_path, token_fn=lambda inbox: tokens.append(inbox) or "tok")
    assert outcome["status"] == "failed"
    assert outcome["error"] == ("Inbox Triage requires the Claude CLI (codex gives the model a shell) - "
                                "switch AI CLI to Claude in Settings")
    assert tokens == [] and provider.fetch_args is None and provider.labelled == []
    assert inbox_seen.load("w", state_dir=tmp_path)["seen"] == {}


def test_quiet_run_makes_no_ai_call(tmp_path):
    def boom(prompt):
        raise AssertionError("AI must not be called with no messages")
    outcome = _run(FakeProvider([]), boom, tmp_path)
    assert outcome["status"] == "quiet" and outcome["counts"] == {}


def test_cap_and_overflow(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "MESSAGE_CAP", 2)
    provider = FakeProvider([_m(1, minute=1), _m(2, minute=2), _m(3, minute=3)])
    outcome = _run(provider, _reply(
        {"id": "m1", "category": "fyi", "reason": "r", "draft_body": None},
        {"id": "m2", "category": "fyi", "reason": "r", "draft_body": None}), tmp_path)
    assert provider.fetch_args[3] == 3
    assert outcome["overflow"] is True and len(outcome["rows"]) == 2


def test_account_mismatch_fails_before_fetch(tmp_path):
    provider = FakeProvider([_m(1)], address="someone@else.com")
    outcome = _run(provider, _reply(), tmp_path)
    assert outcome["status"] == "failed" and "someone@else.com" in outcome["error"]
    assert provider.fetch_args is None


def test_reauth_required_status(tmp_path):
    def token_fn(inbox):
        raise mail_auth.ReauthRequired("w is not connected")
    outcome = _run(FakeProvider([_m(1)]), _reply(), tmp_path, token_fn=token_fn)
    assert outcome["status"] == "needs_reauth" and "not connected" in outcome["error"]


def test_auth_expired_during_fetch_is_needs_reauth(tmp_path):
    class Expiring(FakeProvider):
        def fetch_new(self, *a):
            raise mail_http.AuthExpired(401, "", "http://x")
    outcome = _run(Expiring([]), _reply(), tmp_path)
    assert outcome["status"] == "needs_reauth"


def test_bad_ai_output_applies_nothing_and_records_nothing(tmp_path):
    provider = FakeProvider([_m(1)])
    outcome = _run(provider, lambda prompt, **kw: {"text": "not json", "cost_usd": None}, tmp_path)
    assert outcome["status"] == "failed"
    assert provider.labelled == [] and provider.drafts == []
    assert inbox_seen.load("w", state_dir=tmp_path)["seen"] == {}


def test_partial_label_failure_records_only_labelled(tmp_path):
    provider = FakeProvider([_m(1, minute=1), _m(2, minute=2)], fail_label_on="m2")
    outcome = _run(provider, _reply(
        {"id": "m1", "category": "fyi", "reason": "r", "draft_body": None},
        {"id": "m2", "category": "fyi", "reason": "r", "draft_body": None}), tmp_path)
    assert outcome["status"] == "failed"
    assert set(inbox_seen.load("w", state_dir=tmp_path)["seen"]) == {"m1"}


def test_draft_failure_keeps_label_and_is_reported(tmp_path):
    provider = FakeProvider([_m(1)], fail_draft_on="m1")
    outcome = _run(provider, _reply({"id": "m1", "category": "urgent", "reason": "r", "draft_body": "x"}), tmp_path)
    assert outcome["status"] == "ok"
    assert provider.labelled == [("m1", "id-Loop/Urgent")]
    assert outcome["urgent"][0]["draft_failed"] is True and outcome["urgent"][0]["draft_link"] is None
    assert outcome["urgent"][0]["needs_manual_reply"] is False


def test_keychain_error_during_labelling_records_labelled_and_fails(tmp_path):
    provider = FakeProvider([_m(1, minute=1), _m(2, minute=2)], fail_label_on="m2",
                            label_exception=mail_auth.KeychainError("Keychain is locked"))
    outcome = _run(provider, _reply(
        {"id": "m1", "category": "fyi", "reason": "r", "draft_body": None},
        {"id": "m2", "category": "fyi", "reason": "r", "draft_body": None}), tmp_path)
    assert outcome["status"] == "failed"
    assert provider.labelled == [("m1", "id-Loop/FYI")]
    assert set(inbox_seen.load("w", state_dir=tmp_path)["seen"]) == {"m1"}


def test_keychain_error_during_draft_is_reported_as_draft_failed(tmp_path):
    provider = FakeProvider([_m(1)], fail_draft_on="m1",
                            draft_exception=mail_auth.KeychainError("Keychain is locked"))
    outcome = _run(provider, _reply({"id": "m1", "category": "urgent", "reason": "r", "draft_body": "x"}), tmp_path)
    assert outcome["status"] == "ok"
    assert provider.labelled == [("m1", "id-Loop/Urgent")]
    assert outcome["urgent"][0]["draft_failed"] is True and outcome["urgent"][0]["draft_link"] is None


def test_unexpected_draft_exception_is_contained_and_remaining_drafts_still_run(tmp_path):
    """A ValueError from EmailMessage (a header with a linefeed) or a
    UnicodeEncodeError (a lone surrogate in draft_body) must not escape the
    draft loop: labels and seen IDs are already recorded by then, so an
    escape would discard the urgent list and never retry the skipped drafts."""
    provider = FakeProvider([_m(1, minute=1), _m(2, minute=2)], fail_draft_on="m1",
                            draft_exception=ValueError("Header values may not contain linefeed PRIVATE SUBJECT"))
    outcome = _run(provider, _reply(
        {"id": "m1", "category": "urgent", "reason": "r", "draft_body": "x"},
        {"id": "m2", "category": "urgent", "reason": "r", "draft_body": "y"}), tmp_path)
    assert outcome["status"] == "ok"
    assert provider.drafts == [("m2", "y")]
    assert [u["draft_failed"] for u in outcome["urgent"]] == [True, False]
    assert outcome["urgent"][1]["draft_link"] == "https://mail/m2"
    assert "PRIVATE SUBJECT" not in json.dumps(outcome)


def test_unexpected_draft_exception_logs_only_the_class_name(tmp_path, capsys):
    provider = FakeProvider([_m(1)], fail_draft_on="m1", draft_exception=UnicodeEncodeError(
        "utf-8", "PRIVATE \ud800", 8, 9, "surrogates not allowed"))
    outcome = _run(provider, _reply({"id": "m1", "category": "urgent", "reason": "r", "draft_body": "x"}), tmp_path)
    assert outcome["status"] == "ok" and outcome["urgent"][0]["draft_failed"] is True
    err = capsys.readouterr().err
    assert "UnicodeEncodeError" in err and "PRIVATE" not in err


def test_auth_expired_during_labelling_is_needs_reauth_and_records_labelled(tmp_path):
    provider = FakeProvider([_m(1, minute=1), _m(2, minute=2)], fail_label_on="m2",
                            label_exception=mail_http.AuthExpired(401, "", "http://x"))
    outcome = _run(provider, _reply(
        {"id": "m1", "category": "fyi", "reason": "r", "draft_body": None},
        {"id": "m2", "category": "fyi", "reason": "r", "draft_body": None}), tmp_path)
    assert outcome["status"] == "needs_reauth"
    assert provider.labelled == [("m1", "id-Loop/FYI")]
    assert set(inbox_seen.load("w", state_dir=tmp_path)["seen"]) == {"m1"}


def test_ensure_labels_failure_applies_and_records_nothing(tmp_path):
    provider = FakeProvider([_m(1)], ensure_labels_exception=mail_http.MailHTTPError(500, "", "http://x"))
    outcome = _run(provider, _reply({"id": "m1", "category": "fyi", "reason": "r", "draft_body": None}), tmp_path)
    assert outcome["status"] == "failed"
    assert provider.labelled == [] and provider.drafts == []
    assert inbox_seen.load("w", state_dir=tmp_path)["seen"] == {}


def test_vip_sender_forced_urgent_needs_manual_reply(tmp_path):
    vip_inbox = dict(INBOX, vip_senders=["alice@x.com"])
    provider = FakeProvider([_m(1)])
    outcome = _run(provider, _reply({"id": "m1", "category": "fyi", "reason": "update", "draft_body": None}),
                  tmp_path, inbox=vip_inbox)
    assert outcome["status"] == "ok"
    assert outcome["counts"] == {"urgent": 1}
    assert provider.labelled == [("m1", "id-Loop/Urgent")]
    assert outcome["urgent"][0]["needs_manual_reply"] is True


def test_seen_ids_and_since_passed_to_provider(tmp_path):
    inbox_seen.record("w", [{"id": "old", "date": "2026-09-27T07:00:00+00:00"}], NOW, state_dir=tmp_path)
    provider = FakeProvider([])
    _run(provider, _reply(), tmp_path)
    since, seen, exclude, _ = provider.fetch_args
    assert since == datetime(2026, 9, 27, 7, 0, tzinfo=timezone.utc) and seen == {"old"}


def test_outcome_rows_never_contain_body_text(tmp_path):
    provider = FakeProvider([_m(1)])
    outcome = _run(provider, _reply({"id": "m1", "category": "fyi", "reason": "r", "draft_body": None}), tmp_path)
    assert "PRIVATE BODY" not in json.dumps(outcome)


def _outcome_ok():
    return {"name": "w", "label": "Work", "status": "ok", "counts": {"urgent": 1, "action": 2, "fyi": 3},
            "urgent": [{"from": "Alice <a@x.com>", "subject": "Prod | down", "draft_link": "https://mail/m1",
                        "needs_manual_reply": False, "draft_failed": False}],
            "rows": [{"date": "2026-09-27T08:00:00+00:00", "from": "Alice <a@x.com>", "subject": "Prod | down",
                      "category": "urgent", "reason": "outage", "draft_link": "https://mail/m1"}],
            "overflow": False, "error": None, "cost_usd": 0.01}


def test_write_history_appends_sections_and_escapes_pipes(tmp_path):
    path = runner.write_history(_outcome_ok(), NOW, history_dir=tmp_path)
    runner.write_history(_outcome_ok(), NOW.replace(hour=13), history_dir=tmp_path)
    text = path.read_text()
    assert path.name == "2026-09-27-w.md"
    assert text.count("## Run ") == 2
    assert "Prod \\| down" in text and "[draft](https://mail/m1)" in text


def test_format_digest_covers_every_status():
    outcomes = [
        _outcome_ok(),
        {**_outcome_ok(), "name": "q", "label": "Quiet", "status": "quiet", "counts": {}, "urgent": []},
        {**_outcome_ok(), "name": "r", "label": "Home", "status": "needs_reauth", "error": "expired", "urgent": []},
        {**_outcome_ok(), "name": "f", "label": "Side", "status": "failed", "error": "AI triage failed: x", "urgent": [],
         "overflow": True},
    ]
    text = runner.format_digest(outcomes, NOW)
    assert "*Work*: 1 urgent (drafts ready), 2 action, 3 other" in text
    assert "Prod | down" in text
    assert "*Quiet*: no new mail" in text
    assert "*Home*: needs re-auth" in text
    assert "*Side*: failed - AI triage failed: x" in text


def test_format_digest_manual_reply_and_overflow():
    outcome = _outcome_ok()
    outcome["urgent"][0].update({"draft_link": None, "needs_manual_reply": True})
    outcome["overflow"] = True
    text = runner.format_digest([outcome], NOW)
    assert "reply manually" in text and "more waiting" in text


def test_send_digests_groups_by_bundle(monkeypatch):
    posts = []
    monkeypatch.setattr(runner.slack_notify, "resolve_blocks", lambda notification_key, message: None)
    a = {**_outcome_ok(), "slack_bundle": None}
    b = {**_outcome_ok(), "name": "b", "label": "B", "slack_bundle": "team"}
    runner.send_digests([a, b], NOW, post=lambda text, bundle=None, blocks=None: posts.append((bundle, text)))
    assert [p[0] for p in posts] == [None, "team"]
    assert "*Work*" in posts[0][1] and "*B*" in posts[1][1]


def test_send_digests_slack_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(runner.slack_notify, "resolve_blocks", lambda notification_key, message: None)

    def fail(text, bundle=None, blocks=None):
        raise OSError("no network")
    runner.send_digests([_outcome_ok()], NOW, post=fail)


def test_run_all_inboxes_isolates_failures_and_writes_status(tmp_path, monkeypatch):
    config_path = tmp_path / "inboxes.json"
    config_path.write_text(json.dumps({"default_categories": CATS, "inboxes": [
        INBOX, {**INBOX, "name": "h", "label": "Home"}, {**INBOX, "name": "off", "enabled": False}]}))
    seen = []

    def triage(inbox, config, now):
        seen.append(inbox["name"])
        if inbox["name"] == "w":
            raise RuntimeError("unexpected crash")
        return {**_outcome_ok(), "name": inbox["name"], "label": inbox["label"]}
    digests = []
    monkeypatch.setattr(runner, "send_digests", lambda outcomes, now, post=None: digests.append(outcomes))
    status_path = tmp_path / "status.json"
    outcomes = runner.run_all_inboxes("run_1", now=NOW, config_path=config_path, results_dir=tmp_path / "results",
                                      events_dir=tmp_path / "events", status_path=status_path,
                                      history_dir=tmp_path / "history", triage=triage,
                                      lock_path=tmp_path / "run.lock")
    assert seen == ["w", "h"]
    assert [o["status"] for o in outcomes] == ["failed", "ok"]
    assert "unexpected crash" in outcomes[0]["error"]
    status = json.loads(status_path.read_text())["inboxes"]
    assert status["w"]["state"] == "failed" and status["h"]["state"] == "idle"
    assert status["h"]["counts"] == {"urgent": 1, "action": 2, "fyi": 3}
    assert "PRIVATE BODY" not in status_path.read_text()
    assert len(digests) == 1 and len(digests[0]) == 2
    assert (tmp_path / "history" / "2026-09-27-h.md").exists()


def test_run_all_inboxes_marks_needs_reauth(tmp_path, monkeypatch):
    config_path = tmp_path / "inboxes.json"
    config_path.write_text(json.dumps({"default_categories": CATS, "inboxes": [INBOX]}))
    monkeypatch.setattr(runner, "send_digests", lambda outcomes, now, post=None: None)
    runner.run_all_inboxes("run_1", now=NOW, config_path=config_path, results_dir=tmp_path / "r", events_dir=tmp_path / "e",
                           status_path=tmp_path / "s.json", history_dir=tmp_path / "h", lock_path=tmp_path / "run.lock",
                           triage=lambda inbox, config, now: {**_outcome_ok(), "status": "needs_reauth", "error": "x"})
    assert json.loads((tmp_path / "s.json").read_text())["inboxes"]["w"]["state"] == "needs_reauth"


def test_main_with_argv_usage_and_exit_zero(monkeypatch):
    assert runner.main_with_argv([]) == 2
    monkeypatch.setattr(runner, "run_all_inboxes", lambda run_id, **kw: [{"status": "failed"}])
    assert runner.main_with_argv(["run_1"]) == 0


def _two_inbox_config(tmp_path):
    config_path = tmp_path / "inboxes.json"
    config_path.write_text(json.dumps({"default_categories": CATS, "inboxes": [
        INBOX, {**INBOX, "name": "h", "label": "Home"}]}))
    return config_path


def _run_all(tmp_path, **kwargs):
    kwargs.setdefault("triage", lambda inbox, config, now: {**_outcome_ok(), "name": inbox["name"], "label": inbox["label"]})
    return runner.run_all_inboxes("run_1", now=NOW, config_path=_two_inbox_config(tmp_path),
                                  results_dir=tmp_path / "results", events_dir=tmp_path / "events",
                                  status_path=tmp_path / "status.json", history_dir=tmp_path / "history",
                                  lock_path=tmp_path / "run.lock", **kwargs)


def _states(tmp_path):
    return {k: v["state"] for k, v in json.loads((tmp_path / "status.json").read_text())["inboxes"].items()}


def test_write_history_failure_still_writes_terminal_status_and_continues(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "send_digests", lambda outcomes, now, post=None: None)

    def broken_history(outcome, now, history_dir=None):
        if outcome["name"] == "w":
            raise OSError("disk full")
        return None
    monkeypatch.setattr(runner, "write_history", broken_history)
    outcomes = _run_all(tmp_path)
    assert _states(tmp_path) == {"w": "idle", "h": "idle"}
    assert [o["name"] for o in outcomes] == ["w", "h"]
    assert outcomes[0]["urgent"]  # the triage result survives a history-write failure


def test_runtime_start_failure_marks_inbox_failed_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "send_digests", lambda outcomes, now, post=None: None)

    class PolicyBlocked:
        def __init__(self, **kwargs):
            pass

        def start(self, definition, run_id):
            raise loop_policy.PolicyViolationError([])
    monkeypatch.setattr(runner, "LoopRuntime", PolicyBlocked)
    outcomes = _run_all(tmp_path)
    assert _states(tmp_path) == {"w": "failed", "h": "failed"}
    assert all(o["status"] == "failed" for o in outcomes)
    assert "PolicyViolationError" in outcomes[0]["error"]


def test_status_write_failure_is_contained_per_inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "send_digests", lambda outcomes, now, post=None: None)
    real_write = runner.inbox_status.write

    def flaky_write(name, state, status_path=None, **extra):
        if name == "w" and state == "idle":
            raise OSError("disk full")
        return real_write(name, state, status_path=status_path, **extra)
    monkeypatch.setattr(runner.inbox_status, "write", flaky_write)
    outcomes = _run_all(tmp_path)
    assert _states(tmp_path) == {"w": "failed", "h": "idle"}
    assert len(outcomes) == 2


def test_interrupt_mid_inbox_writes_failed_then_propagates(tmp_path, monkeypatch):
    """SIGTERM from run-loop-now.sh's `timeout` is turned into SystemExit by
    main()'s handler; the inbox being triaged must not latch at running."""
    monkeypatch.setattr(runner, "send_digests", lambda outcomes, now, post=None: None)

    def killed(inbox, config, now):
        raise SystemExit(143)
    with pytest.raises(SystemExit):
        _run_all(tmp_path, triage=killed)
    states = json.loads((tmp_path / "status.json").read_text())["inboxes"]
    assert states["w"]["state"] == "failed" and states["w"]["error"]
    assert "h" not in states


def test_sigterm_handler_raises_system_exit():
    with pytest.raises(SystemExit) as info:
        runner._raise_on_sigterm(15, None)
    assert info.value.code == 143


def test_main_installs_sigterm_handler(monkeypatch):
    installed = {}
    monkeypatch.setattr(runner.signal, "signal", lambda sig, handler: installed.update({sig: handler}))
    monkeypatch.setattr(runner, "main_with_argv", lambda argv: 0)
    runner.main()
    assert installed[runner.signal.SIGTERM] is runner._raise_on_sigterm


def _definition_with_runtime(tmp_path, minutes):
    text = runner.DEFAULT_DEFINITION_PATH.read_text().replace("max_runtime_minutes: 15", f"max_runtime_minutes: {minutes}")
    path = tmp_path / "loop.yaml"
    path.write_text(text)
    return path


def test_run_all_inboxes_derives_ai_timeout_from_loop_definition(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "send_digests", lambda outcomes, now, post=None: None)
    seen = []

    def fake_triage_inbox(inbox, config, now, timeout_seconds=None, **kwargs):
        seen.append(timeout_seconds)
        return {**_outcome_ok(), "name": inbox["name"], "label": inbox["label"]}
    monkeypatch.setattr(runner, "triage_inbox", fake_triage_inbox)
    _run_all(tmp_path, triage=None, definition_path=_definition_with_runtime(tmp_path, 7))
    assert seen == [420, 420]


def test_triage_inbox_threads_timeout_to_the_ai_call(tmp_path, monkeypatch):
    calls = []

    def fake_invoke(prompt, timeout_seconds=None, **kwargs):
        calls.append(timeout_seconds)
        return {"text": json.dumps([{"id": "m1", "category": "fyi", "reason": "r", "draft_body": None}]), "cost_usd": None}
    monkeypatch.setattr(runner, "invoke_triage_agent", fake_invoke)
    provider = FakeProvider([_m(1)])
    outcome = runner.triage_inbox(INBOX, CONFIG, NOW, provider_factory=lambda inbox, token, refresh: provider,
                                  token_fn=lambda inbox: "tok", state_dir=tmp_path, instructions="I",
                                  timeout_seconds=321)
    assert outcome["status"] == "ok" and calls == [321]


def test_invoke_default_timeout_comes_from_the_loop_definition(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "claude")
    monkeypatch.setattr(runner, "DEFAULT_DEFINITION_PATH", _definition_with_runtime(tmp_path, 3))
    fake = _Run(json.dumps({"result": "[]", "total_cost_usd": None, "is_error": False}))
    monkeypatch.setattr(runner.subprocess, "run", fake)
    runner.invoke_triage_agent("p", repo_root=tmp_path, unified_log_path=tmp_path / "l")
    assert fake.calls[0][1]["timeout"] == 180


def test_second_run_while_lock_is_held_does_no_triage(tmp_path, monkeypatch, capsys):
    import fcntl
    digests = []
    monkeypatch.setattr(runner, "send_digests", lambda outcomes, now, post=None: digests.append(outcomes))
    lock_path = tmp_path / "run.lock"
    with open(lock_path, "w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def triage(inbox, config, now):
            raise AssertionError("must not triage while another run holds the lock")
        assert _run_all(tmp_path, triage=triage) == []
    assert digests == [] and not (tmp_path / "status.json").exists()
    assert "already running" in capsys.readouterr().err
    # released: the next run proceeds
    assert len(_run_all(tmp_path)) == 2


def test_lock_is_released_after_a_run(tmp_path, monkeypatch):
    import fcntl
    monkeypatch.setattr(runner, "send_digests", lambda outcomes, now, post=None: None)
    _run_all(tmp_path)
    with open(tmp_path / "run.lock", "w") as probe:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)  # would raise BlockingIOError if still held


def test_default_lock_path_is_resolved_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "send_digests", lambda outcomes, now, post=None: None)
    monkeypatch.setattr(runner, "DEFAULT_LOCK_PATH", tmp_path / "moved" / "run.lock")
    runner.run_all_inboxes("run_1", now=NOW, config_path=_two_inbox_config(tmp_path), results_dir=tmp_path / "r",
                           events_dir=tmp_path / "e", status_path=tmp_path / "s.json", history_dir=tmp_path / "h",
                           triage=lambda inbox, config, now: {**_outcome_ok(), "name": inbox["name"]})
    assert (tmp_path / "moved" / "run.lock").exists()


_HOSTILE_SUBJECT = "<!channel> <https://evil.example|Open draft> & ![p](https://tracker/p.gif) [Open draft](https://evil)"


def test_format_digest_escapes_untrusted_text_for_slack():
    outcome = _outcome_ok()
    outcome["label"] = "Work <!here>"
    outcome["urgent"][0].update({"from": "Mallory <m@x.com> <!everyone>", "subject": _HOSTILE_SUBJECT})
    failed = {**_outcome_ok(), "name": "f", "label": "Side", "status": "failed", "urgent": [],
              "error": "AI triage failed: unknown id '<!channel>'"}
    text = runner.format_digest([outcome, failed], NOW)
    assert "<!channel>" not in text and "<!here>" not in text and "<!everyone>" not in text
    assert "<https://evil.example|" not in text
    assert "&lt;!channel&gt; &lt;https://evil.example|Open draft&gt; &amp;" in text
    assert "Mallory &lt;m@x.com&gt;" in text
    assert "*Work &lt;!here&gt;*" in text
    assert "<https://mail/m1|draft>" in text  # our own draft link stays a real Slack link


def test_slack_escape_helper():
    assert runner._slack_escape("a & <b> c") == "a &amp; &lt;b&gt; c"
    assert runner._slack_escape(None) == ""


def test_markdown_escape_helper_neutralises_markup():
    escaped = runner._md("*b* _i_ `c` [t](u) ![a](u) | # https://x \\ <y>\nnext")
    for raw in ("*b*", "_i_", "`c`", "[t](u)", "![a](u)", " | ", "https://", "\n"):
        assert raw not in escaped
    assert runner._md("- item").startswith("\\-") and runner._md("12. item").startswith("12\\.")
    assert "\x00" not in runner._md("a\x00b")


def test_send_digests_retries_as_plain_text_when_blocks_are_rejected(monkeypatch):
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "x"}}]
    monkeypatch.setattr(runner.slack_notify, "resolve_blocks", lambda notification_key, message: blocks)
    posts = []

    def post(text, bundle=None, blocks=None):
        posts.append(blocks)
        if blocks:
            raise OSError("invalid_blocks")
    runner.send_digests([_outcome_ok()], NOW, post=post)
    assert posts == [blocks, None]


def test_send_digests_does_not_retry_a_plain_text_failure(monkeypatch):
    monkeypatch.setattr(runner.slack_notify, "resolve_blocks", lambda notification_key, message: None)
    posts = []

    def post(text, bundle=None, blocks=None):
        posts.append(blocks)
        raise OSError("no network")
    runner.send_digests([_outcome_ok()], NOW, post=post)
    assert posts == [None]


def test_send_digests_retry_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(runner.slack_notify, "resolve_blocks", lambda notification_key, message: [{"type": "divider"}])

    def post(text, bundle=None, blocks=None):
        raise OSError("down")
    runner.send_digests([_outcome_ok()], NOW, post=post)


def test_invoke_passes_a_budget_cap_to_claude(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "claude")
    fake = _Run(json.dumps({"result": "[]", "total_cost_usd": 0.02, "is_error": False}))
    monkeypatch.setattr(runner.subprocess, "run", fake)
    runner.invoke_triage_agent("p", repo_root=tmp_path, unified_log_path=tmp_path / "l", max_budget_usd=1.5)
    cmd, _ = fake.calls[0]
    assert cmd[cmd.index("--max-budget-usd") + 1] == "1.50"


def test_classify_caps_each_attempt_at_the_remaining_budget():
    caps = []
    replies = iter([{"text": "garbage", "cost_usd": 0.5}, {"text": GOOD, "cost_usd": 0.1}])

    def invoke(prompt, max_budget_usd=None):
        caps.append(max_budget_usd)
        return next(replies)
    runner.classify("p", MSGS, CATS, invoke=invoke, max_cost_usd=2.0)
    assert caps == [2.0, pytest.approx(1.5)]


def test_classify_default_budget_comes_from_the_loop_definition():
    caps = []

    def invoke(prompt, max_budget_usd=None):
        caps.append(max_budget_usd)
        return {"text": GOOD, "cost_usd": None}
    runner.classify("p", MSGS, CATS, invoke=invoke)
    assert caps == [2]
