# Inbox Triage Loop

## Goal

Triage new unread mail in each connected inbox (Gmail or Outlook) since the
last run: categorise every message into one of a fixed-by-default, editable
category set, apply the matching `Loop/*` label or category, and draft a
threaded reply for anything urgent — left in the mailbox's own Drafts folder
for the user to review and send. Every run also sends a Slack digest, one
message per distinct `slack_bundle` used by the connected inboxes (the
default config has every inbox on the default bundle, so a normal run sends
one message); a quiet run — nothing new since last time — still sends its
digest, exactly as the GitLab issue loop and the topic monitor loop still
report on a run with nothing to do.

This is a third, independent loop from the [daily GitLab issue
loop](gitlab-issue-loop.md) and the [topic monitor loop](topic-monitor-loop.md)
— its own entry script (`bin/inbox_triage_runner.py`), instructions doc
(`INBOX_TRIAGE_INSTRUCTIONS.md`), and config files (`inboxes.json`,
`mail_oauth.json`). It shares nothing with either other loop's files except
the one scheduler that runs all three, `bin/loop_scheduler.py`.

## Setup

`bin/scripts/setup.sh` scaffolds `~/.loop-engineering/inboxes.json` from
`config/inboxes.json.template`, the same way it scaffolds `projects.json`
and `topics.json`. Unlike those two, though, connecting an actual mailbox
is not a hand-edit-JSON step: it all happens on the dashboard's **Inbox
Setup** page (`/inbox/setup`):

1. Follow the per-provider numbered instructions to register an OAuth app
   (Google Cloud Console for Gmail, Azure App registrations for Outlook) and
   paste the resulting client ID (and, for Google, client secret) into the
   form there. The wizard shows the exact redirect URI to paste back into
   Google's console — `http://127.0.0.1:<dashboard-port>/oauth/google/callback`
   — since Google allows any loopback port for a Desktop-app client, so this
   works even when the dashboard is normally reached through nginx.
2. Add an inbox (label, provider, account address, `urgent_brief`, VIP/
   excluded senders, optional Slack bundle).
3. Click **Connect** — Gmail opens Google's consent screen in the browser;
   Outlook shows a device code and `https://microsoft.com/devicelogin` and
   polls in the background until you finish signing in there.
4. Click **Test connection** to confirm the stored token's mailbox address
   still matches what you configured.

The loop itself is registered in `~/.loop-engineering/loops.json` as
`inbox-triage-loop`, **disabled by default** — like the GitLab and topic
loops, it must not act before an inbox is actually connected. Enable it from
the dashboard's **Daemons** page (or by hand-editing its `loops.json` entry)
once at least one inbox is connected, or just use the **Run now** button on
the **Inbox Triage** page (`/inbox`) to trigger a run on demand — same
runner either way.

## Scope

Every field below lives in `~/.loop-engineering/inboxes.json` (seeded from
`config/inboxes.json.template`), a JSON object of `default_categories` plus
an `inboxes` array:

