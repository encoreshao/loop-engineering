![Loop X Engineering](https://raw.githubusercontent.com/encoreshao/loop-engineering/main/assets/loop-engineering.jpeg)

# Loop X Engineering

![License](https://img.shields.io/github/license/encoreshao/loop-engineering)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![Platform](https://img.shields.io/badge/platform-macOS-lightgrey)
![Dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-green)
![Shell](https://img.shields.io/badge/shell-bash-4EAA25)

An unattended, weekday-scheduled loop that checks the GitLab issues assigned
to you, and for each one either implements a fix and opens a merge request,
answers directly with a comment, or escalates with a clarifying question —
plus a local web dashboard for watching it work, reviewing its history, and
configuring everything by hand instead of by editing JSON.

It never merges its own merge requests, never self-assigns issues, and only
ever touches the projects you explicitly list in its config.

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

Each scheduled run (`run-loop.sh`):

1. Lists every open GitLab issue assigned to your configured username, across every project alias in your config.
2. Processes them **one at a time, never in parallel**, following the step-by-step decision procedure in [`LOOPX_INSTRUCTIONS.md`](https://github.com/encoreshao/loop-engineering/blob/main/LOOPX_INSTRUCTIONS.md).
3. For each issue, does exactly one of:
  - **Fix it** — in an isolated git worktree, on a `loop/issue-<iid>` branch, only opening a merge request once the project's own lint/test commands pass.
  - **Answer it** — post a GitLab comment when the ask needs no code change (a question, a status check).
  - **Escalate it** — post a GitLab comment asking for clarification when the ask is ambiguous, or when verification fails.
4. Sends a Slack message per issue plus one end-of-run digest (every run, even mornings with nothing assigned).
5. Updates [`PROGRESS.md`](https://github.com/encoreshao/loop-engineering/blob/main/PROGRESS.md) and `outputs/daily-review.md` so the next run — and you — know what happened.

Reusable, cross-run lessons (fix patterns, gotchas) get recorded per issue as markdown task-memory files via `bin/memory_store.py` (entries recorded before this format existed are still read via `bin/project_memory.py`), so later runs start smarter than the last.

A second, independent loop (`run-topic-monitor-loop.sh`) watches arbitrary topics on the wider web instead of GitLab — see [`docs/tasks/topic-monitor-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/topic-monitor-loop.md).

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

Same steps as above, but fails fast if nothing's installed at `--dir` yet instead of silently cloning fresh, and refreshes every one of this project's launchd agents that's currently loaded — not just the dashboard. The dashboard (an always-on server) gets an actual restart (`launchctl kickstart -k`), unlike a bare `launchctl load`, which is a no-op on an already-running agent. The GitLab loop and topic monitor, if you've separately enabled them from the Daemons page, just get their registration reloaded (`unload` + `load -w`) — never kickstarted, since that would trigger a real, out-of-schedule run against live GitLab/Slack right now rather than waiting for their normal schedule.

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
├── instructions.md              # your free-text instructions            ├─ gitignored, yours
├── ai_cli.json                  # your config: Claude Code vs Codex CLI   ┘
├── PROGRESS.md                  # live run state, updated every run
├── outputs/                     # ← generated docs & run history live here (gitignored)
│   ├── daily-review.md          #   latest GitLab-issue-loop report
│   ├── messages.json             #   Activity page message thread
│   ├── status.json               #   current/last run status
│   └── history/<date>.{md,log}   #   every past run's report + log
└── worktrees/                    # ← per-issue git worktrees for tracked projects (gitignored)
    └── <project>-issue-<iid>/    #   that project's own checkout, on branch loop/issue-<iid>
```

`projects.json`, `topics.json`, `instructions.md`, and `ai_cli.json` always resolve to `~/.loop-engineering/…` regardless of where you clone the code — they only end up *inside* the repo folder above because `install.sh`'s default clone target happens to be that same path. If you clone somewhere else by hand, those four files still live at `~/.loop-engineering/`, separate from the code. `projects.json`'s scaffolded `worktree_root` defaults to `~/.loop-engineering/worktrees` too, for the same reason.

Two more config files live outside this tree entirely, editable from the dashboard's **GitLab** and **Notifications** pages instead of by hand: `~/.gitlab/config.json` and `~/.slack/config.json`.

## Configuration


| File                                  | Holds                                                                                                                                                                                                                   | Managed via                                                                                                                                                                   |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `~/.loop-engineering/projects.json`   | Which projects to track, their local checkout paths, target branch, install/lint/test commands, your GitLab username, and the worktree scratch directory (`worktree_root`, defaults to `~/.loop-engineering/worktrees`) | Dashboard **GitLab** page's "Tracked Projects" section, or copy [`config/projects.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/projects.json.template) by hand, or let `bin/scripts/setup.sh` do it |
| ↳ per-project `instance` (optional)   | Overrides the top-level `gitlab_instance` for one project — set this when your projects span more than one GitLab instance. Falls back to `gitlab_instance` when omitted.                                               | Same file, per project entry — see the template's `harbor` example                                                                                                            |
| `~/.loop-engineering/topics.json`     | Which topics to monitor and what counts as notable for each one (topic monitor loop only)                                                                                                                               | Copy [`config/topics.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/topics.json.template) by hand, or let `bin/scripts/setup.sh` do it                                                                |
| `~/.loop-engineering/instructions.md` | Your own free-text instructions, read by the loop at the start of every run                                                                                                                                             | Dashboard **Instructions** page                                                                                                                                               |
| `~/.loop-engineering/ai_cli.json`     | Which AI CLI (Claude Code or Codex CLI) `run-loop.sh` and `run-topic-monitor-loop.sh` both invoke; defaults to `claude`                                                                                                  | Dashboard **AI CLI** page, or let `bin/scripts/setup.sh` do it                                                                                                                |
| `~/.gitlab/config.json`               | GitLab instance URLs, tokens, and project-alias → project-ID mappings (read by the `gitlab-config` skill)                                                                                                               | Dashboard **GitLab** page                                                                                                                                                     |
| `~/.slack/config.json`                | Your Slack incoming webhook URL (and any per-bundle overrides)                                                                                                                                                          | Dashboard **Notifications** page (the default webhook) / **GitLab** page's Access bundles section (per-bundle overrides)                                                              |


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
bash run-loop.sh                # the daily GitLab issue loop
bash run-topic-monitor-loop.sh  # the topic monitor loop
```

Both log to `outputs/history/`, and both also append every `claude` CLI invocation's output to `logs/loop-engineering.log` (viewable on the dashboard's **Logs** page); you can also trigger the GitLab loop from the dashboard's **Run now** button (Overview page) without a terminal.

**On a schedule**, via `launchd` — install the three agents under [`launchd/`](https://github.com/encoreshao/loop-engineering/tree/main/launchd), most easily with a click each from the dashboard's **Daemons** page (which also shows whether each is currently loaded and its PID), or by hand:

```bash
cp launchd/com.hermes.loop-engineering*.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.hermes.loop-engineering.plist
launchctl load -w ~/Library/LaunchAgents/com.hermes.loop-engineering-dashboard.plist
launchctl load -w ~/Library/LaunchAgents/com.hermes.loop-engineering-topic-monitor.plist
```


| Agent                                       | Runs                                                     |
| ------------------------------------------- | -------------------------------------------------------- |
| `com.hermes.loop-engineering`               | The loop itself, weekdays at 10:00 by default            |
| `com.hermes.loop-engineering-dashboard`     | The web dashboard, always-on (`RunAtLoad` + `KeepAlive`) |
| `com.hermes.loop-engineering-topic-monitor` | The topic monitor loop, every day at 10:00 by default    |


To change when a scheduled agent runs, use the schedule editor on the dashboard's **Daemons** page — it rewrites the plist and reloads `launchd` for you.

## The dashboard

A localhost-only, dependency-free (stdlib Python, no JS framework) web UI, served by `bin/web/dashboard_server.py`. Running it directly for local dev (no arguments) uses its own default port, `8420`. `bin/scripts/install.sh` picks a random port in `48420`-`48620` the first time it installs the always-on `launchd` agent (overridable with `--port`, and never re-picked on a later `--upgrade`) — check `launchd/com.hermes.loop-engineering-dashboard.plist` for the port an existing install is actually running on.


| Page              | Shows                                                                                                                                                                           |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Overview**      | Current/last run status, a live progress indicator, and the Run now button                                                                                                      |
| **Run History**   | Every past run's review report, newest first                                                                                                                                    |
| **Live GitLab**   | Your currently assigned issues and open MRs, fetched live                                                                                                                       |
| **Learnings**     | Cross-run lessons recorded per project                                                                                                                                          |
| **Topic Monitor** | Status and saved briefings for every configured topic                                                                                                                           |
| **Daemons**       | Load state, an editable schedule, and enable/disable for every `launchd` agent                                                                                                  |
| **Skills**        | Every external skill this loop depends on, and whether it's actually installed                                                                                                  |
| **GitLab**        | Manage `~/.gitlab/config.json` (instances, project aliases, access bundles) and `~/.loop-engineering/projects.json` (tracked projects, loop settings) without hand-editing JSON |
| **Notifications** | Manage `~/.slack/config.json`'s default webhook                                                                                                                                 |
| **AI CLI**        | Choose which AI CLI tool (Claude Code or Codex CLI) the GitLab issue loop and the Topic Monitor loop both use, with a live installed/not-found check for each                   |
| **Activity**      | A message thread with the loop, plus its own live progress indicator. Paste a GitLab issue link here to have the loop work on that one issue immediately, regardless of who it's assigned to.                                                                                                    |
| **Logs**          | The tail of `logs/loop-engineering.log` - every `claude` CLI invocation's output, across the GitLab loop, the topic monitor loop, and this dashboard's own chat assistant       |
| **README**        | This file, rendered in-app with a jump-to-section quicknav                                                                                                                      |
| **Preferences**   | Color mode, accent theme, and auto-refresh interval — saved to this browser's `localStorage`                                                                                    |
| **Instructions**  | Your own free-text instructions, read by the loop at the start of every run                                                                                                     |


**Optional: a friendly hostname via nginx**

By default the dashboard is only reachable at `http://127.0.0.1:<port>` (see above for how `<port>` is chosen). `bin/scripts/setup-nginx.sh` sets up a local nginx reverse proxy so it's reachable at `http://loop.local/` (port 80) instead — installs nginx via Homebrew if needed, writes the proxy config, adds `loop.local` to `/etc/hosts`, and starts nginx as a system service. `install.sh` already passes it the installed port automatically; idempotent, safe to re-run standalone too:

```bash
bin/scripts/setup-nginx.sh
# or, with no clone at all:
curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/setup-nginx.sh | bash
```

Writing `/etc/hosts` and starting the nginx service both need `sudo` — macOS will prompt for your password at those two steps. Pass `--domain`/`--port` to use something other than `loop.local`/`8420`.

## Scripts reference

Expand for the full list


| Script                              | Purpose                                                                                                                                                                                                                              |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `run-loop.sh`                       | Entry point for one scheduled or manual run — logs to `outputs/history/` and `logs/loop-engineering.log`, notifies Slack on failure                                                                                                  |
| `bin/scripts/build_run_prompt.sh`   | Builds the `PROMPT` string `run-loop.sh` hands to the AI CLI - the scheduled/all-assigned-issues prompt with no args, or a single-issue prompt when called with `<alias> <issue_iid>` (used when the dashboard's Activity chat triggers a scoped run) |
| `bin/web/dashboard_server.py`       | The web dashboard; also a small CLI (`write-status`, `write-skills-install-status`, `read-messages`, `add-message`, `chat-tool`) used by `run-loop.sh`, the dashboard's own actions, and the Activity page's embedded chat assistant |
| `bin/loop_config.py`                | Reads `~/.loop-engineering/projects.json`                                                                                                                                                                                            |
| `bin/list_assigned_issues.py`       | Lists open GitLab issues assigned to the configured user across configured projects                                                                                                                                                  |
| `bin/track_new_comments.py`         | Detects which notes on a cached issue are new since the loop last looked                                                                                                                                                             |
| `bin/project_memory.py`             | Reads (legacy) durable per-project lessons learned, stored inline in the GitLab cache                                                                                                                                                |
| `bin/memory_store.py`               | Reads/records durable per-issue task memory as markdown files (one per issue, plus a per-project MEMORY.md index)                                                                                                                    |
| `run-topic-monitor-loop.sh`         | Entry point for one scheduled or manual topic-monitor run                                                                                                                                                                            |
| `bin/topic_config.py`               | Reads `~/.loop-engineering/topics.json`                                                                                                                                                                                              |
| `bin/topic_seen.py`                 | Rolling 7-day dedup window per topic, so briefings don't repeat the same story two days running                                                                                                                                      |
| `bin/slack_notify.py`               | Posts a message to the configured Slack incoming webhook                                                                                                                                                                             |
| `bin/scripts/new_worktree.sh`       | Creates (or reuses) an isolated git worktree on a `loop/issue-<iid>` branch                                                                                                                                                          |
| `bin/scripts/open_merge_request.sh` | Pushes an issue branch and opens its MR — refuses anything not named `loop/issue-*`                                                                                                                                                  |
| `bin/scripts/install.sh`            | Online installer — clones (or updates) this repo, then runs `setup.sh`; `--upgrade` for an existing install, refreshing every currently-loaded launchd agent (dashboard restarted, GitLab loop/topic monitor just re-registered) so they pick up the new code; safe to pipe from `curl`                                          |
| `bin/scripts/setup.sh`              | One-command install: the `gitlab-config` skill + the `projects.json`/`topics.json` scaffolds                                                                                                                                         |
| `bin/scripts/setup-nginx.sh`        | Optional local nginx reverse proxy (`http://loop.local/` → the dashboard)                                                                                                                                                            |
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
| [`TASK.md`](https://github.com/encoreshao/loop-engineering/blob/main/TASK.md)                                                   | Index of every scheduled task this repo runs, each pointing at its own spec under `docs/tasks/`        |
| [`docs/tasks/gitlab-issue-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/gitlab-issue-loop.md)   | The GitLab issue loop's human-facing spec: goal, scope, safety boundaries                              |
| [`docs/tasks/topic-monitor-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/topic-monitor-loop.md) | The topic monitor loop's human-facing spec: goal, scope, safety boundaries                             |
| [`LOOPX_INSTRUCTIONS.md`](https://github.com/encoreshao/loop-engineering/blob/main/LOOPX_INSTRUCTIONS.md)                         | The step-by-step procedure the GitLab issue loop itself follows each run                               |
| [`TOPIC_MONITOR_INSTRUCTIONS.md`](https://github.com/encoreshao/loop-engineering/blob/main/TOPIC_MONITOR_INSTRUCTIONS.md)       | The step-by-step procedure the topic monitor loop itself follows each run                              |
| [`PROGRESS.md`](https://github.com/encoreshao/loop-engineering/blob/main/PROGRESS.md)                                           | Live state the loop reads and updates every run — last run's summary, open escalations, decisions made |
| [`docs/troubleshooting/crash-looping-launchd-agent.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/troubleshooting/crash-looping-launchd-agent.md) | Diagnose and fix a `com.hermes.loop-engineering*` launchd agent stuck crash-looping and flooding its log |




## License

[MIT](https://github.com/encoreshao/loop-engineering/blob/main/LICENSE) — see the [`LICENSE`](https://github.com/encoreshao/loop-engineering/blob/main/LICENSE) file.