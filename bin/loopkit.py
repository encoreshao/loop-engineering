#!/usr/bin/env python3
"""LoopKit: a small plugin runner for "discover items -> ask the model about
each -> act on the answer" loops. A plugin supplies discover() and
after_item(); LoopKit supplies the run lock, de-dup (seen store), the
LoopRuntime iteration with an output-contract verifier (so a malformed
answer is retried with feedback), result/history/last-run files and Slack
notifications. Paths resolve at call time under repo_root so tests can
point everything at tmp_path."""
import contextlib
import fcntl
import hashlib
import json
import re
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import loops_config
import notify as _notify
import seen_store
from agents import sealed
from loop_definition import LoopDefinition
from loop_runtime import LoopRuntime
from loop_serialize import write_result
from loop_state import LoopState
from loop_verifiers import VerificationResult, Verifier, build_verifiers

REPO_ROOT = Path(__file__).resolve().parent.parent
_LAST_RUN_OUTCOME_CAP = 50


@dataclass(frozen=True)
class WorkItem:
    key: str
    title: str
    url: str = ""
    payload: dict = field(default_factory=dict)


@dataclass
class Outcome:
    item_key: str
    status: str  # "done" | "skipped" | "failed"
    summary: str
    url: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class LoopContext:
    loop_name: str
    run_id: str
    now: datetime
    definition: LoopDefinition
    settings: dict
    history_dir: Path
    log: Callable[[str], None]
    repo_root: Path = REPO_ROOT
    force: bool = False


class LoopPlugin:
    loop_name: str = ""
    definition_dir: str = ""
    max_items_per_run: int = 20
    output_keys: tuple = ()

    def discover(self, ctx):
        raise NotImplementedError

    def build_prompt(self, item, ctx):
        template = (Path(ctx.repo_root) / "loops" / self.definition_dir / "prompt.md").read_text()
        item_json = json.dumps({"key": item.key, "title": item.title, "url": item.url,
                                "payload": item.payload}, indent=2, default=str)
        return (template.replace("{{item_json}}", item_json)
                .replace("{{settings_json}}", json.dumps(ctx.settings, indent=2, default=str)))

    def call_model(self, prompt, ctx):
        timeout = ctx.definition.stop_conditions.max_runtime_minutes * 60
        return sealed.sealed_call(prompt, timeout, log=ctx.log)

    def after_item(self, item, answer, ctx):
        raise NotImplementedError

    def digest(self, outcomes, ctx):
        return None


