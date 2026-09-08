#!/usr/bin/env python3
"""ClaudeAgent - see docs/superpowers/specs/2026-09-08-agent-adapter-design.md."""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cost as cost_module
from agents.base import Agent, AgentResult, failure_result


class ClaudeAgent(Agent):
    def run(self, prompt, context, *, cwd, timeout_seconds, allowed_tools=None,
            disallowed_tools=None, add_dirs=(), output_format="json"):
        cmd = ["claude", "-p"]
        for add_dir in add_dirs:
            cmd += ["--add-dir", str(add_dir)]
        cmd += ["--permission-mode", "acceptEdits"]
        if allowed_tools is not None:
            cmd += ["--allowedTools", allowed_tools]
        if disallowed_tools is not None:
            cmd += ["--disallowedTools", disallowed_tools]
        cmd += ["--output-format", output_format, prompt]

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout_seconds, check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            return failure_result(exc, timeout_seconds, int((time.monotonic() - start) * 1000))
        duration_ms = int((time.monotonic() - start) * 1000)

        if output_format == "json":
            try:
                parsed = json.loads(proc.stdout) if proc.stdout else None
            except json.JSONDecodeError:
                return AgentResult(
                    status="failed", output=proc.stdout[-800:], exit_code=0, duration_ms=duration_ms,
                    input_tokens=None, output_tokens=None, estimated_cost_usd=None,
                )
            if parsed is not None and not isinstance(parsed, dict):
                return AgentResult(
                    status="failed", output=proc.stdout[-800:], exit_code=0, duration_ms=duration_ms,
                    input_tokens=None, output_tokens=None, estimated_cost_usd=None,
                )
            output = cost_module.extract_result_text(parsed) if parsed else "(no result text in CLI output)"
            usage = cost_module.extract_claude_usage(parsed) if parsed else None
            input_tokens = usage["input_tokens"] if usage else None
            output_tokens = usage["output_tokens"] if usage else None
            cost_usd = usage["cost_usd"] if usage else None
        else:
            output = proc.stdout
            input_tokens = output_tokens = cost_usd = None

        return AgentResult(
            status="success", output=output, exit_code=0, duration_ms=duration_ms,
            input_tokens=input_tokens, output_tokens=output_tokens, estimated_cost_usd=cost_usd,
        )
