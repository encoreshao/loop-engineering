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
def _no_real_ai_cli(sanitized_path):
    pass


def test_claude_command_disables_tools_and_mcp():
    cmd = runner._cli_command("claude")
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd
    assert json.loads(cmd[cmd.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "--no-session-persistence" in cmd
    assert json.loads(cmd[cmd.index("--settings") + 1]) == {"disableAllHooks": True}
    assert not any("Bash" in part or "WebFetch" in part for part in cmd)


def test_codex_command_is_read_only_without_mcp_and_reads_stdin():
    cmd = runner._cli_command("codex")
    assert cmd[:2] == ["codex", "exec"]
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "mcp_servers={}" in cmd
    assert "tools.web_search=false" in cmd
    assert cmd[-1] == "-"


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


def test_invoke_codex_returns_raw_stdout_and_no_cost(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "codex")
    monkeypatch.setattr(runner.subprocess, "run", _Run("[]"))
    log = tmp_path / "log.txt"
    out = runner.invoke_triage_agent("p", repo_root=tmp_path, unified_log_path=log)
    assert out == {"text": "[]", "cost_usd": None}
    assert "codex triage call ok" in log.read_text()


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
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "codex")
    exc = subprocess.CalledProcessError(1, ["codex"], output="LEAKED BODY", stderr="LEAKED BODY")
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
    decisions, cost = runner.classify("p", MSGS, CATS, invoke=lambda prompt: next(replies))
    assert decisions[0]["category"] == "fyi"
    assert cost == pytest.approx(0.03)


def test_classify_fails_after_two_bad_replies():
    with pytest.raises(runner.TriageFailed, match="no JSON array"):
        runner.classify("p", MSGS, CATS, invoke=lambda prompt: {"text": "nope", "cost_usd": None})


def test_classify_retries_on_subprocess_failure():
    calls = []

    def invoke(prompt):
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
    return lambda prompt: {"text": json.dumps(list(decisions)), "cost_usd": 0.01}


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
    outcome = _run(provider, lambda prompt: {"text": "not json", "cost_usd": None}, tmp_path)
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