def parse_answer(text, required_keys):
    """Parse the model's answer. With required_keys: a JSON object (optionally
    wrapped in a ```json fence) containing every key, else ValueError.
    Without: any non-empty text, returned as-is."""
    stripped = (text or "").strip()
    if not required_keys:
        if not stripped:
            raise ValueError("empty answer")
        return stripped
    fence = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```$", stripped, re.S)
    if fence:
        stripped = fence.group(1).strip()
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"answer is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("answer must be a JSON object")
    missing = [k for k in required_keys if k not in data]
    if missing:
        raise ValueError(f"answer is missing required keys: {', '.join(missing)}")
    return data


class OutputContractVerifier(Verifier):
    name = "output_contract"

    def __init__(self, holder, required_keys):
        self.holder = holder
        self.required_keys = tuple(required_keys)

    def verify(self, context) -> VerificationResult:
        error = self.holder.get("error")
        if error is None:
            try:
                parse_answer(self.holder.get("text"), self.required_keys)
            except ValueError as exc:
                error = str(exc)
        passed = error is None
        return VerificationResult(name=self.name, passed=passed, exit_code=0 if passed else 1,
                                  duration_ms=0, output="" if passed else error,
                                  evidence={"required_keys": list(self.required_keys)})


@contextlib.contextmanager
def exclusive_run_lock(lock_path):
    """Yields True while this process holds an exclusive, non-blocking flock
    on lock_path, False (immediately) if another run holds it. The kernel
    drops the lock when the holder exits, however it exits, so a killed run
    can never leave it stuck."""
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def raise_on_sigterm(signum, frame):
    """run-loop-now.sh's `timeout` stops a run with SIGTERM, whose default
    action kills Python without running any `finally`. Raising SystemExit
    lets cleanup (and subprocess.run killing the AI child) happen."""
    raise SystemExit(128 + signum)


def _slug(key):
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", key).strip("-")[:40] or "item"
    return f"{base}-{hashlib.sha1(key.encode()).hexdigest()[:8]}"


def _make_logger(repo_root, loop_name):
    path = Path(repo_root) / "logs" / "loop-engineering.log"

    def log(message):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as handle:
                handle.write(f"{datetime.now(timezone.utc).isoformat()} {loop_name}: {message}\n")
        except OSError:
            pass
    return log


def _run_item(plugin, item, ctx, run_id, events_dir, results_dir, repo_root):
    holder = {}

    def agent_fn(context):
        prompt = plugin.build_prompt(item, ctx)
        previous = context.get("previous")
        if previous is not None and holder.get("text") is not None:
            failed = [r for r in previous.verification_results if not r.passed]
            if failed and failed[0].name == OutputContractVerifier.name:
                prompt += ("\n\nYour previous answer violated the output contract: "
                           f"{failed[0].output}. Answer again with valid JSON only.")
            elif failed:
                prompt += ("\n\nYour previous attempt failed verification: "
                           f"{failed[0].output}. Fix it and answer again.")
        holder.pop("error", None)
        holder["text"] = None
        try:
            res = plugin.call_model(prompt, ctx)
        except Exception as exc:  # noqa: BLE001
            # Crashed model calls are surfaced as verification failures so LoopRuntime
            # retries them (metrics show verification.failed; final state ESCALATED).
            holder["error"] = f"model call failed: {type(exc).__name__}: {exc}"
            return {"cost_usd": 0}
        holder["text"] = res["text"]
        return {"cost_usd": res.get("cost_usd")}

    verifiers = [OutputContractVerifier(holder, plugin.output_keys)]
    verifiers += build_verifiers(ctx.definition.verifiers, cwd=repo_root)
    result = LoopRuntime(agent_fn, verifiers, events_dir=events_dir).start(
        ctx.definition, run_id=f"{run_id}_{_slug(item.key)}", loop_id=plugin.loop_name)
    write_result(result, results_dir)
    if result.final_state == LoopState.COMPLETED:
        try:
            outcome = plugin.after_item(item, parse_answer(holder["text"], plugin.output_keys), ctx)
            if not outcome.url:
                outcome.url = item.url
            return outcome
        except Exception as exc:  # noqa: BLE001 - one item's failure must not stop the run
            return Outcome(item.key, "failed", f"{type(exc).__name__}: {exc}", url=item.url)
    cause = holder.get("error") or next(
        (r.output for it in reversed(result.iterations) for r in it.verification_results if not r.passed), "")
    summary = f"loop ended {result.final_state.value}: {result.stop_reason}"
    if cause:
        summary += f" ({str(cause)[:200]})"
    return Outcome(item.key, "failed", summary, url=item.url)


def _write_reports(plugin, run_id, now, outcomes, history_dir, last_run_path):
    counts = {s: sum(1 for o in outcomes if o.status == s) for s in ("done", "skipped", "failed")}
    history_dir = Path(history_dir)
    history_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"# {plugin.loop_name} run {run_id}", ""]
    for o in outcomes:
        lines.append(f"- [{o.status}] {o.item_key}: {o.summary}" + (f" ({o.url})" if o.url else ""))
    (history_dir / f"{now.strftime('%Y-%m-%d_%H%M%S')}.md").write_text("\n".join(lines) + "\n")
    last_run_path.parent.mkdir(parents=True, exist_ok=True)
    last_run_path.write_text(json.dumps({
        "run_id": run_id,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "outcomes": [{"item_key": o.item_key, "status": o.status, "summary": o.summary, "url": o.url}
                     for o in outcomes[:_LAST_RUN_OUTCOME_CAP]],
    }, indent=2) + "\n")
    return counts


def run_plugin(plugin, run_id, now=None, *, repo_root=None, results_dir=None, events_dir=None,
               state_dir=None, history_dir=None, lock_path=None, loop_lookup=None, notifier=None,
               force=False):
    repo_root = Path(repo_root) if repo_root is not None else REPO_ROOT
    now = now or datetime.now(timezone.utc)
    loop_name = plugin.loop_name
    loop_dir = repo_root / "outputs" / "loops" / loop_name
    if lock_path is None:
        lock_path = loop_dir / "run.lock"
    if results_dir is None:
        results_dir = repo_root / "outputs" / "loop-runs"
    if history_dir is None:
        history_dir = loop_dir / "history"
    if notifier is None:
        notifier = lambda loop, text: _notify.notify(loop, text)  # noqa: E731
    if loop_lookup is None:
        loop_lookup = loops_config.get_loop
    log = _make_logger(repo_root, loop_name)

    with exclusive_run_lock(lock_path) as acquired:
        if not acquired:
            log("another run holds the lock; skipping")
            return []
        definition = LoopDefinition.from_yaml(repo_root / "loops" / plugin.definition_dir / "loop.yaml")
        settings = loop_lookup(loop_name).get("settings", {})
        ctx = LoopContext(loop_name=loop_name, run_id=run_id, now=now, definition=definition,
                          settings=settings, history_dir=Path(history_dir), log=log,
                          repo_root=repo_root, force=force)
        try:
            items = plugin.discover(ctx)
        except Exception as exc:
            notifier(loop_name, f"{loop_name} FAILED during discovery: {type(exc).__name__}: {exc}")
            log(f"discovery failed: {type(exc).__name__}")
            raise
        seen = seen_store.SeenStore(loop_name, state_dir=state_dir, now_fn=lambda: now)
        if not force:
            items = [i for i in items if not seen.has(i.key)]
        items = items[:plugin.max_items_per_run]
        log(f"processing {len(items)} item(s)")

        outcomes = []
        try:
            for item in items:
                try:
                    outcome = _run_item(plugin, item, ctx, run_id, events_dir, results_dir, repo_root)
                except Exception as exc:  # noqa: BLE001 - one item's crash must not stop the run
                    outcome = Outcome(item.key, "failed", f"{type(exc).__name__}: {exc}", url=item.url)
                if outcome.status in ("done", "skipped"):
                    seen.add(item.key)
                    seen.save()  # persist now: a later crash/SIGTERM must not re-run acted-on items
                outcomes.append(outcome)
                log(f"item {_slug(item.key)}: {outcome.status}")
        finally:
            seen.save()
            counts = _write_reports(plugin, run_id, now, outcomes, history_dir, loop_dir / "last-run.json")
        text = plugin.digest(outcomes, ctx)
        if text:
            notifier(loop_name, text)
        if counts["failed"]:
            notifier(loop_name, f"{loop_name}: {counts['failed']} of {len(outcomes)} item(s) failed")
        return outcomes


def main(plugin, argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in argv
    args = [a for a in argv if a != "--force"]
    if len(args) != 1:
        print(f"Usage: {plugin.loop_name} <run_id> [--force]", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, raise_on_sigterm)
    outcomes = run_plugin(plugin, args[0], force=force)
    if outcomes and all(o.status == "failed" for o in outcomes):
        return 1
    return 0
