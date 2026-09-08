#!/usr/bin/env python3
"""CodexAgent - placeholder for future Codex CLI adapter.
See docs/superpowers/specs/2026-09-08-agent-adapter-design.md."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agents.base import Agent


class CodexAgent(Agent):
    def run(self, prompt, context, *, cwd, timeout_seconds, allowed_tools=None,
            disallowed_tools=None, add_dirs=(), output_format="json"):
        raise NotImplementedError("CodexAgent not yet implemented")
