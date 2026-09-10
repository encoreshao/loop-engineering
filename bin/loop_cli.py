#!/usr/bin/env python3
"""`loop` CLI - see docs/superpowers/specs/2026-09-07-loop-cli-design.md.
Manual sys.argv subcommand dispatch (matching bin/events.py's style, not
argparse). `run` invokes a real agent via `--prompt`/`--prompt-file`; with
neither flag given, its agent_fn falls back to the original no-op
(L0/observe, plan section 32). `replay` also re-invokes a real agent, using
the prompt and definition path recorded by the original `run`."""
import sys
import time
import uuid
from pathlib import Path

from agents.base import get_agent
from loop_audit import CheckStatus, audit_definition
from loop_definition import LoopDefinition
from loop_eval import run_all
from loop_result import LoopResult
from loop_runtime import LoopRuntime
from loop_serialize import find_latest_result, list_results, read_result, summarize_run_costs, write_result
from loop_state import LoopState
from loop_verifiers import build_verifiers

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"
DEFAULT_EVAL_CASES_DIR = REPO_ROOT / "evals" / "cases"

# Floor guardrail for the CLI's own run/replay invocations - same deny list
# as bin/gitlab_loop_runner.py's _DISALLOWED_TOOLS. Defense in depth: even if
# a loop definition ever supplied its own (looser) allow list, these can
# never run.
_DEFAULT_DISALLOWED_TOOLS = (
    "Bash(git merge*) Bash(git push --force*) Bash(git push -f*) Bash(git checkout*) "
    "Bash(git reset*) Bash(git clean*) Read(**/.env*) Read(**/*.key) Read(**/id_rsa*)"
)


def _make_progress_writer(results_dir):
    def _write_progress(run_id, loop_id, definition_name, iterations):
        partial = LoopResult(
            loop_id=loop_id,
            run_id=run_id,
            definition_name=definition_name,
            final_state="running",
            iterations=iterations,
            stop_reason="running",
            status="running",
        )
        write_result(partial, results_dir=results_dir)
    return _write_progress


def _available_templates():
    if not TEMPLATES_DIR.exists():
        return {}
    return {p.parent.name: p for p in sorted(TEMPLATES_DIR.glob("*/loop.yaml"))}


def _parse_flag(argv, name, default=None):
    if name not in argv:
        return default
    idx = argv.index(name)
    if idx + 1 >= len(argv):
        return default
    return argv[idx + 1]


def _cmd_init(argv):
    template_name = _parse_flag(argv, "--template", "generic")
    target_dir = Path(_parse_flag(argv, "--dir", "."))
    force = "--force" in argv

    templates = _available_templates()
    if template_name not in templates:
        print(f"init: unknown template {template_name!r} (choices: {', '.join(sorted(templates))})", file=sys.stderr)
        return 1

    loop_dir = target_dir / ".loop"
    loop_yaml_path = loop_dir / "loop.yaml"
    if loop_yaml_path.exists() and not force:
        print(f"init: {loop_yaml_path} already exists - pass --force to overwrite", file=sys.stderr)
        return 1

    loop_dir.mkdir(parents=True, exist_ok=True)
    loop_yaml_path.write_text(templates[template_name].read_text())
    print(f"Created {loop_yaml_path}")
    return 0


def _cmd_validate(argv):
    if not argv:
        print("Usage: loop_cli.py validate <path/to/loop.yaml>", file=sys.stderr)
        return 2

    try:
        definition = LoopDefinition.from_yaml(argv[0])
    except (ValueError, OSError) as exc:
        print(f"Invalid loop configuration: {exc}", file=sys.stderr)
        return 1

    print("✓ trigger configured" if definition.trigger.type else "✗ trigger missing")
    print("✓ goal configured" if definition.goal.type else "✗ goal missing")
    print("✓ verifier(s) configured" if definition.verifiers else "✗ no verifiers configured")
    print("✓ stop conditions configured")
    print("✓ budget configured" if definition.stop_conditions.max_cost_usd is not None else "✗ budget unbounded")
    print("✓ human gates configured" if definition.human_gates else "✗ no human gates configured")
    print()
    print("Loop configuration valid.")
    return 0


def _cmd_audit(argv):
    if not argv:
        print("Usage: loop_cli.py audit <path/to/loop.yaml>", file=sys.stderr)
        return 2

    definition = LoopDefinition.from_yaml(argv[0])
    report = audit_definition(definition)

    for check in report.checks:
        print(f"{check.status.value:<4}  {check.name:<22} {check.detail}")
    print()
    if report.is_partial:
        print(f"Loop Ready Score: {report.score} / 100 (partial - missing: {', '.join(report.missing_components)})")
    else:
        print(f"Loop Ready Score: {report.score} / 100")

    return 1 if any(c.status == CheckStatus.FAIL for c in report.checks) else 0


