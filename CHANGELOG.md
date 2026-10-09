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

- The dashboard overview shows a Live link for each enabled Meeting Prep and Inbox
  Triage loop.
- `build_macos_app.sh --dmg` packs `dist/Loop X.dmg`, and `rebuild_macos_app.sh` pulls the
  latest changes, rebuilds the app and DMG, and relaunches the app in one command.
- Generated content is now saved and viewable in the app any time: Inbox Triage has a
  Drafts tab with the full text of every reply draft it wrote, and every LoopKit loop (Meeting
  Prep briefs, RSS Watch highlights, ...) has a Content tab with its saved results.
- `bin/scripts/build_macos_app.sh` builds `dist/Loop X.app`, a native macOS
  window around the dashboard (pywebview). It attaches to the running
  dashboard daemon, or serves the dashboard itself when none is running. `--desktop-shortcut` adds an alias to it on `~/Desktop`. It has its own
  app icon (`assets/app-icon.svg`).

- Custom instructions now apply to the topic monitor and inbox triage loops
  too, not just the GitLab issue loop. Besides the global
  `~/.loop-engineering/instructions.md`, you can add per-loop files at
  `~/.loop-engineering/instructions/<loop>.md` (`gitlab-issue`,
  `topic-monitor`, `inbox-triage`).

### Changed

- New Loop X logo (a ring of four coloured arcs broken by an X) on the
  dashboard: browser tab icon, sidebar and chat bubbles. The sidebar now
  always shows the logo (28px) next to the name, not only when collapsed.
- Calendar Prep is now **Meeting Prep**. Its Live page lists every meeting
  today as past, ongoing or upcoming, with each title linking to the meeting
  link, a Brief ready / pending badge and the brief's summary, and its run table shows meeting titles instead of internal keys. Its
  Slack brief is now formatted ("Meeting Prep:" bold title, a time chip and a
  clickable Join meeting link, quoted summary, a numbered Agenda and bulleted
  Open items / Raise / From last time with clickable GitLab links; other
  chat services get plain `label (url)`). The loop is
  now `meeting-prep-loop`; an existing `loops.json` entry named
  `calendar-prep-loop` (and its default label) is renamed automatically.
- A loop's History tab is now grouped by day (newest first, today open) and
  shows each run's done/skipped/failed counts and first items.
- The dashboard now defaults to the Indigo accent theme (a saved choice still wins).
- A loop's "Notify via" picker is now compact (capped height, label beside it) instead of stretching tall.
- Inbox Setup (Add inbox, Gmail app, Outlook app) and Connectors > Accounts have
  a roomier layout: paired form fields, numbered setup steps, and account rows
  with status on the left and actions on the right.
- The built-in agent instructions moved into one `instructions/` folder:
  `LOOPX_INSTRUCTIONS.md` is now `instructions/gitlab-issue.md`,
  `TOPIC_MONITOR_INSTRUCTIONS.md` is `instructions/topic-monitor.md` and
  `INBOX_TRIAGE_INSTRUCTIONS.md` is `instructions/inbox-triage.md`.

## [0.2.0] - 2026-10-07

### Added

- **Stale Work Sweeper** loop (disabled by default): every Monday at 09:00 it
  lists your GitLab issues and MRs idle 14+ days and reviews waiting on you
  3+ days. It makes no model call.

### Changed

- LoopKit loops cap each retry at what is left of the item's
  `max_cost_usd`, so a retry can no longer spend the full budget a second
  time.
- Inbox Triage AI calls are now capped by the loop's `max_cost_usd`
  (previously uncapped), each attempt at what is left of it.
- Topic Monitor reports what each Claude run cost (the CLI now runs with JSON
  output), so its runs show a real cost instead of unknown.
- Insights → Analytics: the Loop Health tiles for cost efficiency, retry rate
  and learning effectiveness now show the figure behind the score ($ per
  verified issue, % of runs retried, points gained with memory).

### Fixed

- On phone-width screens (about 390px) the topbar's controls scroll sideways
  instead of being cut off, and cards, grids and tab bars stay inside the
  page.
- A plugin loop's page and its run history files show that loop's own status
  in the topbar, not the GitLab loop's.
- Connectors: a failed save no longer leaves a secret behind in the Keychain
  (a new account's secret is removed, a replaced or renamed one is restored),
  and simultaneous saves or Test clicks no longer overwrite each other.
- MR Review no longer downloads the diff of an MR whose current commit it
  already reviewed, finds its old draft notes beyond the first 100, and
  anchors notes on unchanged context lines and renamed files (they used to
  fall back to general notes).
- Daily Digest's "yesterday" and "today's meetings" windows no longer drift by
  an hour on a daylight-saving change day.
- A LoopKit loop that is interrupted (SIGTERM, crash) now always reports the
  original error, even if writing its run report fails too; a crashed item's
  summary is capped at 300 characters.
- The dashboard starts without the gitlab-config skill installed (e.g. in a
  sandbox with a scratch `HOME`); legacy project learnings then read as empty.
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

[Unreleased]: https://github.com/encoreshao/loop-engineering/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/encoreshao/loop-engineering/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/encoreshao/loop-engineering/releases/tag/v0.1.0
