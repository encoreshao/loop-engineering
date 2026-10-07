#!/usr/bin/env python3
"""Golden eval suite: runs the real GitLab issue agent (gate mode) against
synthetic fixture repos under evals/golden/<case>/case.yaml and grades what
it did - the handoff action, which files it touched, and whether the
project's checks pass afterwards. Unlike bin/loop_eval.py (scripted agents,
free), every real case costs money, so the suite has a hard budget and
stops launching cases once it is spent.

Grading and suite control are pure; `run_case_real` is the paid path."""
import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES_DIR = REPO_ROOT / "evals" / "golden"
NEW_WORKTREE_SCRIPT = REPO_ROOT / "bin" / "scripts" / "new_worktree.sh"
DEFAULT_LAST_RUN_PATH = REPO_ROOT / "outputs" / "evals" / "golden-last.json"
_REPO_LAST_RUN_PATH = DEFAULT_LAST_RUN_PATH
EVALS_DIR_ENV = "LOOP_EVALS_DIR"

ACTIONS = ("fix", "answer", "escalate")
ISSUE_IID = 1
ALIAS = "golden"
DEFAULT_MAX_ATTEMPTS = 2  # loops/gitlab-issue/loop.yaml's max_iterations
DEFAULT_BRANCH = "main"
_GIT_IDENTITY = ("-c", "user.name=golden-eval", "-c", "user.email=golden-eval@example.invalid")
# Build/test droppings that are never "a change the agent made".
_IGNORED = ("__pycache__/", "*.pyc", ".pytest_cache/", "node_modules/", ".DS_Store")


def load_case(path):
    case = yaml.safe_load(Path(path).read_text())
    if not isinstance(case, dict):
        raise ValueError(f"{path}: case must be a mapping")
    for key in ("name", "issue", "repo", "expect"):
        if key not in case:
            raise ValueError(f"{path}: missing {key!r}")
    action = (case.get("expect") or {}).get("action")
    if action not in ACTIONS:
        raise ValueError(f"{path}: expect.action must be one of {', '.join(ACTIONS)}, got {action!r}")
    if not (case.get("repo") or {}).get("files"):
        raise ValueError(f"{path}: repo.files must list at least one file")
    case["expect"].setdefault("allowed_paths", [])
    case["expect"].setdefault("checks_pass", None)
    return case


def load_cases(cases_dir=None, names=None):
    if cases_dir is None:
        cases_dir = DEFAULT_CASES_DIR
    cases = [load_case(p) for p in sorted(Path(cases_dir).glob("*/case.yaml"))]
    if names:
        known = {c["name"] for c in cases}
        unknown = [n for n in names if n not in known]
        if unknown:
            raise ValueError(f"unknown golden case(s): {', '.join(unknown)}")
        cases = [c for c in cases if c["name"] in names]
    return cases


def _git(*args, cwd=None):
    return subprocess.run(["git", *_GIT_IDENTITY, *args], cwd=cwd, capture_output=True, text=True, check=True)


def worktree_path(repo, root):
    """Where new_worktree.sh puts issue #1's worktree for this fixture."""
    return Path(root) / "worktrees" / f"{Path(repo).name}-issue-{ISSUE_IID}"


def build_fixture(case, root, new_worktree_script=None):
    """A fresh git repo with the case's files committed as "initial" (also
    tagged `initial`), a local bare `origin`, and issue #1's worktree on
    loop/issue-1 created by the real new_worktree.sh - so the layout is
    exactly what the agent's own new_worktree.sh call expects to find."""
    if new_worktree_script is None:
        new_worktree_script = NEW_WORKTREE_SCRIPT
    root = Path(root)
    repo = root / "fixture-repo"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", DEFAULT_BRANCH, cwd=repo)
    # Repo-local identity: the agent commits inside the worktree, which
    # shares this config, and must not depend on the machine's global one.
    _git("config", "user.name", "golden-eval", cwd=repo)
    _git("config", "user.email", "golden-eval@example.invalid", cwd=repo)
    (repo / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("\n".join(_IGNORED) + "\n")
    for rel, content in case["repo"]["files"].items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if content is not None else "")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "initial", cwd=repo)
    _git("tag", "initial", cwd=repo)
    origin = root / "fixture-origin.git"
    _git("clone", "-q", "--bare", str(repo), str(origin))
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("fetch", "-q", "origin", cwd=repo)
    subprocess.run(
        ["bash", str(new_worktree_script), str(repo), DEFAULT_BRANCH, str(ISSUE_IID), str(root / "worktrees")],
        capture_output=True, text=True, check=True,
    )
    return repo


