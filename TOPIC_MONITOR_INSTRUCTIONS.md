# Topic Monitor Instructions

You are running the topic monitor loop. Read `<loop_dir>/docs/tasks/topic-monitor-loop.md` before doing anything else — `<loop_dir>` is defined just below, and is where this file lives.

## `<loop_dir>`: always invoke this repo's scripts by absolute path

Throughout this file, `<loop_dir>` means the directory this file lives in (the loop repo root). Every script invocation below is written as `python3 <loop_dir>/bin/<path-to-script>.py ...` and must be run in exactly that absolute form.

## Configuration

Which topics to monitor, and what counts as notable for each one, live in `~/.loop-engineering/topics.json`, not in this file. Look them up as you go:

```
python3 <loop_dir>/bin/topic_config.py names             # every configured topic's name, one per line
python3 <loop_dir>/bin/topic_config.py topic <name>       # {name, label, brief, slack_bundle} for one topic
```

If `~/.loop-engineering/topics.json` does not exist, stop immediately and report that setup is incomplete — do not guess topics.

## Step 1: List today's topics

Run `python3 <loop_dir>/bin/topic_config.py names`. This is the run's task list. Process every topic in the order listed, **one at a time, never in parallel**.

## Step 2: Process each topic

For each topic name:

1. **Look up its details.** `python3 <loop_dir>/bin/topic_config.py topic <name>` — keep `label` and `brief` on hand.

2. **Report you're starting.**
   ```
   python3 <loop_dir>/bin/web/dashboard_server.py write-topic-status <name> running --current-step researching
   ```

3. **Read what's already been reported.**
   ```
   python3 <loop_dir>/bin/topic_seen.py get <name>
   ```
   This returns a list of `{url, title}` already covered in the last 7 days — never lead a new briefing with one of these.

4. **Research.** Use WebSearch/WebFetch to find what's genuinely new for this topic since the last run, guided by its `brief` text. Use your own judgment for how many searches are enough — this step is not script-driven. Skip anything already in the seen-items list from step 3.