def _cmd_run(argv):
    if not argv:
        print(
            "Usage: loop_cli.py run <path/to/loop.yaml> [--cwd PATH] [--results-dir PATH] "
            "[--prompt TEXT | --prompt-file PATH]",
            file=sys.stderr,
        )
        return 2

    definition_path = Path(argv[0])
    cwd = _parse_flag(argv, "--cwd", str(definition_path.resolve().parent))
    results_dir = _parse_flag(argv, "--results-dir")
    prompt_file = _parse_flag(argv, "--prompt-file")
    if prompt_file:
        try:
            prompt = Path(prompt_file).read_text()
        except OSError as exc:
            print(f"run: could not read --prompt-file: {exc}", file=sys.stderr)
            return 1
    else:
        prompt = _parse_flag(argv, "--prompt")

    definition = LoopDefinition.from_yaml(definition_path)
    verifiers = build_verifiers(definition.verifiers, cwd=cwd)

    run_id = f"run_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    if prompt is None:
        agent_fn = lambda context: {"changed": False}  # noqa: E731 - L0/observe, unchanged default
    else:
        agent = get_agent(definition.agent.provider)

        def agent_fn(context):
            agent_result = agent.run(
                prompt, context, cwd=cwd, timeout_seconds=definition.stop_conditions.max_runtime_minutes * 60,
                disallowed_tools=_DEFAULT_DISALLOWED_TOOLS,
            )
            if agent_result.status != "success":
                print(f"agent invocation {agent_result.status}: {agent_result.output[-800:]}", file=sys.stderr)
                raise RuntimeError(agent_result.output[-800:])
            return {"cost_usd": agent_result.estimated_cost_usd}

    loop_id = f"loop_{uuid.uuid4().hex}"
    on_iteration = _make_progress_writer(results_dir)
    on_iteration(run_id, loop_id, definition.name, [])  # initial running snapshot, before any iteration completes

    runtime = LoopRuntime(agent_fn=agent_fn, verifiers=verifiers, on_iteration=on_iteration)

    try:
        result = runtime.start(definition, run_id=run_id, loop_id=loop_id)
    except ValueError as exc:
        print(f"run: policy violation, refusing to start: {exc}", file=sys.stderr)
        return 1

    result.prompt = prompt
    result.definition_path = str(definition_path.resolve())
    result.status = "finished"

    path = write_result(result, results_dir=results_dir)
    print(f"run_id: {result.run_id}")
    print(f"final_state: {result.final_state.value}")
    print(f"stop_reason: {result.stop_reason}")
    print(f"Result written to {path}")

    return 0 if result.final_state == LoopState.COMPLETED else 1


def _cmd_status(argv):
    results_dir = _parse_flag(argv, "--results-dir")
    latest = find_latest_result(results_dir=results_dir)

    if latest is None:
        print("No runs yet.")
        return 0

    data = read_result(latest)
    print(data["definition_name"])
    print()

    if data.get("status") == "running":
        last_iteration = data["iterations"][-1] if data["iterations"] else {}
        budget = last_iteration.get("budget", {})
        iterations_budget = budget.get("iterations", {})
        cost_budget = budget.get("cost", {})

        print("Status: RUNNING")
        print(f"Run: {data['run_id']}")

        used = iterations_budget.get("used")
        limit = iterations_budget.get("limit")
        if used is not None:
            if limit:
                print(f"Iteration: {used}/{limit}")
                pct = min(used / limit, 1.0)
                filled = int(pct * 10)
                bar = "█" * filled + "░" * (10 - filled)
                print(f"Budget: {bar} {pct * 100:.0f}%")
            else:
                print(f"Iteration: {used}")

        cost_used = cost_budget.get("used_usd")
        if cost_used is not None:
            print(f"Cost: ${cost_used:.2f}")

        return 0

    print(f"Status: {data['final_state'].upper()}")
    print(f"Run: {data['run_id']}")
    print(f"Iterations: {len(data['iterations'])}")
    return 0


def _cmd_inspect(argv):
    if not argv:
        print("Usage: loop_cli.py inspect <run_id> [--results-dir PATH]", file=sys.stderr)
        return 2

    run_id = argv[0]
    results_dir = _parse_flag(argv, "--results-dir")

    for path in list_results(results_dir=results_dir):
        data = read_result(path)
        if data["run_id"] == run_id:
            _print_run_detail(data)
            return 0

    print(f"inspect: no result found for run_id {run_id!r}", file=sys.stderr)
    return 1