def _lines(result):
    return {line for line in result.stdout.splitlines() if line}


def changed_paths(repo, worktree):
    """Every path that differs from `initial`: in the issue worktree
    (committed, modified or untracked) and in the source checkout itself,
    which the agent must never edit directly."""
    changed = set()
    for cwd in (worktree, repo):
        if not Path(cwd).is_dir():
            continue
        changed |= _lines(_git("diff", "--name-only", "--no-renames", "initial", cwd=cwd))
        changed |= _lines(_git("ls-files", "--others", "--exclude-standard", cwd=cwd))
    return sorted(changed)


def _allowed(path, patterns):
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def grade(case, handoff, changed_paths, checks_passed):
    """(passed, reasons). `handoff` is the validated handoff dict or None;
    `checks_passed` is the project's checks after the last attempt (None
    when they were not run, i.e. no fix was handed off)."""
    expect = case["expect"]
    if handoff is None:
        return False, ["no handoff (missing or malformed)"]
    reasons = []
    action = handoff.get("action")
    if action != expect["action"]:
        reasons.append(f"expected action {expect['action']}, got {action}")
    allowed = expect.get("allowed_paths") or []
    for path in changed_paths:
        if not _allowed(path, allowed):
            reasons.append(f"changed {path} outside allowed paths {allowed}")
    if expect.get("checks_pass") is True and checks_passed is not True:
        reasons.append("project checks did not pass" if checks_passed is False else "project checks were not run")
    return not reasons, reasons


def run_suite(cases, budget_usd=10.0, run_case=None):
    """Run cases in order until the budget is spent; the rest are reported
    as not_run. `run_case(case)` returns {"passed", "reasons", "cost_usd"};
    by default it is the real, paid path, capped at what is left of the
    budget. A case that raises counts as failed, never as a crash. A case
    that reports no cost (None) is booked at what was left of the budget -
    it may have spent all of it - so the suite total stays bounded."""
    results, not_run, spent = [], [], 0.0
    for case in cases:
        if spent >= budget_usd:
            not_run.append(case["name"])
            continue
        remaining = budget_usd - spent
        runner = run_case
        if runner is None:
            def runner(c, remaining=remaining):
                return run_case_real(c, max_budget_usd=remaining)
        try:
            outcome = runner(case)
        except Exception as exc:  # noqa: BLE001 - one broken case must not stop the suite
            outcome = {"passed": False, "reasons": [f"case crashed: {type(exc).__name__}: {exc}"],
                       "cost_usd": getattr(exc, "cost_usd", None)}
        cost = outcome.get("cost_usd")
        if cost is None:
            cost = round(remaining, 6)
        spent += cost
        results.append({"name": case["name"], "passed": bool(outcome.get("passed")),
                        "reasons": list(outcome.get("reasons") or []), "cost_usd": cost})
    return {"results": results, "not_run": not_run, "spent_usd": round(spent, 6)}


def exit_code(summary):
    """0 only when at least one case ran, every case ran, and all passed."""
    results = summary.get("results") or []
    if not results or summary.get("not_run"):
        return 1
    return 0 if all(r.get("passed") for r in results) else 1


# --- the real (paid) path ----------------------------------------------------

def _resolve_last_run_path(path):
    """Resolved at call time: an explicit path, then a monkeypatched
    DEFAULT_LAST_RUN_PATH, then $LOOP_EVALS_DIR/golden-last.json, then the
    repo's outputs/evals/golden-last.json."""
    if path is not None:
        return Path(path)
    if DEFAULT_LAST_RUN_PATH != _REPO_LAST_RUN_PATH:
        return Path(DEFAULT_LAST_RUN_PATH)
    env = os.environ.get(EVALS_DIR_ENV)
    return Path(env) / "golden-last.json" if env else Path(DEFAULT_LAST_RUN_PATH)


