# Tasks

This loop can run more than one scheduled task over time. Each task gets its
own spec — goal, scope, safety boundary — under `docs/tasks/`, plus its own
entry in `~/.loop-engineering/loops.json`, the unified scheduler's loop
registry (see `bin/loop_scheduler.py` and
`docs/superpowers/specs/2026-09-14-unified-loop-scheduler-design.md`). A
single launchd job, `com.hermes.loop-engineering` (`bin/loop_scheduler.py`,
polling on a `StartInterval`), checks that registry and runs whichever
loop(s) are due — there is no longer a separate `launchd/*.plist` per task.

| Task | Spec | Schedule |
|---|---|---|
| Daily GitLab issue loop | [`docs/tasks/gitlab-issue-loop.md`](docs/tasks/gitlab-issue-loop.md) | Weekdays 10:00, from its `loops.json` entry |
| Topic monitor loop | [`docs/tasks/topic-monitor-loop.md`](docs/tasks/topic-monitor-loop.md) | Every day 10:00 by default, from its `loops.json` entry |
| Inbox Triage loop | [docs/tasks/inbox-triage-loop.md](docs/tasks/inbox-triage-loop.md) | Weekdays 09:00 once enabled, from its loops.json entry (disabled by default) |
| Daily Digest loop | [`docs/tasks/daily-digest-loop.md`](docs/tasks/daily-digest-loop.md) | Weekdays 09:30 once enabled, from its `loops.json` entry (disabled by default) |
| MR Review loop | [`docs/tasks/mr-review-loop.md`](docs/tasks/mr-review-loop.md) | Every 2 hours once enabled, from its `loops.json` entry (disabled by default) |
| Pipeline Doctor loop | [`docs/tasks/pipeline-doctor-loop.md`](docs/tasks/pipeline-doctor-loop.md) | Hourly once enabled, from its `loops.json` entry (disabled by default) |
| RSS Watch loop | [`docs/tasks/rss-watch-loop.md`](docs/tasks/rss-watch-loop.md) | Daily 08:00 once enabled, from its `loops.json` entry (disabled by default) |

Each task gets its own spec under `docs/tasks/`, its own instructions doc,
and its own entry script — the topic monitor loop answered the "own
instructions doc? own config? shares `projects.json`?" question this file
used to defer: it has its own (`TOPIC_MONITOR_INSTRUCTIONS.md`,
`~/.loop-engineering/topics.json`), and shares nothing with the GitLab
loop's files except the one scheduler that runs both. The Inbox Triage loop
likewise has its own instructions doc (`INBOX_TRIAGE_INSTRUCTIONS.md`) and
config (`inboxes.json`, `mail_oauth.json`).
