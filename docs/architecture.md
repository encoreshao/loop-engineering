# Architecture

This is the consolidated V2 architecture reference for this repo — the
pieces described in `~/Downloads/LOOP-ENGINEERING-V2-TECH-PLAN.md` and how
they actually landed in code, in one place. The ~30 design docs under
`docs/superpowers/specs/` remain the source of record for *why* each piece
was built the way it was (constraints, rejected alternatives, review
findings); this file is the map, not a replacement for them — each section
below links to its own spec rather than re-explaining it.

Read [`README.md`](../README.md) first for what this project *does*
(schedule, dashboard, safety boundaries). Read
[`LOOPX_INSTRUCTIONS.md`](../LOOPX_INSTRUCTIONS.md) for the GitLab issue
loop's own step-by-step spec — this file is about the runtime underneath
both scheduled loops, not either loop's own decision logic.

## 1. Vision

Loop Engineering is not another agent framework. It's the layer around
one: it doesn't decide *how* an agent solves a task, it decides how that
agent's work is triggered, bounded, verified, stopped, retried, escalated,
and measured.

```text
Trigger → Goal → Context → Agent → Actions → Verification → Evaluation
                                                                  │
                                              ┌───────────────────┼───────────────────┐
                                              ↓                   ↓                   ↓
                                           SUCCESS              RETRY             ESCALATE
                                              ↓                   ↓                   ↓
                                          COMPLETE          next iteration          HUMAN

                                   Memory + Metrics accumulate alongside every run
```

## 2. Two run-history systems, not yet unified

This is the single most important thing to know before reading further:
**there are two independent, non-overlapping histories of what this
project has done**, and most of the code below belongs to one or the
other.

- **The GitLab issue loop's own event log** — `outputs/events/*.jsonl`,
  written by `bin/events.py`'s CLI (`bin/events.py emit --type ...`),
  called directly from `run-loop.sh` (`run.started`/`run.failed`) and by
  the agent itself per `LOOPX_INSTRUCTIONS.md` (`issue.started`,
  `issue.classified`, `verification.started/passed/failed`,
  `issue.completed`/`issue.escalated`, `memory.created`/`memory.reused`).
  `bin/metrics.py`, `bin/cost.py`, `bin/health.py`, `bin/learning.py`, and
  `bin/risk.py` all read *only* this log. This is what backs the
  **Analytics**, **Cost**, and **Memory** dashboard pages.
- **`LoopRuntime`'s persisted results** — `outputs/loop-runs/<run_id>/result.json`,
  written by `bin/loop_serialize.py`, one file per `LoopRuntime.start()`
  call. `bin/loop_serialize.py` and `bin/loop_budget.py`'s
  `summarize_*` functions read *only* this. This is what backs the
  **Loop Runs**, **Budget**, and **Audit** dashboard pages.

The topic monitor loop only ever wrote to the second system (it was built
directly on `LoopRuntime` from the start). The GitLab issue loop writes to
*both*: its own event log (for Analytics/Cost/Memory, which predate
`LoopRuntime`) and, since `bin/gitlab_loop_runner.py` wired it up, a
`LoopRuntime` result per issue (for Loop Runs/Budget/Audit). Nothing here
merges the two — a fact worth knowing before assuming a dashboard number
comes from "the" run history.

## 3. Module map

Every runtime module under `bin/` — one line each, pulled from its own
docstring so this stays honest as the code changes. Flat `bin/*.py`,
not the plan's original `bin/runtime/`, `bin/verifiers/`, etc.
subdirectories — see §9.

