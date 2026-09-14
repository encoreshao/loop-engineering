# Loop X Engineering

![CI](https://github.com/encoreshao/loop-engineering/actions/workflows/ci.yml/badge.svg)
![License](https://img.shields.io/github/license/encoreshao/loop-engineering)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![Platform](https://img.shields.io/badge/platform-macOS-lightgrey)
![Dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-green)
![Shell](https://img.shields.io/badge/shell-bash-4EAA25)

Loop X Engineering's mission is to give you back the time issue triage
eats: a standing, unattended teammate that works your GitLab queue every
weekday so nothing assigned to you sits untouched — shipping fixes,
answering questions, or flagging what genuinely needs your judgment — and
your attention goes only where it actually matters. A local web dashboard
lets you watch it work, review everything it's done, and configure it all
by hand — no editing JSON.

It's built to be safe to leave running unattended: it never merges its own
merge requests, never assigns itself new issues, and only ever touches the
projects you've explicitly told it about.

## Table of contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Directory layout](#directory-layout)
- [Configuration](#configuration)
- [Running it](#running-it)
- [The dashboard](#the-dashboard)
- [Scripts reference](#scripts-reference)
- [Safety boundaries](#safety-boundaries)
- [Testing](#testing)
- [Project docs](#project-docs)
- [License](#license)



## How it works

Each scheduled run (`run-loop-now.sh gitlab-loop`):

1. Lists every open GitLab issue assigned to your configured username, across every project alias in your config.
2. Processes them **one at a time, never in parallel**, following the step-by-step decision procedure in [`LOOPX_INSTRUCTIONS.md`](https://github.com/encoreshao/loop-engineering/blob/main/LOOPX_INSTRUCTIONS.md).
3. For each issue, does exactly one of:
  - **Fix it** — in an isolated git worktree, on a `loop/issue-<iid>` branch, only opening a merge request once the project's own lint/test commands pass.
  - **Answer it** — post a GitLab comment when the ask needs no code change (a question, a status check).
  - **Escalate it** — post a GitLab comment asking for clarification when the ask is ambiguous, or when verification fails.
4. Sends a Slack message per issue plus one end-of-run digest (every run, even mornings with nothing assigned).
5. Updates [`PROGRESS.md`](https://github.com/encoreshao/loop-engineering/blob/main/PROGRESS.md) and `outputs/daily-review.md` so the next run — and you — know what happened.

Reusable, cross-run lessons (fix patterns, gotchas) get recorded per issue as markdown task-memory files via `bin/memory_store.py` (entries recorded before this format existed are still read via `bin/project_memory.py`), so later runs start smarter than the last.

A second, independent loop (`run-loop-now.sh topic-loop`) watches arbitrary topics on the wider web instead of GitLab — see [`docs/tasks/topic-monitor-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/topic-monitor-loop.md).

## Requirements

- macOS (the schedule and the dashboard both run as `launchd` agents)
- Python 3.12+ — this repo's own code is **stdlib-only**, no `pip install` needed to run it
- `git` 2.42+ (worktrees, push-options)
- A GitLab account + personal access token for the projects you want tracked
- (optional) A Slack incoming webhook, for run notifications
- The `[gitlab-config](https://github.com/encoreshao/encore-skills/tree/main/skills/gitlab-config)` skill from `[encore-skills](https://github.com/encoreshao/encore-skills)` — this loop's one external dependency, deployed to `~/.encore-skills` by `setup.sh`. Check it's actually present any time from the dashboard's **Skills** page.
- `pytest` — dev-only, for running this repo's own test suite



## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/install.sh | bash
```

Clones this repo into `~/.loop-engineering` (pass `--dir <path>` for somewhere else) and runs `bin/scripts/setup.sh`, which installs the `gitlab-config` skill and scaffolds `projects.json`/`topics.json` from their templates. It then sets up the local nginx reverse proxy and starts the dashboard as an always-on `launchd` agent, so this one command ends with the dashboard actually reachable and running — pass `--skip-nginx` and/or `--skip-launchd-daemons` to opt out of either. (The scheduled GitLab loop and topic monitor are *not* auto-started, since they'd act on `projects.json`/`topics.json` before you've filled them in — start those yourself, once configured, from the dashboard's **Daemons** page.) Re-running the same command later just pulls the latest `main` instead of re-cloning.

Already installed and just want to update? Add `--upgrade`:

```bash
curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/install.sh | bash -s -- --upgrade
```

Same steps as above, but fails fast if nothing's installed at `--dir` yet instead of silently cloning fresh, and refreshes every one of this project's launchd agents that's currently loaded — not just the dashboard. The dashboard (an always-on server) gets an actual restart (`launchctl kickstart -k`), unlike a bare `launchctl load`, which is a no-op on an already-running agent. `com.hermes.loop-engineering` — the single scheduler that runs every loop registered in `loops.json`, if you've enabled it from the Daemons page — just gets its registration reloaded (`unload` + `load -w`) — never kickstarted, since that would trigger a real, out-of-schedule run against live GitLab/Slack right now rather than waiting for the scheduler's own next poll. `--upgrade` also migrates a pre-existing rendered plist left over from before the unified scheduler (one that still points at the now-deleted `run-loop.sh`) and removes the now-orphaned `com.hermes.loop-engineering-topic-monitor` daemon if it's still installed from before that migration.

Prefer to see the clone happen yourself first?

```bash
git clone https://github.com/encoreshao/loop-engineering.git
cd loop-engineering
bin/scripts/setup.sh
```

Already have the skill installed and just want the config scaffolds?

```bash
bin/scripts/setup.sh --skip-skills-install
```

Once it's done, open the dashboard's **Skills** page to confirm everything needed is actually installed — it checks live, no guesswork.

**Working in Claude Code already?** Paste this instead of running the commands yourself:

> Clone and set up [https://github.com/encoreshao/loop-engineering](https://github.com/encoreshao/loop-engineering) for me: run its online installer
> (`curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/install.sh | bash`),
> then help me fill in `~/.loop-engineering/projects.json` with my own GitLab project(s), and `~/.gitlab/config.json` with my GitLab token.



### Uninstalling

```bash
bin/scripts/uninstall.sh                 # or: curl -fsSL .../uninstall.sh | bash
```

Unloads and removes this repo's `launchd` agents, reverses `setup-nginx.sh` if you ran it, and removes the whole `~/.loop-engineering` folder — code, config, and run history together — pass `--keep-config` to leave it all in place instead (e.g. you're about to reinstall). Safe to re-run.

## Directory layout

Using the default install path, everything lands under one folder:

```
~/.loop-engineering/            # install.sh's clone target
├── bin/, docs/, tests/, ...    # this repo's own code (tracked in git)
├── projects.json                # your config: GitLab projects to track  ┐
├── topics.json                  # your config: topics to monitor         │
├── loops.json                   # your config: scheduled-loop registry   ├─ gitignored, yours
├── instructions.md              # your free-text instructions            │
├── ai_cli.json                  # your config: Claude Code vs Codex CLI   ┘
├── loop_scheduler_state.json    # managed automatically, not hand-edited
├── PROGRESS.md                  # live run state, updated every run
├── outputs/                     # ← generated docs & run history live here (gitignored)
│   ├── daily-review.md          #   latest GitLab-issue-loop report
│   ├── messages.json             #   Activity page message thread
│   ├── status.json               #   GitLab loop's current/last run status
│   ├── status/<loop_name>.json   #   every other registered loop's current/last run status
│   └── history/<date>.{md,log}   #   every past run's report + log
└── worktrees/                    # ← per-issue git worktrees for tracked projects (gitignored)
    └── <project>-issue-<iid>/    #   that project's own checkout, on branch loop/issue-<iid>
```

`projects.json`, `topics.json`, `loops.json`, `instructions.md`, and `ai_cli.json` always resolve to `~/.loop-engineering/…` regardless of where you clone the code — they only end up *inside* the repo folder above because `install.sh`'s default clone target happens to be that same path. If you clone somewhere else by hand, those five files still live at `~/.loop-engineering/`, separate from the code. `projects.json`'s scaffolded `worktree_root` defaults to `~/.loop-engineering/worktrees` too, for the same reason.

Two more config files live outside this tree entirely, editable from the dashboard's **GitLab** and **Notifications** pages instead of by hand: `~/.gitlab/config.json` and `~/.slack/config.json`.

## Configuration


| File                                  | Holds                                                                                                                                                                                                                   | Managed via                                                                                                                                                                   |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `~/.loop-engineering/projects.json`   | Which projects to track, their local checkout paths, target branch, install/lint/test commands, your GitLab username, and the worktree scratch directory (`worktree_root`, defaults to `~/.loop-engineering/worktrees`) | Dashboard **GitLab Settings** page's "Tracked Projects" section, or copy [`config/projects.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/projects.json.template) by hand, or let `bin/scripts/setup.sh` do it |
| ↳ per-project `instance` (optional)   | Overrides the top-level `gitlab_instance` for one project — set this when your projects span more than one GitLab instance. Falls back to `gitlab_instance` when omitted.                                               | Same file, per project entry — see the template's `harbor` example                                                                                                            |
| `~/.loop-engineering/topics.json`     | Which topics to monitor and what counts as notable for each one (topic monitor loop only)                                                                                                                               | Copy [`config/topics.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/topics.json.template) by hand, or let `bin/scripts/setup.sh` do it                                                                |
| `~/.loop-engineering/loops.json`      | The registry of scheduled loops: each entry's name, schedule (weekdays/hour/minute), entry point module, timeout, and per-loop knobs — read by `bin/loops_config.py`, polled by `bin/loop_scheduler.py`                 | Copy [`config/loops.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/loops.json.template) by hand, or let `bin/scripts/setup.sh` do it                                                                  |
| `~/.loop-engineering/loop_scheduler_state.json` | Per-loop last-attempted date, so the scheduler never runs the same loop twice in a day — not something you hand-edit                                                                                            | Written automatically by `bin/loop_scheduler.py`; seeded with today's date for every registered loop by `bin/scripts/setup.sh` so enabling the scheduler doesn't fire an immediate run |
| `~/.loop-engineering/instructions.md` | Your own free-text instructions, read by the loop at the start of every run                                                                                                                                             | Dashboard **Settings** page's Instructions tab                                                                                                                               |
| `~/.loop-engineering/ai_cli.json`     | Which AI CLI (Claude Code or Codex CLI) `run-loop-now.sh` invokes for every registered loop; defaults to `claude`                                                                                                        | Dashboard **Settings** page's AI CLI tab, or let `bin/scripts/setup.sh` do it                                                                                                                |
| `~/.gitlab/config.json`               | GitLab instance URLs, tokens, and project-alias → project-ID mappings (read by the `gitlab-config` skill)                                                                                                               | Dashboard **GitLab Settings** page                                                                                                                                                     |
| `~/.slack/config.json`                | Your Slack incoming webhook URL (and any per-bundle overrides)                                                                                                                                                          | Dashboard **Settings** page's Notifications tab (the default webhook) / **GitLab Settings** page's Access bundles section (per-bundle overrides)                                                              |


`bin/loop_config.py` is the only code that reads `projects.json` — use it to sanity-check your config from a terminal:

```bash
python3 bin/loop_config.py aliases                # every configured project alias
python3 bin/loop_config.py project <alias>         # that alias's full config, incl. resolved GitLab instance
python3 bin/loop_config.py assignee                # the GitLab username being tracked
python3 bin/loop_config.py worktree-root           # where per-issue worktrees get created
```

If `~/.loop-engineering/projects.json` doesn't exist yet, every script that needs it fails fast with a message telling you to run `bin/scripts/setup.sh` — nothing silently guesses paths.

**Access bundles** — per-project token/webhook overrides

Most projects just use their GitLab instance's default token. An **access bundle** is a named override — its own `{instance, token}` pair, plus an optional Slack webhook — for the rare project whose default instance token doesn't have the access that project needs.

Manage bundles from the dashboard's **GitLab** page, in their own "Access bundles" section:

- **Add a bundle**: name it, pick which GitLab instance it authenticates against, paste its token, and optionally a Slack webhook URL.
- **Assign a bundle to a project**: edit the project alias's row and pick the bundle from the **Bundle** dropdown — defaults to "(use instance default)".
- A bundle can't be deleted, and its instance can't be changed, while any project alias still points at it.
- Deleting a bundle also clears its Slack webhook override, if it had one.

Bundles live in `~/.gitlab/config.json`'s `bundles` key and, if a webhook override is set, `~/.slack/config.json`'s `bundle_webhooks` key — joined only by the bundle's name.

## Running it

**Manually**, once, to see it work before trusting it with a schedule:

```bash
bash run-loop-now.sh gitlab-loop   # the daily GitLab issue loop
bash run-loop-now.sh topic-loop    # the topic monitor loop
```

Both log to `outputs/history/`, and both also append every `claude` CLI invocation's output to `logs/loop-engineering.log` (viewable on the dashboard's **Logs** page); you can also trigger the GitLab loop from the dashboard's **Run now** button (Overview page) without a terminal.

**On a schedule**, via `launchd` — install the two agents under [`launchd/`](https://github.com/encoreshao/loop-engineering/tree/main/launchd), most easily with a click each from the dashboard's **Daemons** page (which also shows whether each is currently loaded and its PID), or by hand:

```bash
cp launchd/com.hermes.loop-engineering*.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.hermes.loop-engineering.plist
launchctl load -w ~/Library/LaunchAgents/com.hermes.loop-engineering-dashboard.plist
```


| Agent                                   | Runs                                                                                                                                             |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------|
| `com.hermes.loop-engineering`           | The single scheduler poll loop (`bin/loop_scheduler.py`), every 15 minutes (`StartInterval`) — runs whichever loop(s) registered in `~/.loop-engineering/loops.json` are due, via `run-loop-now.sh` |
| `com.hermes.loop-engineering-dashboard` | The web dashboard, always-on (`RunAtLoad` + `KeepAlive`)                                                                                          |


Which loops run and on what schedule is config, not code — edit `~/.loop-engineering/loops.json` (see [`config/loops.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/loops.json.template)) to add a loop or change when it's due; adding a third loop needs a new `loops.json` entry, not a new plist. The **Daemons** page's per-agent schedule editor only applies to a plist's own `StartCalendarInterval`, which `com.hermes.loop-engineering` no longer has (it polls every 15 minutes on a fixed `StartInterval` and defers to `loops.json` for which loop is actually due) — editing a loop's own schedule is a hand-edit of `loops.json` for now.

## The dashboard

A localhost-only, dependency-free (stdlib Python, no JS framework) web UI, served by `bin/web/dashboard_server.py`. Running it directly for local dev (no arguments) uses its own default port, `8420`. `bin/scripts/install.sh` picks a random port in `48420`-`48620` the first time it installs the always-on `launchd` agent (overridable with `--port`, and never re-picked on a later `--upgrade`) — check `launchd/com.hermes.loop-engineering-dashboard.plist` for the port an existing install is actually running on.


| Page              | Shows                                                                                                                                                                           |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Overview**      | Current/last run status, a live progress indicator, and the Run now button                                                                                                      |
| **Activity**      | A message thread with the loop, plus its own live progress indicator. Paste a GitLab issue link here to have the loop work on that one issue immediately, regardless of who it's assigned to.                                                                                                    |
| **Live GitLab**   | Your currently assigned issues and open MRs, fetched live                                                                                                                       |
| **Topic Monitor** | Status and saved briefings for every configured topic                                                                                                                           |
| **Logs**          | The tail of `logs/loop-engineering.log` - every `claude` CLI invocation's output, across the GitLab loop, the topic monitor loop, and this dashboard's own chat assistant       |
| **Loop Runs**     | Every run recorded under `outputs/loop-runs/` (one per issue or topic processed), most recent first — read-only; an overview strip shows total runs, success/escalation rate, average cost, and the experimental Loop Efficiency Score                                                                |
| **Run History**   | Every past run's review report, newest first                                                                                                                                    |
| **Analytics**     | The loop's performance over a selectable day window: a Loop Health score, outcomes, quality, risk & classification, failure breakdown, and learning trends                     |
| **Memory**        | Cross-run lessons recorded per project, one markdown file per GitLab issue, plus anything recorded before this format existed (shown under "Legacy learnings")                 |
| **Cost**          | AI usage cost — the GitLab issue loop's own windowed cost, and total cost across every run under `outputs/loop-runs/`                                                           |
| **Audit**         | A score and pass/fail checks for each loop definition                                                                                                                           |
| **Budget**        | Each recorded run's last-known budget status, plus rollups by loop definition and by day/week/month                                                                            |
| **Daemons**       | Load state, an editable schedule, and enable/disable for every `launchd` agent, plus a Registered Loops breakdown of every loop the unified scheduler runs (its own schedule and last-run status, read from `loops.json`) |
| **Skills**        | Every external skill this loop depends on, and whether it's actually installed                                                                                                  |
| **GitLab Settings** | Manage `~/.gitlab/config.json` (instances, project aliases, access bundles) and `~/.loop-engineering/projects.json` (tracked projects, loop settings) without hand-editing JSON |
| **Topic Settings** | Add, edit, and delete monitored topics — split out of Topic Monitor so configuration doesn't clutter that page's live status view                                             |
| **Settings**      | Notifications (manage `~/.slack/config.json`'s default webhook), AI CLI (choose Claude Code or Codex CLI, with a live installed/not-found check for each), Appearance (color mode, accent theme, auto-refresh interval — saved to this browser's `localStorage`), and Instructions (your own free-text instructions, read by the loop at the start of every run) — clustered as tabs on one page |
| **README**        | This file, rendered in-app with a jump-to-section quicknav                                                                                                                      |


**Optional: a friendly hostname via nginx**

By default the dashboard is only reachable at `http://127.0.0.1:<port>` (see above for how `<port>` is chosen). `bin/scripts/setup-nginx.sh` sets up a local nginx reverse proxy so it's reachable at `http://loop.x/` (port 80) instead — installs nginx via Homebrew if needed, writes the proxy config, adds `loop.x` to `/etc/hosts`, and starts nginx as a system service. `install.sh` already passes it the installed port automatically; idempotent, safe to re-run standalone too:

```bash
bin/scripts/setup-nginx.sh
# or, with no clone at all:
curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/setup-nginx.sh | bash
```

Writing `/etc/hosts` and starting the nginx service both need `sudo` — macOS will prompt for your password at those two steps. Pass `--domain`/`--port` to use something other than `loop.x`/`8420`.

## Scripts reference

Expand for the full list


| Script                              | Purpose                                                                                                                                                                                                                              |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `run-loop-now.sh`                   | Generic entry point for one registered loop's run (looked up from `~/.loop-engineering/loops.json` via `bin/loops_config.py`) — logs to `outputs/history/`, notifies Slack on failure. Invoked by `bin/loop_scheduler.py` (on schedule) or the dashboard (on demand) |
| `bin/loop_scheduler.py`             | The single launchd-scheduled poll loop: reads `~/.loop-engineering/loops.json` and runs whichever registered loop(s) are due, via `run-loop-now.sh`                                                                                  |
| `bin/loops_config.py`               | Reads `~/.loop-engineering/loops.json` — the registry of scheduled loops (name, schedule, entry point); no write path today, hand-edit the file (or copy the template) to change it                                                 |
| `bin/gitlab_loop_runner.py`         | The per-issue orchestrator `run-loop-now.sh` delegates to when running `gitlab-loop`: discovers assigned issues, runs each one through its own `LoopRuntime` (one `LoopResult` per issue under `outputs/loop-runs/`), owns the `claude -p`/`codex exec` invocation and its `--allowedTools`/`--disallowedTools` safety boundary, then runs one unconditional end-of-run wrap-up for the whole batch |
| `bin/scripts/build_run_prompt.sh`   | Builds the prompt string `bin/gitlab_loop_runner.py` hands to the AI CLI — a single-issue prompt for `<alias> <issue_iid>` (the dashboard's Activity-chat scoped run), `--batch-issue <alias> <issue_iid>` for one issue inside a scheduled batch (no end-of-run), and `--batch-end-of-run` for the batch's one digest/daily-review wrap-up |
| `bin/web/dashboard_server.py`       | The web dashboard; also a small CLI (`write-status`, `write-skills-install-status`, `read-messages`, `add-message`, `chat-tool`) used by `run-loop-now.sh`, `bin/loop_scheduler.py`, the dashboard's own actions, and the Activity page's embedded chat assistant |
| `bin/loop_config.py`                | Reads `~/.loop-engineering/projects.json`                                                                                                                                                                                            |
| `bin/list_assigned_issues.py`       | Lists open GitLab issues assigned to the configured user across configured projects                                                                                                                                                  |
| `bin/track_new_comments.py`         | Detects which notes on a cached issue are new since the loop last looked                                                                                                                                                             |
| `bin/project_memory.py`             | Reads (legacy) durable per-project lessons learned, stored inline in the GitLab cache                                                                                                                                                |
| `bin/memory_store.py`               | Reads/records durable per-issue task memory as markdown files (one per issue, plus a per-project MEMORY.md index)                                                                                                                    |
| `bin/ai_cli_config.py`              | Reads/writes `~/.loop-engineering/ai_cli.json` — which AI CLI (`claude` or `codex`) `run-loop-now.sh` invokes for every registered loop                                                                                              |
| `bin/topic_monitor_runner.py`       | The per-topic orchestrator `run-loop-now.sh` delegates to when running `topic-loop`: runs each configured topic through its own `LoopRuntime` (one `LoopResult` per topic under `outputs/loop-runs/`), owns the `claude -p`/`codex exec` invocation and its safety boundary — same role for the topic monitor loop as `bin/gitlab_loop_runner.py` plays for the GitLab loop |
| `bin/scripts/build_topic_prompt.sh` | Builds the prompt string for one configured topic, same role as `build_run_prompt.sh` above; kept as a documented manual escape hatch even though `topic_monitor_runner.py` no longer calls it                                       |
| `bin/topic_config.py`               | Reads `~/.loop-engineering/topics.json`                                                                                                                                                                                              |
| `bin/topic_seen.py`                 | Rolling 7-day dedup window per topic, so briefings don't repeat the same story two days running                                                                                                                                      |
| `bin/slack_notify.py`               | Posts a message to the configured Slack incoming webhook                                                                                                                                                                             |
| `bin/scripts/new_worktree.sh`       | Creates (or reuses) an isolated git worktree on a `loop/issue-<iid>` branch                                                                                                                                                          |
| `bin/scripts/open_merge_request.sh` | Pushes an issue branch and opens its MR — refuses anything not named `loop/issue-*`                                                                                                                                                  |
| `bin/scripts/install.sh`            | Online installer — clones (or updates) this repo, then runs `setup.sh` (forwarding `--config-path`/`--topics-config-path`/`--ai-cli-config-path`/`--loops-config-path`/`--state-path` through to it); `--upgrade` for an existing install, refreshing every currently-loaded launchd agent (dashboard restarted, scheduler daemon just re-registered) so they pick up the new code; also migrates a stale pre-unified-scheduler `com.hermes.loop-engineering.plist` in place and removes the old, now-orphaned `com.hermes.loop-engineering-topic-monitor` daemon if still installed from before the unified scheduler; safe to pipe from `curl`                                          |
| `bin/scripts/setup.sh`              | One-command install: the `gitlab-config` skill + the `projects.json`/`topics.json`/`ai_cli.json`/`loops.json` scaffolds, plus a `loop_scheduler_state.json` seeded with today's date for every registered loop so enabling the scheduler right after install doesn't fire an immediate run |
| `bin/scripts/setup-nginx.sh`        | Optional local nginx reverse proxy (`http://loop.x/` → the dashboard)                                                                                                                                                            |
| `bin/scripts/uninstall.sh`          | Reverses `setup.sh`/`setup-nginx.sh`/`install.sh`; safe to pipe from `curl`                                                                                                                                                          |




## Safety boundaries

Fixed, and does not loosen with time or repeated success (see [`docs/tasks/gitlab-issue-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/gitlab-issue-loop.md)):

- **Never merges a merge request.** The loop's job ends at "MR opened, verification passing" — merging is always a manual human step.
- Every code change happens in its own git worktree, on a `loop/issue-<iid>` branch, never on the target branch directly.
- An MR only opens if the project's own configured `test_cmd`/`lint_cmd` pass, and the diff only touches files relevant to the issue.
- No arbitrary shell, no dependency upgrades, no reading `.env`/credentials/SSH keys — only the command allow-list in `LOOPX_INSTRUCTIONS.md`.
- Issues are processed one at a time, sequentially, never in parallel.
- A verification failure on the same issue is never retried within a run — it escalates via a GitLab comment instead.



## Testing

```bash
python3 -m pytest tests/
```

Every script under `bin/` (Python or shell, whichever folder it lives in) has a matching `tests/test_*.py`, exercised against real subprocesses/tmp dirs rather than mocks wherever practical (see `tests/test_new_worktree.py` for an example using a real local git repo).

## Project docs


| Doc                                                                    | What it's for                                                                                          |
| ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| [`docs/architecture.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/architecture.md)                         | The V2 runtime architecture: `LoopDefinition`/`LoopState`/`LoopRuntime`, verification/budget/policy, observability, and the CLI — the map, not either loop's own spec |
| [`TASK.md`](https://github.com/encoreshao/loop-engineering/blob/main/TASK.md)                                                   | Index of every scheduled task this repo runs, each pointing at its own spec under `docs/tasks/`        |
| [`docs/tasks/gitlab-issue-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/gitlab-issue-loop.md)   | The GitLab issue loop's human-facing spec: goal, scope, safety boundaries                              |
| [`docs/tasks/topic-monitor-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/topic-monitor-loop.md) | The topic monitor loop's human-facing spec: goal, scope, safety boundaries                             |
| [`LOOPX_INSTRUCTIONS.md`](https://github.com/encoreshao/loop-engineering/blob/main/LOOPX_INSTRUCTIONS.md)                         | The step-by-step procedure the GitLab issue loop itself follows each run                               |
| [`TOPIC_MONITOR_INSTRUCTIONS.md`](https://github.com/encoreshao/loop-engineering/blob/main/TOPIC_MONITOR_INSTRUCTIONS.md)       | The step-by-step procedure the topic monitor loop itself follows each run                              |
| [`PROGRESS.md`](https://github.com/encoreshao/loop-engineering/blob/main/PROGRESS.md)                                           | Live state the loop reads and updates every run — last run's summary, open escalations, decisions made |
| [`docs/troubleshooting/crash-looping-launchd-agent.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/troubleshooting/crash-looping-launchd-agent.md) | Diagnose and fix a `com.hermes.loop-engineering*` launchd agent stuck crash-looping and flooding its log |




## License

[MIT](https://github.com/encoreshao/loop-engineering/blob/main/LICENSE) — see the [`LICENSE`](https://github.com/encoreshao/loop-engineering/blob/main/LICENSE) file.