5. **Write the briefing.** Compose a short markdown file: a one-line summary at the top, then each notable item as a heading with a one-or-two sentence description and its source link. If nothing new turned up, write a briefing that says so explicitly (e.g. "Nothing notable since the last run.") — never skip writing the file.

   Save it with the Write tool to exactly this path (creating the `outputs/topic-monitor/history/` directory if it doesn't exist yet):
   ```
   <loop_dir>/outputs/topic-monitor/history/<YYYY-MM-DD>-<name>.md
   ```
   using today's date and the topic's own `name` (not its `label`).

6. **Record seen-items.** For every item included in today's briefing:
   ```
   python3 <loop_dir>/bin/topic_seen.py add <name> "<url>" "<title>"
   ```

7. **Notify Slack.** One message containing the briefing's content directly (the dashboard is localhost-only, so never link to it):
   ```
   python3 <loop_dir>/bin/slack_notify.py<bundle_flag> "*<label> briefing (<YYYY-MM-DD>):* <condensed summary>"
   ```
   `<bundle_flag>` is the empty string if this topic's `slack_bundle` is `null`, or ` --bundle=<slack_bundle>` (including the leading space) otherwise — same convention `LOOPX_INSTRUCTIONS.md` uses for GitLab loop notifications.

8. **Report you're done.**
   ```
   python3 <loop_dir>/bin/web/dashboard_server.py write-topic-status <name> idle
   ```

## Failure policy

If WebSearch/WebFetch or any command above fails for a topic, still write a briefing noting the failure and move on to the next topic — never let one topic's failure stop the whole run. Report the failed topic's status as `failed` instead of `idle` in step 8.

## Tool permissions policy

This section is prose. The list actually enforced at runtime is the `ALLOWED_TOOLS`/`DISALLOWED_TOOLS` variables in `run-topic-monitor-loop.sh`; whenever either this prose or those variables change, update both together.

**A note on AI CLI choice:** the confinement to `outputs/topic-monitor/`
described below is enforced by the harness only when this loop runs
under Claude Code (the default). If the dashboard's AI CLI page has
Codex CLI selected instead, this loop invokes `codex exec --sandbox
workspace-write -c approval_policy=never -c
sandbox_workspace_write.network_access=true -c tools.web_search=true`,
which has no equivalent to Claude's deny-list-based confinement of writes
to a single directory -
this loop's write boundary becomes policy this document asks the agent
to follow, not a technically enforced boundary. Switch to Codex only
with that trade-off in mind.

Allowed: `WebSearch`, `WebFetch`; file access scoped to `outputs/topic-monitor/` via `Read(**/outputs/topic-monitor/**)` and `Edit(**/outputs/topic-monitor/**)` — plus read-only access to this file and `docs/tasks/topic-monitor-loop.md`, the two documents the run itself has to read; `python3 bin/topic_config.py`, `bin/topic_seen.py`, `bin/slack_notify.py`, `bin/web/dashboard_server.py` (relative or absolute path); `cd`.

Not allowed: any `git` command, any command touching a project checkout, reading `.env`/credentials/SSH keys, writing anywhere outside `outputs/topic-monitor/`.

### How that boundary is actually enforced

Three things here are easy to get wrong, and each was checked by running the real CLI rather than assumed:

- **`--add-dir` enforces nothing.** It only *adds* directories to the workspace. The run's working directory is already the loop repo root, so add-dir'ing a subdirectory of it grants nothing and restricts nothing.
- **The allow list enforces nothing either.** An allow rule grants; it never revokes. Scoping the grant to `Edit(**/outputs/topic-monitor/**)` states the intent, but a path no rule mentions is still writable — under `--permission-mode acceptEdits`, and under whatever the machine's own `~/.claude/settings.json` allows globally.
- **The deny list is the boundary.** Deny beats every allow, local or global, so `DISALLOWED_TOOLS` in `run-topic-monitor-loop.sh` is the only rule kind here that can actually stop a write. Every rule in it except one is written `**/<shape>`, and `**/`-prefixed patterns are anchored to the run's working directory (this repo's root, since the script `cd`s there) — they never match an absolute path outside it. Those cwd-anchored rules deny, by extension, every `*.sh`/`*.py`/`*.plist`/`*.json`/`*.yml`/`*.toml`; by directory, `bin/`, `launchd/`, `docs/`, `config/`, `tests/`, `assets/`, `.claude/`, `.git/` and the GitLab loop's `outputs/history/`; and by name, this repo's root markdown files (`LOOPX_INSTRUCTIONS.md`, this file, `CLAUDE.md`, `README.md`, `TASK.md`, `PROGRESS.md`) — listed individually because the briefings are markdown too, so a blanket `**/*.md` would block the run's own work. None of those shapes occurs under `outputs/topic-monitor/`.

  Being cwd-anchored, none of the above reach outside this repo checkout — in particular they do **not** cover the *installed* copy of the GitLab loop's own launchd schedule at `~/Library/LaunchAgents/com.hermes.loop-engineering.plist`, which lives outside `$LOOP_DIR` entirely. That gap is closed by the one non-`**/`-prefixed rule in the list, `Edit(/$HOME/Library/LaunchAgents/**)`, which uses an absolute path (leading `/`) precisely because absolute patterns are *not* cwd-anchored. Without it, a prompt injection from fetched web content could rewrite that plist's `ProgramArguments` and get arbitrary code execution on the machine's own schedule — the same escalation class this loop's confinement exists to prevent. This rule was verified against the real CLI in a scratch replica: it denies a write under a fake `~/Library/LaunchAgents/` path while leaving `outputs/topic-monitor/**` writable.

One more detail worth knowing before editing that list: file permission rules match on `Read(...)` and `Edit(...)` **only**. An `Edit(...)` rule covers every file-editing tool, `Write` included; a `Write(path)` rule matches nothing at all and the CLI prints a warning about it. Never write a `Write(path)` rule and assume it does something.

Residual gap, known and accepted: a *new* root-level file whose extension isn't in the denied set (a stray `.md`/`.txt`) can still be created. Nothing reads such a file, so it's clutter rather than a privilege escalation; every existing control file at that level is covered by name. An absolute-path deny (`Edit(//<repo>/*)`) does not fix it — its `*` crosses `/` and swallows the briefings too.

## Verification checklist (before ending the run)

- Every topic from `topic_config.py names` has a briefing file for today under `outputs/topic-monitor/history/`.
- Every topic's status was written as `idle` or `failed` (never left `running`).
- No file outside `outputs/topic-monitor/` was created or modified.