| Module | Responsibility |
|---|---|
| `loop_definition.py` | `LoopDefinition` dataclasses + YAML loader/validator |
| `loop_state.py` | `LoopState` enum + its explicit transition table (raises on an invalid transition, not silent) |
| `loop_runtime.py` | `LoopRuntime` — the orchestrator; owns iteration, budget, retry, policy, events |
| `loop_verifiers.py` | `Verifier` interface + `CommandVerifier`/`DiffVerifier` |
| `loop_budget.py` | `BudgetController` (per-iteration OK/WARNING/EXCEEDED) + the Budget dashboard's rollup functions |
| `loop_policy.py` | `RiskLevel` (L0–L3) classification + `PolicyEngine` — which *action types* need a human gate |
| `loop_progress.py` | `ProgressDetector` — same failing-verifier signature twice in a row? |
| `risk.py` | Deterministic keyword risk scorer for *issue text* (advisory only) — distinct from `loop_policy.py`'s action-type risk |
| `loop_serialize.py` | `LoopResult` JSON persistence (`result.json`) + `summarize_results()` |
| `loop_audit.py` | `loop audit` — scores a `LoopDefinition`'s readiness (PASS/WARN/FAIL per check + weighted score) |
| `loop_eval.py` | Evaluation dataset harness — drives a real `LoopRuntime` with a scripted agent/verifiers |
| `loop_cli.py` | The `loop` CLI: `init`/`validate`/`run`/`status`/`inspect`/`audit`/`cost`/`doctor`/`replay`/`eval` |
| `agents/base.py`, `agents/claude.py`, `agents/codex.py` | `Agent` ABC + `AgentResult` + provider adapters |
| `events.py` | Append-only JSONL event log (the GitLab-issue-loop world, §2) |
| `metrics.py`, `cost.py`, `health.py`, `learning.py` | Pure report builders over the event log (the GitLab-issue-loop world, §2) |
| `gitlab_loop_runner.py` | Wires the real GitLab issue loop through `LoopRuntime`, one issue at a time |
| `topic_monitor_runner.py` | Wires the real Topic Monitor loop through `LoopRuntime`, one topic at a time |

## 4. `LoopDefinition`

A `LoopDefinition` (`bin/loop_definition.py`) is loaded from a `loop.yaml`
(see `loops/gitlab-issue/loop.yaml`, `loops/topic-monitor/loop.yaml`, or
any of the 8 generic templates under `templates/`):

```text
LoopDefinition
├── trigger: {type, schedule}
├── goal: {type}
├── agent: {provider, model}
├── context: {sources: [...]}
├── actions: [...]
├── verification: {required: [...]}
├── stop_conditions: {max_iterations, max_runtime_minutes, max_cost_usd, no_progress_iterations}
├── human_gates: [...]
├── retry: {enabled, max_attempts}
├── verifiers: [...]                    # loop_verifiers.build_verifiers() input
├── permissions: {credentials_read}
└── observability_enabled: bool
```

Every field has a validated default via `_STOP_CONDITION_DEFAULTS`/
`_RETRY_DEFAULTS` — there is no such thing as an unbounded loop by
omission. See
[`2026-09-06-loop-runtime-foundation-design.md`](superpowers/specs/2026-09-06-loop-runtime-foundation-design.md).

### Risk levels (`loop_policy.py`)

Every *action type* a loop can take (not the loop definition's own
config — the actual actions like `modify_code`, `create_merge_request`)
gets classified:

| Level | Meaning | Example actions |
|---|---|---|
| L0 | Read-only | `inspect_issue`, `read_issue` |
| L1 | Local mutation | `modify_code`, `run_tests` |
| L2 | External change | `create_merge_request`, `post_public_comment` |
| L3 | Irreversible | `merge`, `production_deploy` |

An action not in the table defaults to **L3** — fail-safe, not
fail-open. `PolicyEngine` refuses any L3 action whose type isn't
explicitly listed in the definition's `human_gates`. See
[`2026-09-07-policy-engine-design.md`](superpowers/specs/2026-09-07-policy-engine-design.md).

## 5. `LoopState`

