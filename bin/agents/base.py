#!/usr/bin/env python3
"""Agent ABC + AgentResult - see
docs/superpowers/specs/2026-09-08-agent-adapter-design.md. `Agent.run()`
never raises: a subprocess timeout or non-zero exit becomes
AgentResult(status="timeout"/"failed", ...) instead of an exception - the
caller (loop_cli.py) decides whether/how to turn that into a raised
exception for LoopRuntime's own agent-failure handling. This module has
no dependency on claude.py/codex.py - get_agent() imports them lazily
inside the function body to avoid a circular import (they import Agent/
AgentResult/classify_subprocess_error from here)."""
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_cli_config

_VALID_PROVIDERS = ("claude", "codex")


@dataclass
class AgentResult:
    status: str  # "success" | "failed" | "timeout"
    output: str
    exit_code: int | None
    duration_ms: int
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: float | None


class Agent(ABC):
    @abstractmethod
    def run(self, prompt, context, *, cwd, timeout_seconds, allowed_tools=None,
            disallowed_tools=None, add_dirs=(), output_format="json"):
        ...


def classify_subprocess_error(exc, timeout_seconds):
    """(status, reason) for a caught subprocess.TimeoutExpired/
    CalledProcessError - same branch gitlab_loop_runner.py's
    _invoke_cli_with_prompt already has, shared here instead of copied a
    third time."""
    import subprocess
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timeout", f"timed out after {timeout_seconds}s"
    return "failed", f"exited {exc.returncode}"


def get_agent(provider=None):
    if provider is None or provider == "default":
        provider = ai_cli_config.get_selected_cli()
    if provider not in _VALID_PROVIDERS:
        raise ValueError(f"Unknown agent provider: {provider!r}")

    if provider == "claude":
        from agents.claude import ClaudeAgent
        return ClaudeAgent()
    from agents.codex import CodexAgent
    return CodexAgent()
