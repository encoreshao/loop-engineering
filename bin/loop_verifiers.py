#!/usr/bin/env python3
"""Verifier interface + CommandVerifier - see
docs/superpowers/specs/2026-09-06-loop-runtime-foundation-design.md.
`context` is currently unused by CommandVerifier (accepted only so the
`Verifier` interface stays uniform for future verifier types that do need
it, e.g. a diff-scope verifier reading the current worktree path out of
context)."""
import dataclasses
import shlex
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import loop_config

_OUTPUT_TAIL_CHARS = 4000


def _decode_or_empty(value):
    """subprocess.TimeoutExpired's .stdout/.stderr are bytes even when
    subprocess.run(..., text=True) was used - text-mode decoding only
    applies to the completed-process path, not the partial-output-on-
    timeout exception path. Decode defensively so CommandVerifier.verify()
    never hands back a bytes `output` (which json.dumps can't serialize -
    see write_result)."""
    if not value:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


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
    def __init__(self, name, command, cwd=None, timeout_seconds=None):
        self.name = name
        self.command = command
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds

    def verify(self, context) -> VerificationResult:
        start = time.monotonic()
        evidence = {"command": self.command, "cwd": str(self.cwd) if self.cwd else None}
        if self.timeout_seconds is not None:
            evidence["timeout_seconds"] = self.timeout_seconds

        try:
            completed = subprocess.run(
                shlex.split(self.command),
                cwd=str(self.cwd) if self.cwd else None,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            evidence["timed_out"] = True
            return VerificationResult(
                name=self.name,
                passed=False,
                exit_code=None,
                duration_ms=duration_ms,
                output=_decode_or_empty(exc.stdout) + _decode_or_empty(exc.stderr),
                evidence=evidence,
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        return VerificationResult(
            name=self.name,
            passed=completed.returncode == 0,
            exit_code=completed.returncode,
            duration_ms=duration_ms,
            output=completed.stdout + completed.stderr,
            evidence=evidence,
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


class ProjectCommandsVerifier(Verifier):
    """Re-runs the project's own test_cmd/lint_cmd (from projects.json)
    inside the worktree the agent used for this issue. No worktree means
    the agent made no code change, so there is nothing to verify (a
    vacuous pass). Empty/missing commands are skipped."""

    def __init__(self, name, alias, issue_iid, timeout_seconds,
                 project_fn=None, worktree_root_fn=None, runner=None):
        self.name = name
        self.alias = alias
        self.issue_iid = issue_iid
        self.timeout_seconds = timeout_seconds
        self.project_fn = project_fn
        self.worktree_root_fn = worktree_root_fn
        self.runner = runner

    def verify(self, context) -> VerificationResult:
        # Never raises: LoopRuntime calls verifiers unguarded, and a
        # misconfigured project or missing binary must not crash a batch.
        try:
            return self._verify(context)
        except Exception as exc:  # noqa: BLE001
            return VerificationResult(self.name, False, None, 0, f"{type(exc).__name__}: {exc}", {"error": True})

    def _verify(self, context) -> VerificationResult:
        start = time.monotonic()
        project_fn = self.project_fn or loop_config.get_project
        worktree_root_fn = self.worktree_root_fn or loop_config.get_worktree_root
        project = project_fn(self.alias)
        worktree = Path(worktree_root_fn()) / f"{Path(project['local_path']).name}-issue-{self.issue_iid}"
        if not worktree.is_dir():
            return VerificationResult(self.name, True, None, 0, "no worktree - nothing to verify", {"vacuous": True})

        commands, chunks, passed = [], [], True
        for kind in ("test", "lint"):
            command = project.get(f"{kind}_cmd")
            if not command:
                continue
            verifier_cls = self.runner or CommandVerifier
            result = verifier_cls(
                name=f"{self.name}_{kind}", command=command, cwd=worktree, timeout_seconds=self.timeout_seconds,
            ).verify(context)
            passed = passed and result.passed
            commands.append({"kind": kind, "command": command, "passed": result.passed, "exit_code": result.exit_code})
            chunks.append(f"$ {command}\n{result.output[-_OUTPUT_TAIL_CHARS:]}")

        return VerificationResult(
            name=self.name,
            passed=passed,
            exit_code=0 if passed else 1,
            duration_ms=int((time.monotonic() - start) * 1000),
            output="\n".join(chunks),
            evidence={"commands": commands},
        )


class ObserveOnly(Verifier):
    """Wraps a verifier so it is recorded but never fails the loop; the
    real outcome is kept in evidence["observed_passed"]."""

    def __init__(self, inner):
        self.inner = inner

    def verify(self, context) -> VerificationResult:
        result = self.inner.verify(context)
        return dataclasses.replace(
            result, passed=True,
            evidence={**result.evidence, "observed_passed": result.passed, "mode": "observe"},
        )


def build_verifiers(specs, cwd=None, issue=None, mode="observe"):
    """Build real Verifier instances from raw LoopDefinition.verifiers
    spec dicts - see docs/superpowers/specs/2026-09-07-loop-cli-design.md.
    Fails loud (ValueError) on an unknown type or a spec missing its
    required key, rather than silently skipping a misconfigured
    verifier."""
    verifiers = []
    for spec in specs:
        name = spec["name"]
        spec_type = spec.get("type")
        if spec_type == "command":
            if "command" not in spec:
                raise ValueError(f"verifier {name!r}: type 'command' requires a 'command' key")
            verifiers.append(CommandVerifier(name=name, command=spec["command"], cwd=cwd))
        elif spec_type == "git_diff":
            if "allowed_paths" not in spec:
                raise ValueError(f"verifier {name!r}: type 'git_diff' requires an 'allowed_paths' key")
            verifiers.append(DiffVerifier(name=name, allowed_paths=spec["allowed_paths"], cwd=cwd))
        elif spec_type == "project_commands":
            if not issue:
                raise ValueError(f"verifier {name!r}: type 'project_commands' requires issue context")
            verifier = ProjectCommandsVerifier(
                name=name, alias=issue["alias"], issue_iid=issue["issue_iid"],
                timeout_seconds=issue["timeout_seconds"],
            )
            verifiers.append(ObserveOnly(verifier) if mode == "observe" else verifier)
        else:
            raise ValueError(f"verifier {name!r}: unknown verifier type {spec_type!r}")
    return verifiers
