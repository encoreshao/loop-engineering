# Changelog

All notable changes to Loop X Engineering are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the
version is `0.x`, a minor bump may include breaking changes; those are called
out under **Changed** or **Removed**.

Every user-visible change adds a line under **Unreleased** in the same commit
or PR. To cut a release, see "Changelog and releases" in `CLAUDE.md`.

## [Unreleased]

### Added

- **Stale Work Sweeper** loop (disabled by default): every Monday at 09:00 it
  lists your GitLab issues and MRs idle 14+ days and reviews waiting on you
  3+ days. It makes no model call.

### Fixed

- Older runs added to the ledger by the backfill whose cost was never
  reported (Codex, topic monitor) now show an unknown cost instead of $0.

## [0.1.0] - 2026-10-07

The first tagged release. It records everything shipped up to this point;
later releases list only what changed since the previous one.

### Added

#### Loops

- **GitLab issue loop**: works on assigned GitLab issues one at a time in its
  own worktree. For each issue it fixes, answers, escalates or waits for a
  reviewer. It opens MRs through `open_merge_request.sh` and posts
  structured-markdown comments. It re-runs the project's own `test_cmd` and
  `lint_cmd` as external verification, and records per-project fix lessons
  in its memory.
- **Verification gate** for the GitLab issue loop (`verification.mode`):
  - `observe` (the default) records check results without changing the
    outcome.
  - `gate` retries once with the failed-check output as feedback. In gate
    mode the harness, not the agent, opens the MR, and only when checks
    pass. Otherwise it escalates with a labeled comment, the
    `loop:needs-human` label and one Slack alert.
- **Topic Monitor loop**: researches each enabled topic on a schedule and
  sends Slack summaries with source links. Topics can be renamed and
  enabled or disabled individually.
- **Inbox Triage loop** (Gmail and Outlook):
  - Classifies new mail into categories and applies `Loop/` labels or
    categories.
  - Writes reply drafts and never sends mail.
  - Requires the Claude CLI, and refuses to run under codex before reading
    any mail.
  - Signs in with Google PKCE or the Microsoft device code; tokens are kept
    only in the macOS Keychain.
- **LoopKit** (`bin/loopkit.py`), a plugin runner for scheduled loops:
  - run lock and seen store
  - per-item isolation, with progress saved after each item
  - an output-contract check that retries the model with feedback
  - run history and notifications
  - sanitizing of chat text, links and URLs
  - a sealed model call with no tools, no MCP and no session persistence
- **Loops built on LoopKit** (all disabled by default):
  - **Daily Digest**: a weekday-morning brief of todos, assigned issues,
    MRs to review, today's meetings and what Loop X did.
  - **MR Review**: pre-reviews MRs where you are a reviewer, as GitLab draft
    notes only.
  - **Pipeline Doctor**: explains failed pipelines (cause, culprit, fix) and
    flags failures that keep recurring.
  - **RSS Watch**: ranks new feed entries against your interests.
  - **Calendar Prep**: a brief before each meeting with the agenda, linked
    GitLab work, recent mail with the attendees, and follow-ups from last
    time.
  - **Release Notes**: when a tracked project gets a new tag, writes notes
    from the MRs merged since the previous tag.
- **Unified scheduler** (`bin/loop_scheduler.py`): one launchd job polls a
  loop registry (`loops.json`) that supports daily, weekly, monthly, hourly
  and 15/30/45-minute schedules.

#### Dashboard

- Navigation in 7 hubs: Dashboard, Loops, Runs, Insights, Harness,
  Connectors and Settings. Every old URL redirects to its new home.
- Chat-style Dashboard with sessions and history, and an AI side panel that
  can explain runs, add topics and projects, and turn loops on or off.
- Loops page: a managed list of loop cards with enable/disable, schedule
  editing, Run now, and per-loop settings and "Notify via".
- **Connectors**: add, test and delete accounts for many services. Secrets
  are kept only in the Keychain.
  - Code and issues: GitLab, GitHub, Jira Cloud, Linear, Notion.
  - Chat: Slack, Telegram, WeCom, Google Chat, Feishu, DingTalk, Microsoft
    Teams, Discord and generic webhooks.
  - Feeds and mail: RSS/Atom, Gmail and Outlook.
  - Calendar: Google Calendar.
- **Runs**: Loop Runs (with incomplete runs), Run History and Logs.
- **Insights**: Analytics with a 7/7 Loop Health score, Cost, Budget
  rollups, and Memory with a "Needs review" flag on down-weighted lessons.
- **Harness**: Gates (agreement rate and gate outcomes), Evals (the last
  scripted and golden runs) and Audit.
- Slack Block Kit Builder for notification templates.
- Japanese, Simplified Chinese and French translations, with a language
  switcher and READMEs in all four languages.
- Light and dark themes with selectable accents.

#### Harness and tooling

- `loop_cli.py`: `init`, `validate`, `audit`, `run`, `status`, `inspect`,
  `cost`, `doctor`, `replay`, `eval` and `ledger`.
- One run ledger built from `loop.result` events, with automatic backfill of
  older `result.json` runs.
- Cost caps: every Claude call gets `--max-budget-usd` set to the run's
  remaining budget.
- Loop audit (Loop Ready Score); CI blocks any loop definition scoring
  below 70.
- Scripted evals (`loop eval`) and paid golden evals (`loop eval --golden`)
  that run the real agent offline on synthetic fixture repos.
- `install.sh`, `uninstall.sh` and `setup.sh` with launchd agents and an
  optional nginx front end.

[Unreleased]: https://github.com/encoreshao/loop-engineering/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/encoreshao/loop-engineering/releases/tag/v0.1.0