def write_last_run(summary, budget_usd, path=None):
    """outputs/evals/golden-last.json - per checkout, like outputs/events/:
    the latest `loop eval --golden` run, for PR before/after summaries."""
    path = _resolve_last_run_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "budget_usd": budget_usd,
        "spent_usd": summary.get("spent_usd", 0.0),
        "results": summary.get("results", []),
        "not_run": summary.get("not_run", []),
    }
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def prepare_sandbox(case, root):
    """Everything one real case runs against, all under `root`: the fixture
    repo and its issue worktree, a LOOP_ENGINEERING_HOME holding a
    projects.json that points only at the fixture, a scratch events dir,
    and the issue JSON the --issue-file prompt reads."""
    root = Path(root)
    repo = build_fixture(case, root / "fixture")
    worktree = worktree_path(repo, root / "fixture")
    home = root / "home"
    home.mkdir()
    project = {
        "project_id": 0,
        "local_path": str(repo),
        "target_branch": DEFAULT_BRANCH,
        "install_cmd": "",
        "test_cmd": case["repo"].get("test_cmd") or "",
        "lint_cmd": case["repo"].get("lint_cmd") or "",
    }
    config = {
        "gitlab_instance": "golden-eval.invalid",
        "assignee_username": "golden-eval",
        "worktree_root": str(worktree.parent),
        "projects": {ALIAS: project},
    }
    (home / "projects.json").write_text(json.dumps(config, indent=2))
    # Claude is the CLI that reports cost and enforces the tool denials.
    (home / "ai_cli.json").write_text(json.dumps({"cli": "claude"}))
    events_dir = root / "events"
    events_dir.mkdir()
    issue_file = root / "issue.json"
    issue_file.write_text(json.dumps({"title": case["issue"]["title"], "body": case["issue"]["body"]}))
    return {
        "root": root, "repo": repo, "worktree": worktree, "home": home, "project": project,
        "events_dir": events_dir, "issue_file": issue_file, "log": root / "loop.log",
        "env": {"LOOP_ENGINEERING_HOME": str(home), "LOOP_EVENTS_DIR": str(events_dir)},
    }


def run_agent_subprocess(sandbox, run_id, feedback, max_budget_usd, repo_root, timeout_seconds):
    """One agent attempt in a child process whose environment points at the
    sandbox. A child, not an in-process call, because loop_config and
    ai_cli_config bind LOOP_ENGINEERING_HOME at import time: only a fresh
    interpreter is guaranteed never to read the real ~/.loop-engineering."""
    root = Path(sandbox["root"])
    result_file = root / f"agent-result-{uuid.uuid4().hex[:8]}.json"
    cmd = [sys.executable, str(Path(repo_root) / "bin" / "golden_eval.py"), "agent",
           "--issue-file", str(sandbox["issue_file"]), "--run-id", run_id,
           "--result-file", str(result_file), "--repo-root", str(repo_root),
           "--log", str(sandbox["log"]), "--timeout", str(timeout_seconds)]
    if max_budget_usd is not None:
        cmd += ["--max-budget-usd", f"{max_budget_usd:.4f}"]
    if feedback:
        feedback_file = root / "feedback.txt"
        feedback_file.write_text(feedback)
        cmd += ["--feedback-file", str(feedback_file)]
    env = {**os.environ, **sandbox["env"], "LOOP_RUN_ID": run_id}
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout_seconds + 120)
    except subprocess.TimeoutExpired:
        return {"cost_usd": None, "error": f"agent subprocess timed out after {timeout_seconds + 120}s"}
    try:
        return json.loads(result_file.read_text())
    except (OSError, ValueError):
        tail = (proc.stderr or proc.stdout or "")[-800:]
        return {"cost_usd": None, "error": f"agent subprocess exited {proc.returncode}: {tail}"}


def _run_checks(sandbox, handoff, timeout_seconds):
    import loop_verifiers

    project = sandbox["project"]
    return loop_verifiers.ProjectCommandsVerifier(
        "golden_checks", ALIAS, ISSUE_IID, timeout_seconds,
        project_fn=lambda _alias: project,
        worktree_root_fn=lambda: str(Path(sandbox["worktree"]).parent),
        handoff_path=handoff,
    ).verify({})


