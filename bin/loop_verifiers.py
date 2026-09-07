#!/usr/bin/env python3
"""Verifier interface + CommandVerifier - see
docs/superpowers/specs/2026-09-06-loop-runtime-foundation-design.md.
`context` is currently unused by CommandVerifier (accepted only so the
`Verifier` interface stays uniform for future verifier types that do need
it, e.g. a diff-scope verifier reading the current worktree path out of
context)."""
import shlex
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class VerificationResult:
    name: str
    passed: bool
    exit_code: int | None
    duration_ms: int
    output: str
    evidence: dict


class Verifier(ABC):
    @abstractmethod
    def verify(self, context) -> VerificationResult:
        ...


class CommandVerifier(Verifier):
    def __init__(self, name, command, cwd=None):
        self.name = name
        self.command = command
        self.cwd = cwd

    def verify(self, context) -> VerificationResult:
        start = time.monotonic()
        completed = subprocess.run(
            shlex.split(self.command),
            cwd=str(self.cwd) if self.cwd else None,
            capture_output=True,
            text=True,
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        return VerificationResult(
            name=self.name,
            passed=completed.returncode == 0,
            exit_code=completed.returncode,
            duration_ms=duration_ms,
            output=completed.stdout + completed.stderr,
            evidence={"command": self.command, "cwd": str(self.cwd) if self.cwd else None},
        )


def _is_allowed_path(path, allowed_paths):
    for allowed in allowed_paths:
        prefix = allowed if allowed.endswith("/") else allowed + "/"
        if path == allowed or path.startswith(prefix):
            return True
    return False


class DiffVerifier(Verifier):
    """Fails if any changed file (tracked-modified or new-untracked)
    falls outside `allowed_paths` - see
    docs/superpowers/specs/2026-09-07-diff-verifier-design.md."""

    def __init__(self, name, allowed_paths, cwd=None):
        self.name = name
        self.allowed_paths = list(allowed_paths)
        self.cwd = cwd

    def verify(self, context) -> VerificationResult:
        start = time.monotonic()
        cwd = str(self.cwd) if self.cwd else None

        tracked = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"], cwd=cwd, capture_output=True, text=True
        )
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"], cwd=cwd, capture_output=True, text=True
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        changed_files = sorted(
            {line for line in tracked.stdout.splitlines() if line}
            | {line for line in untracked.stdout.splitlines() if line}
        )
        disallowed_files = [f for f in changed_files if not _is_allowed_path(f, self.allowed_paths)]
        passed = not disallowed_files

        return VerificationResult(
            name=self.name,
            passed=passed,
            exit_code=0 if passed else 1,
            duration_ms=duration_ms,
            output="\n".join(disallowed_files),
            evidence={
                "changed_files": changed_files,
                "disallowed_files": disallowed_files,
                "allowed_paths": self.allowed_paths,
            },
        )
