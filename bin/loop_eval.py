#!/usr/bin/env python3
"""Evaluation dataset harness - see
docs/superpowers/specs/2026-09-09-loop-eval-harness-design.md. Drives the
real, unmodified LoopRuntime with a ScriptedAgent and ScriptedVerifiers so
a passing case is evidence about the runtime's stop/verify/escalate/cost
behavior, not about this harness's own logic."""
from dataclasses import dataclass
from pathlib import Path

import yaml

from loop_definition import LoopDefinition
from loop_verifiers import VerificationResult, Verifier


class ScriptExhausted(RuntimeError):
    pass


class ScriptedAgent:
    """A callable agent_fn: pops the next scripted {changed, cost_usd}
    dict per call, in order."""

    def __init__(self, script):
        self._script = list(script)
        self._calls = 0

    def __call__(self, context):
        if self._calls >= len(self._script):
            raise ScriptExhausted(
                f"ScriptedAgent has no entry for call {self._calls + 1} "
                f"- only {len(self._script)} scripted"
            )
        entry = self._script[self._calls]
        self._calls += 1
        return {"changed": entry.get("changed", True), "cost_usd": entry.get("cost_usd")}


class ScriptedVerifier(Verifier):
    """A Verifier ABC implementation: pops the next scripted bool per
    call, in order, for one named verifier."""

    def __init__(self, name, script):
        self.name = name
        self._script = list(script)
        self._calls = 0

    def verify(self, context):
        if self._calls >= len(self._script):
            raise ScriptExhausted(
                f"ScriptedVerifier {self.name!r} has no entry for call "
                f"{self._calls + 1} - only {len(self._script)} scripted"
            )
        passed = self._script[self._calls]
        self._calls += 1
        return VerificationResult(
            name=self.name,
            passed=passed,
            exit_code=0 if passed else 1,
            duration_ms=0,
            output="",
            evidence={},
        )


@dataclass
class EvalCase:
    name: str
    description: str
    definition: LoopDefinition
    agent_script: list
    verifier_scripts: dict
    expect: dict


_REQUIRED_CASE_FIELDS = ("name", "description", "definition", "script", "expect")


def load_case(path):
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    for required_key in _REQUIRED_CASE_FIELDS:
        if required_key not in data:
            raise ValueError(f"eval case {path}: missing required field '{required_key}'")

    script = data["script"]
    return EvalCase(
        name=data["name"],
        description=data["description"],
        definition=LoopDefinition.from_dict(data["definition"]),
        agent_script=script.get("agent", []),
        verifier_scripts=script.get("verifiers", {}),
        expect=data["expect"],
    )


def load_cases(cases_dir):
    return [load_case(p) for p in sorted(Path(cases_dir).glob("*.yaml"))]