```text
PENDING → DISCOVERING → PLANNING → EXECUTING → VERIFYING → EVALUATING
                                        ↑                        │
                                        └────── RETRY ───────────┤
                                                                  ├→ COMPLETED
                                                                  ├→ BLOCKED → ESCALATED
                                                                  ├→ ESCALATED
                                                                  ├→ STOPPED
                                                                  └→ FAILED
```

`transition(current, target)` raises `InvalidTransition` for any pair not
in `VALID_TRANSITIONS` — "every loop must have explicit exits" (plan
§4.3) is enforced by this table, not left as a convention `LoopRuntime`
is merely trusted to follow.

## 6. `LoopRuntime`

The actual public surface is smaller than the plan's original mockup
(`start`/`execute`/`transition`/`stop`) — it collapsed to one entry point:

```python
runtime = LoopRuntime(agent_fn, verifiers, progress_detector=None,
                       policy_engine=None, events_dir=None, on_iteration=None)
result = runtime.start(definition, run_id, loop_id=None)  # -> LoopResult
```

`agent_fn` and `verifiers` are injected, not imported — `loop_runtime.py`
itself has no GitLab/Claude/Codex/Slack dependency. Each iteration:
executes the agent, runs every configured verifier, checks budget
(`loop_budget.BudgetController`), checks progress
(`loop_progress.ProgressDetector`), checks policy
(`loop_policy.PolicyEngine`) for any action needing a human gate, and
either transitions to `COMPLETED`, retries (`EXECUTING` again, subject to
`retry.max_attempts`), or exits to `BLOCKED`/`ESCALATED`/`STOPPED`/`FAILED`.
Every step emits through `bin/events.py`, best-effort (an event-log write
failure never crashes the runtime — same `|| true` philosophy as
`run-loop.sh`). See
[`2026-09-06-loop-runtime-foundation-design.md`](superpowers/specs/2026-09-06-loop-runtime-foundation-design.md).

## 7. Observability

- **Events** — `outputs/events/<UTC date>.jsonl`, one JSON object per
  line: `{event_id, run_id, iteration, type, timestamp, data}`. See §2 for
  which subsystems read which event stream.
- **Metrics** (`metrics.py`) — issue/verification/classification/failure-
  taxonomy/quality-and-autonomy metrics computed from the event log.
- **Cost** (`cost.py`) — Claude-only usage/cost extraction (`claude -p
  --output-format json`'s `total_cost_usd`); Codex is skipped, its
  success-path usage schema being unverified.
- **Health** (`health.py`) — a *partial* Loop Health score: only 4 of the
  plan's 7 weighted components have a real data source today (Retry Rate
  and Learning Effectiveness need data that doesn't exist yet; "Cost
  Efficiency" has no defined formula in the plan) — reported as partial
  rather than guessed.
- **Audit** (`loop_audit.py`) — `loop audit <loop.yaml>` scores a
  `LoopDefinition`'s readiness: goal/trigger/verification/stop_conditions/
  retry/budget/no_progress_detection/human_gates/context_strategy/
  memory_strategy/credential_boundary/observability, each PASS/WARN/FAIL,
  weighted into a 0–100 score. Same honest-degradation pattern as
  `health.py`.
- **Loop Efficiency Score** (`loop_serialize.summarize_results`) — the
  plan's experimental §17 metric: verified-successful runs / (total
  cost_usd × total duration_hours × total iterations), summed across all
  persisted `LoopRuntime` runs. Shown as one tile among several on the
  Loop Runs page, deliberately not a standalone KPI.
- **External GitLab issue verification** (`gitlab_loop_runner._external_verify_issue`)
  — independently re-runs a project's real `test_cmd`/`lint_cmd` against
  the worktree an issue's agent call actually used, recording the result
  but not (yet) changing the issue's outcome. See
  [`2026-09-13-gitlab-issue-external-verification-design.md`](superpowers/specs/2026-09-13-gitlab-issue-external-verification-design.md).
  `loop audit` doesn't know about this mechanism — it still scores
  `loops/gitlab-issue/loop.yaml`'s `verification` check as a FAIL.