def _cmd_replay(argv):
    if not argv:
        print("Usage: loop_cli.py replay <run_id> [--results-dir PATH]", file=sys.stderr)
        return 2

    run_id = argv[0]
    results_dir = _parse_flag(argv, "--results-dir")

    stored = None
    for path in list_results(results_dir=results_dir):
        data = read_result(path)
        if data["run_id"] == run_id:
            stored = data
            break

    if stored is None:
        print(f"replay: no result found for run_id {run_id!r}", file=sys.stderr)
        return 1

    if stored.get("prompt") is None or stored.get("definition_path") is None:
        print("replay: no prompt recorded for this run — showing inspect output instead")
        _print_run_detail(stored)
        return 0

    definition = LoopDefinition.from_yaml(stored["definition_path"])
    cwd = str(Path(stored["definition_path"]).resolve().parent)
    verifiers = build_verifiers(definition.verifiers, cwd=cwd)
    agent = get_agent(definition.agent.provider)
    prompt = stored["prompt"]

    def agent_fn(context):
        agent_result = agent.run(
            prompt, context, cwd=cwd, timeout_seconds=definition.stop_conditions.max_runtime_minutes * 60,
            disallowed_tools=_DEFAULT_DISALLOWED_TOOLS,
        )
        if agent_result.status != "success":
            print(f"agent invocation {agent_result.status}: {agent_result.output[-800:]}", file=sys.stderr)
            raise RuntimeError(agent_result.output[-800:])
        return {"cost_usd": agent_result.estimated_cost_usd}

    new_run_id = f"run_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    new_loop_id = f"loop_{uuid.uuid4().hex}"
    on_iteration = _make_progress_writer(results_dir)
    on_iteration(new_run_id, new_loop_id, definition.name, [])  # initial running snapshot

    runtime = LoopRuntime(agent_fn=agent_fn, verifiers=verifiers, on_iteration=on_iteration)

    try:
        result = runtime.start(definition, run_id=new_run_id, loop_id=new_loop_id)
    except ValueError as exc:
        print(f"replay: policy violation, refusing to start: {exc}", file=sys.stderr)
        return 1

    result.prompt = prompt
    result.definition_path = stored["definition_path"]

    path = write_result(result, results_dir=results_dir)
    print(f"replayed run_id: {run_id}")
    print(f"new run_id: {result.run_id}")
    print(f"final_state: {result.final_state.value}")
    print(f"stop_reason: {result.stop_reason}")
    print(f"Result written to {path}")

    return 0 if result.final_state == LoopState.COMPLETED else 1


def _print_run_detail(data):
    print(f"Run {data['run_id']} ({data['definition_name']})")
    print(f"Final state: {data['final_state']}  Stop reason: {data['stop_reason']}")
    for iteration in data["iterations"]:
        print(f"  Iteration {iteration['iteration']}: {iteration['state']}")
        for verification in iteration["verification_results"]:
            mark = "PASS" if verification["passed"] else "FAIL"
            print(f"    [{mark}] {verification['name']}")


def _cmd_cost(argv):
    results_dir = _parse_flag(argv, "--results-dir")
    summary = summarize_run_costs(results_dir=results_dir)

    print("Loop Cost Report")
    print()
    print(f"Runs                  {summary['total_runs']}")
    print(f"Estimated Cost       ${summary['total_cost_usd']:.2f}")
    if summary["cost_per_run_usd"] is not None:
        print(f"Cost / Run           ${summary['cost_per_run_usd']:.2f}")
    return 0


def _cmd_doctor(argv):
    if not argv:
        print("Usage: loop_cli.py doctor <path/to/loop.yaml>", file=sys.stderr)
        return 2

    definition = LoopDefinition.from_yaml(argv[0])
    report = audit_definition(definition)

    print(f"Loop Health: {report.score}/100")
    print()
    issues = sorted(
        (c for c in report.checks if c.status != CheckStatus.PASS),
        key=lambda c: c.status != CheckStatus.FAIL,
    )
    if not issues:
        print("No issues found.")
    else:
        for i, check in enumerate(issues[:3], start=1):
            print(f"{i}. [{check.status.value}] {check.name}: {check.detail}")
    return 0


def _cmd_eval(argv):
    cases_dir = Path(argv[0]) if argv else DEFAULT_EVAL_CASES_DIR
    if not cases_dir.is_dir():
        print(f"eval: no such directory {cases_dir}", file=sys.stderr)
        return 2

    outcomes = run_all(cases_dir)
    if not outcomes:
        print(f"eval: no case files found in {cases_dir}", file=sys.stderr)
        return 2

    for outcome in outcomes:
        status = "PASS" if outcome.passed else "FAIL"
        print(f"{status}  {outcome.case_name}")
        if not outcome.passed:
            print(f"      {outcome.detail}")

    passed_count = sum(1 for o in outcomes if o.passed)
    print()
    print(f"{passed_count}/{len(outcomes)} cases passed")

    return 0 if passed_count == len(outcomes) else 1


_COMMANDS = {
    "init": _cmd_init,
    "validate": _cmd_validate,
    "audit": _cmd_audit,
    "run": _cmd_run,
    "status": _cmd_status,
    "inspect": _cmd_inspect,
    "cost": _cmd_cost,
    "doctor": _cmd_doctor,
    "eval": _cmd_eval,
    "replay": _cmd_replay,
}


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] not in _COMMANDS:
        print(f"Usage: loop_cli.py <{'|'.join(_COMMANDS)}> ...", file=sys.stderr)
        return 2
    return _COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
