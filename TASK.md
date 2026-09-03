# Tasks

This loop can run more than one scheduled task over time. Each task gets its
own spec — goal, scope, safety boundary — under `docs/tasks/`, plus its own
`launchd/*.plist` schedule.

| Task | Spec | Schedule |
|---|---|---|
| Daily GitLab issue loop | [`docs/tasks/gitlab-issue-loop.md`](docs/tasks/gitlab-issue-loop.md) | Weekdays 10:00 (`launchd/com.hermes.loop-engineering.plist`) |
| Topic monitor loop | [`docs/tasks/topic-monitor-loop.md`](docs/tasks/topic-monitor-loop.md) | Every day 10:00 by default, editable on the Daemons page (`launchd/com.hermes.loop-engineering-topic-monitor.plist`) |

Each task gets its own spec under `docs/tasks/`, its own instructions doc,
its own entry script, and its own `launchd/*.plist` — the topic monitor
loop answered the "own instructions doc? own config? shares
`projects.json`?" question this file used to defer: it has its own
(`TOPIC_MONITOR_INSTRUCTIONS.md`, `~/.loop-engineering/topics.json`), and
shares nothing with the GitLab loop's files.