## 8. CLI (`bin/loop_cli.py`)

Manual `sys.argv` subcommand dispatch (matching `events.py`'s own style,
not `argparse`):

| Command | Does |
|---|---|
| `init --template <name>` | Scaffold `.loop/loop.yaml` from one of the 10 shipped templates |
| `validate <loop.yaml>` | Check trigger/goal/verifiers/budget/human-gates are configured |
| `run <loop.yaml>` | Real `LoopRuntime.start()` invocation, via `--prompt`/`--prompt-file` (no-op agent otherwise — L0/observe, plan §32) |
| `status <run_id>` / `inspect <run_id>` | Read back a persisted `result.json` |
| `audit <loop.yaml>` | See §7 |
| `cost` | Cost report across persisted runs |
| `doctor` | Top issues, human-readable |
| `replay <run_id>` | Re-invoke a real agent using the prompt/definition path recorded by the original `run` |
| `eval [cases_dir]` | Run the evaluation harness (§ below) against `evals/cases/*.yaml` |

See [`2026-09-07-loop-cli-design.md`](superpowers/specs/2026-09-07-loop-cli-design.md).

### Evaluation harness (`loop_eval.py`, `evals/cases/`)

Drives a real, unmodified `LoopRuntime` with a `ScriptedAgent` and
`ScriptedVerifiers`, so a passing case is evidence about the runtime's
stop/verify/escalate/cost behavior, not about the harness's own logic.
Six shipped cases: `success`, `retry`, `no-progress`, `budget`,
`unsafe-action`, `ambiguous-task` — testing whether the *loop* stops,
verifies, and escalates correctly, not whether an agent can write code
(plan §29). See
[`2026-09-09-loop-eval-harness-design.md`](superpowers/specs/2026-09-09-loop-eval-harness-design.md).

## 9. How the two production loops actually plug in

```text
run-loop.sh                          run-topic-monitor-loop.sh
     │                                       │
     ↓                                       ↓
gitlab_loop_runner.py                 topic_monitor_runner.py
     │  (one issue at a time)               │  (one topic at a time)
     ↓                                       ↓
LoopRuntime(agent_fn, verifiers).start(definition, run_id=issue_run_id)
```

Each issue/topic gets its own `LoopRuntime.start()` call — the runtime
tracks and bounds that single invocation, it does not span the whole
scheduled batch. `run-loop.sh`/`run-topic-monitor-loop.sh` themselves are
unchanged as the actual `launchd`-scheduled entry points; nothing here
requires `loop run gitlab-issue` as the plan originally imagined (plan
§25/§26's "final state" was never reached — the shell scripts remain the
real trigger, calling into `LoopRuntime` one level down rather than being
replaced by it).

## 10. Deliberately not (yet) built

Documented gaps, not oversights:

- **No polymorphic `Trigger` interface** (plan §8's `bin/triggers/*.py`).
  Schedule is `launchd`, manual is the dashboard's "Run now" button or a
  pasted GitLab issue link, and neither goes through a shared `Trigger.fire()`
  abstraction.
- **No CI-enforced audit score** — `loop audit` runs in CI
  (`.github/workflows/ci.yml`) as an informational step only; a loop
  definition failing a check does not fail the build (the shipped
  `gitlab-issue` loop's own `verification` check is a known, accepted
  FAIL — see the audit design docs).
- **The Loop Efficiency Score is unvalidated** — no production data has
  been run through it yet to know if the formula in §7 is actually a
  useful signal over time.

## Where to look next

Each module above links to its own design spec above; browse all of them
under `docs/superpowers/specs/` for full rationale, rejected alternatives,
and review findings. `docs/tasks/gitlab-issue-loop.md` and
`docs/tasks/topic-monitor-loop.md` are the two loops' own behavioral
specs — not covered here, since this file is about the runtime
underneath them.
