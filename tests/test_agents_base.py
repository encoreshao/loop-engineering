import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from agents.base import Agent, AgentResult, classify_subprocess_error, get_agent


def test_agent_result_holds_expected_fields():
    result = AgentResult(
        status="success", output="did the thing", exit_code=0, duration_ms=1234,
        input_tokens=100, output_tokens=50, estimated_cost_usd=0.42,
    )

    assert result.status == "success"
    assert result.output == "did the thing"
    assert result.exit_code == 0
    assert result.duration_ms == 1234
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.estimated_cost_usd == 0.42


def test_agent_is_abstract_and_requires_run():
    try:
        Agent()
        assert False, "expected TypeError instantiating an abstract class"
    except TypeError:
        pass


def test_classify_subprocess_error_reports_timeout():
    exc = subprocess.TimeoutExpired(cmd=["claude"], timeout=900, output=b"", stderr=b"stuck")
    status, reason = classify_subprocess_error(exc, timeout_seconds=900)

    assert status == "timeout"
    assert "900" in reason


def test_classify_subprocess_error_reports_nonzero_exit():
    exc = subprocess.CalledProcessError(returncode=1, cmd=["claude"], output=b"", stderr=b"boom")
    status, reason = classify_subprocess_error(exc, timeout_seconds=900)

    assert status == "failed"
    assert "1" in reason


def test_get_agent_resolves_via_ai_cli_config(monkeypatch):
    import agents.base as agents_base

    monkeypatch.setattr(agents_base.ai_cli_config, "get_selected_cli", lambda: "codex")
    agent = get_agent()

    assert type(agent).__name__ == "CodexAgent"


def test_get_agent_honors_explicit_provider():
    agent = get_agent(provider="claude")

    assert type(agent).__name__ == "ClaudeAgent"


def test_get_agent_default_sentinel_resolves_same_as_no_argument(monkeypatch):
    import agents.base as agents_base

    calls = []
    monkeypatch.setattr(agents_base.ai_cli_config, "get_selected_cli", lambda: calls.append(1) or "codex")

    agent_none = get_agent()
    agent_default = get_agent(provider="default")

    assert type(agent_none).__name__ == type(agent_default).__name__ == "CodexAgent"
    assert len(calls) == 2


def test_get_agent_rejects_unknown_provider():
    try:
        get_agent(provider="not-a-real-cli")
        assert False, "expected ValueError"
    except ValueError:
        pass