def run_case_real(case, max_budget_usd=None, repo_root=None, agent_runner=None,
                  max_attempts=DEFAULT_MAX_ATTEMPTS, timeout_seconds=900):
    """Run the real agent on one case: gate mode, offline, in a throwaway
    sandbox. Mirrors the gated loop: after a fix handoff the project's
    checks are re-run, and a failure is fed back for one more attempt (up to
    `max_attempts`, the loop's own max_iterations). Each attempt is capped at
    what is left of `max_budget_usd`; the case's cost is the CLI's reported
    spend summed over attempts; an attempt whose cost is unknown (timeout,
    crash, no envelope) is booked at the cap it was given. Never raises: a
    crash mid-case fails the case with the spend booked so far."""
    import cost as cost_module
    import gitlab_loop_runner as glr

    if repo_root is None:
        repo_root = REPO_ROOT
    if agent_runner is None:
        agent_runner = run_agent_subprocess
    run_id = f"golden-{case['name']}-{uuid.uuid4().hex[:8]}"
    handoff_file = glr.handoff_path(run_id, ALIAS, ISSUE_IID, repo_root=repo_root)
    spent, reasons, handoff, checks = 0.0, [], None, None
    in_flight_cap = None  # the cap of an agent call whose cost is not booked yet
    with tempfile.TemporaryDirectory(prefix="golden-eval-") as tmp:
        try:
            sandbox = prepare_sandbox(case, tmp)
            feedback = None
            for _attempt in range(max_attempts):
                if max_budget_usd is not None and spent >= max_budget_usd:
                    reasons.append("budget exhausted before the case finished")
                    break
                cap = cost_module.remaining_budget(max_budget_usd, spent)
                handoff_file.unlink(missing_ok=True)
                in_flight_cap = cap
                outcome = agent_runner(sandbox, run_id, feedback, cap, repo_root, timeout_seconds)
                cost = outcome.get("cost_usd")
                spent += cost if cost is not None else (cap or 0)
                in_flight_cap = None
                if outcome.get("error"):
                    reasons.append(f"agent call failed: {outcome['error']}")
                handoff = glr.read_handoff(handoff_file, ISSUE_IID)
                if outcome.get("error") or handoff is None or handoff["action"] != "fix":
                    checks = None
                    break
                verification = _run_checks(sandbox, handoff_file, timeout_seconds)
                checks = verification.passed
                if checks:
                    break
                feedback = glr.format_feedback(SimpleNamespace(verification_results=[verification]))
            passed, grade_reasons = grade(case, handoff, changed_paths(sandbox["repo"], sandbox["worktree"]), checks)
        except Exception as exc:  # noqa: BLE001 - reported as a failed case, the suite goes on
            spent += in_flight_cap or 0
            passed, grade_reasons = False, [f"case crashed: {type(exc).__name__}: {exc}"]
        finally:
            shutil.rmtree(handoff_file.parent, ignore_errors=True)
    reasons = reasons + grade_reasons
    return {"passed": passed and not reasons, "reasons": reasons, "cost_usd": round(spent, 6)}


def _agent_main(argv):
    """`golden_eval.py agent ...` - the child side of run_agent_subprocess."""
    parser = argparse.ArgumentParser(prog="golden_eval.py agent")
    parser.add_argument("--issue-file", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--max-budget-usd", type=float)
    parser.add_argument("--feedback-file")
    parser.add_argument("--log")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args(argv)
    if not os.environ.get("LOOP_ENGINEERING_HOME"):
        print("golden_eval.py agent: refusing to run without a sandbox LOOP_ENGINEERING_HOME", file=sys.stderr)
        return 2
    import gitlab_loop_runner as glr

    feedback = Path(args.feedback_file).read_text() if args.feedback_file else None
    try:
        result = glr.invoke_issue_file_agent(
            ALIAS, ISSUE_IID, args.issue_file, repo_root=Path(args.repo_root),
            timeout_seconds=args.timeout, unified_log_path=Path(args.log) if args.log else None,
            feedback=feedback, run_id=args.run_id, max_budget_usd=args.max_budget_usd,
        )
        out = {"cost_usd": (result or {}).get("cost_usd"), "error": None}
    except Exception as exc:  # noqa: BLE001 - reported to the parent, which grades it
        out = {"cost_usd": getattr(exc, "cost_usd", None), "error": f"{type(exc).__name__}: {exc}"}
    Path(args.result_file).write_text(json.dumps(out))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "agent":
        return _agent_main(argv[1:])
    print("Usage: golden_eval.py agent ... (run the suite with `loop_cli.py eval --golden`)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