| Field | Meaning |
|---|---|
| `name` | Stable slug (`^[a-z0-9][a-z0-9-]*$`); the identity key for the Keychain entry, seen-state file, and history file names. Immutable once the inbox is created — renaming means delete and re-add. |
| `label` | Human-readable name shown on the dashboard and in Slack. |
| `provider` | `gmail` or `outlook`. |
| `account` | The expected mailbox address. **Test connection** and every run verify the token's own profile address matches this (case-insensitively) and fail the inbox otherwise. |
| `enabled` | Toggled with the **Pause**/**Resume** button on `/inbox`, not the edit form — pausing an inbox stops it being touched by a run without losing its configuration. |
| `urgent_brief` | Free text steering what counts as urgent for this inbox, same idea as a topic's `brief`. |
| `vip_senders` | Addresses that are always categorised `urgent`, enforced in Python after the AI responds — never left to the prompt alone. |
| `exclude_senders` | Exact addresses or `@domain` suffixes. A matching message is filtered out of what's fetched: it is never sent to the AI and never labelled. |
| `categories` | `null` uses `default_categories`; a custom list must still include an `urgent` key, since the VIP-sender rule above always needs one to apply. |
| `slack_bundle` | Same meaning as in `topics.json` — `null` uses the default Slack webhook, or names an access bundle's webhook override. |

Default categories (overridable per inbox, but a custom set must keep an
`urgent` entry):

| Category | Label | Meaning | Draft reply |
|---|---|---|---|
| `urgent` | `Loop/Urgent` | Needs a response today: a direct ask from a real person with a deadline, a blocker, a client/boss escalation | yes |
| `action` | `Loop/Action` | Needs a reply or task, but not today | no |
| `fyi` | `Loop/FYI` | Worth reading, no action: updates, CCs, announcements | no |
| `notifications` | `Loop/Notifications` | Automated: GitLab, CI, calendar, SaaS alerts | no |
| `newsletters` | `Loop/Newsletters` | Marketing, digests, subscriptions | no |

Per run, per inbox: unread Inbox messages newer than the high-water mark are
fetched oldest-first (first run ever, with no high-water mark yet: the last
48 hours), excluding `exclude_senders` and anything already triaged in the
last 14 days, capped at 50 messages — anything past the cap is noted in the
digest as overflow and picked up automatically on the next run. Gmail pages
its message list up to 20 pages of 100; Outlook follows Graph's
`@odata.nextLink` up to 20 pages the same way. Inboxes are processed one at
a time, sequentially, never in parallel — same discipline as the GitLab
loop's issues and the topic monitor's topics.

## Expected output

Each run produces, per connected and enabled inbox:

- `outputs/inbox-triage/history/<date>-<inbox>.md` — one row per triaged
  message: time, sender, subject, category, reason, and a link to the draft
  if one was created. Never the message body. Multiple runs on the same day
  append a new section to the same file, same pattern as the topic
  monitor's history.
- `outputs/inbox-triage/state/<inbox>.json` — the high-water mark and the
  rolling 14-day set of already-triaged message IDs.
- `outputs/inbox-triage/status.json` — per inbox: `idle` / `running` /
  `failed` / `needs_reauth`, last run time, last counts per category.

And, across the whole run: **one Slack digest per distinct `slack_bundle`**
in use by the connected inboxes — the default config has every inbox on the
same (default) bundle, so a normal run sends exactly one message, with one
section per inbox in that bundle, e.g. "Work Gmail: 2 urgent (drafts
ready), 4 action, 11 other", followed by each urgent sender and subject.
Sent through `bin/slack_notify.py` under a new `notification_key`,
`inbox_triage_digest`, so it can be bound to its own Block Kit template the
same way the GitLab and topic-monitor alerts are. A run with no new mail in
any inbox still sends the digest, saying so.

## Safety boundary (fixed — does not loosen with time or repeated success)

- **Never sends mail.** Neither provider module (`bin/mail_providers/gmail.py`,
  `bin/mail_providers/outlook.py`) contains a send function; a test asserts
  neither module references a send endpoint (`messages/send`, `drafts/send`,
  `/sendMail`, `/send`). For Outlook the token additionally lacks
  `Mail.Send` — `bin/mail_auth.py`'s requested scope is
  `offline_access https://graph.microsoft.com/Mail.ReadWrite` only — so
  sending is impossible at the token level, not just at the code level.
  Gmail has no scope that permits drafts and labels without also permitting
  send, so for Gmail this guarantee is code-enforced instead: the loop's
  code never calls a send endpoint even though the token could.
- **Never archives, deletes, moves, or changes read state.** The only
  mailbox writes anywhere in this loop are: creating `Loop/*` labels
  (Gmail) or master categories (Outlook), applying one to a message, and
  creating a reply draft. Outlook's reply draft is a single `createReply`
  call with `{"comment": body}` — the reply text goes above the quoted
  original, and nothing else about the original message is touched.
- **The AI never sees a token and never has a tool.** The AI call is a
  single tool-less JSON-in/JSON-out invocation; all mail I/O — fetching,
  labelling, drafting — is plain Python, never something the AI itself
  does. Claude is invoked with `--tools ""`, `--strict-mcp-config` plus an
  empty `--mcp-config` (otherwise the user's own claude.ai connectors —
  which can include a Gmail connector with a send tool — would load into
  the session), `--no-session-persistence`, and hooks disabled via
  `--settings '{"disableAllHooks": true}'`. Codex is invoked as `codex exec
  --sandbox read-only --skip-git-repo-check -c mcp_servers={} -c
  tools.web_search=false -`. Both take the prompt on stdin (so message
  content never appears in a process's argv / `ps`), and both run with
  their working directory set to a fresh, disposable temp directory rather
  than this repo checkout — so the AI call never loads this repo's own
  `CLAUDE.md`/auto-memory into the session (see the exception below for
  Claude specifically).
  **Known gap:** `codex exec` unconditionally writes its own session
  rollout — including the prompt, i.e. the trimmed email bodies sent to
  it — to `~/.codex/sessions/`. No config key or flag to disable that
  rollout writer was found in `codex --help`, `codex exec --help`, or the
  installed binary's own embedded config-field list. **Anyone who needs the
  "message bodies never persist" guarantee to actually hold should select
  Claude, not Codex, as this loop's AI CLI** (dashboard **Settings** → AI
  CLI). Separately, on the Claude path, the user's own *global*
  `~/.claude/CLAUDE.md` still loads into the `claude -p` call the same way
  it would for any other `claude` invocation on the machine — only this
  repo's own project `CLAUDE.md` is avoided, by way of the scratch-directory
  `cwd`.
- **Message bodies never persist.** Body text exists only in memory and in
  the one AI call above. It is never written to `outputs/`, the unified
  log, or the Slack digest — only sender, subject, category, reason, and a
  draft link ever reach any of those.
- **Only configured, enabled inboxes are touched.** A `enabled: false`
  inbox (the **Pause** toggle) is skipped entirely by a run.
- Inboxes are processed one at a time, sequentially — never multiple
  inboxes' mail fetched or triaged in parallel in the same run.
- Refresh tokens live only in the macOS **Keychain**, never on disk in
  plain text: written via `/usr/bin/security add-generic-password ... -w
  <token> -T /usr/bin/security`, which briefly puts the token on that one
  `security` process's own argv — visible only to a same-user `ps` for the
  instant that process runs. `security` offers no other non-interactive way
  to write a Keychain item; this is accepted as a single-user-Mac
  trade-off, the same way `bin/mail_auth.py`'s own module docstring
  frames it. When `LOOP_ENGINEERING_HOME` is set, the Keychain service name
  (`loop-engineering.mail`) is suffixed `.sandbox-<hash of that path>`, so
  a dev sandbox or the test suite can never read or overwrite the real
  mailbox tokens.
