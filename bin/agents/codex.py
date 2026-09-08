#!/usr/bin/env python3
"""CodexAgent - see docs/superpowers/specs/2026-09-08-agent-adapter-design.md.
estimated_cost_usd is always None: the Codex CLI emits no structured cost
today, an existing, unchanged limitation - see
docs/superpowers/specs/2026-09-04-cost-tracking-design.md."""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agents.base import Agent, AgentResult, classify_subprocess_error


class CodexAgent(Agent):
    def run(self, prompt, context, *, cwd, timeout_seconds, allowed_tools=None,
            disallowed_tools=None, add_dirs=(), output_format="json"):
        cmd = ["codex", "exec", "--sandbox", "workspace-write"]
        if add_dirs:
            writable_roots = json.dumps([str(d) for d in add_dirs], separators=(",", ":"))
            cmd += ["-c", f"sandbox_workspace_write.writable_roots={writable_roots}"]
            cmd += ["-c", "approval_policy=never"]
        else:
            cmd += ["-c", "approval_policy=never"]
        cmd += ["-c", "sandbox_workspace_write.network_access=true", prompt]

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout_seconds, check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            status, _reason = classify_subprocess_error(exc, timeout_seconds)
            detail = exc.stderr or exc.output or ""
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", "replace")
            return AgentResult(
                status=status, output=detail, exit_code=getattr(exc, "returncode", None),
                duration_ms=int((time.monotonic() - start) * 1000),
                input_tokens=None, output_tokens=None, estimated_cost_usd=None,
            )

        return AgentResult(
            status="success", output=proc.stdout, exit_code=0,
            duration_ms=int((time.monotonic() - start) * 1000),
            input_tokens=None, output_tokens=None, estimated_cost_usd=None,
        )
