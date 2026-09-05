# Loop X Instructions

You are running the daily GitLab issue loop. Read `<loop_dir>/docs/tasks/gitlab-issue-loop.md` and `<loop_dir>/PROGRESS.md` before doing anything else — `<loop_dir>` is defined just below, and is where this file lives.

This same procedure also runs on demand, scoped to exactly one issue,
when triggered from the dashboard's Activity page chat by pasting a
GitLab issue link (see `chat-tool run-issue` in
`bin/web/dashboard_server.py`). In that mode, Step 1 below is skipped
entirely - the task list is just the one issue named in the prompt -
and the issue does not need to be assigned to the configured username;
pasting the link is itself the authorization. Every safety boundary
below (worktree-only edits, the lint/test gate before opening an MR,
never merging, never touching an untracked project) still applies
unchanged.

If `~/.loop-engineering/instructions.md` exists and is non-empty, read it too and follow it for the rest of this run, on top of (never in place of) everything in this file — it's the user's own free-text instructions, saved via the dashboard's **Instructions** page (see `render_instructions_page` in `bin/web/dashboard_server.py`). It's fine, and expected, for this file to not exist or to be empty; that just means no additional instructions were set.

## Configuration

All project-specific facts — which projects to track, their GitLab project IDs, their local checkout paths, their target branch, their install/lint/test commands, which GitLab username to track, and the worktree scratch directory — live in `~/.loop-engineering/projects.json`, not in this file. This file is the same for every team member; the config file is per-machine.

## `<loop_dir>`: always invoke this repo's scripts by absolute path

Throughout this file, `<loop_dir>` means the directory this `LOOPX_INSTRUCTIONS.md` lives in (the loop repo root). **Every** script invocation below is written as `python3 <loop_dir>/bin/<path-to-script>.py ...` / `bash <loop_dir>/bin/<path-to-script>.sh ...` and must be run in exactly that absolute form — never as a bare `bin/<script>` relative path.

The reason: during an issue's work you `cd` into that issue's worktree (step 5), so the current directory is not `<loop_dir>` for most of the run. An absolute path works identically from any directory, so there is never anything to reason about. Both the relative and absolute forms are on the permission allowlist, but only the absolute form is correct unconditionally, so always use it.

Look things up as you go:
```
python3 <loop_dir>/bin/loop_config.py aliases                 # every configured project alias, one per line
python3 <loop_dir>/bin/loop_config.py project <alias>          # {project_id, local_path, target_branch, install_cmd, lint_cmd, test_cmd, instance} for one alias
python3 <loop_dir>/bin/loop_config.py assignee                 # the GitLab username to track
python3 <loop_dir>/bin/loop_config.py instance                 # the config's default GitLab instance (only used when an alias doesn't set its own `instance`)
python3 <loop_dir>/bin/loop_config.py worktree-root            # where per-issue worktrees are created
python3 <loop_dir>/bin/project_memory.py get <instance> <project_id>  # legacy lessons learned on this project from prior runs
python3 <loop_dir>/bin/memory_store.py list <alias>                   # file-based task memory for prior issues on this project
```
If `~/.loop-engineering/projects.json` does not exist, stop immediately and report that setup is incomplete — do not guess paths.

## Step 1: List today's issues

Run:
```
python3 <loop_dir>/bin/list_assigned_issues.py
```
With no arguments this defaults to every alias from `loop_config.py aliases` and the username from `loop_config.py assignee`. This returns `{alias: [issue, ...]}` for open issues currently assigned to that user. This is the run's task list. If every list is empty, skip Step 2 and go straight to "End of run" — **but still complete "End of run" in full, including the Slack digest.** A quiet morning still gets a Slack message (e.g. "No assigned issues today — nothing to do"), it just skips the per-issue work.

## Event reporting

Throughout Step 2 below, you'll see `python3 <loop_dir>/bin/events.py emit
...` calls alongside the existing Slack/status calls. Every one of them is
best-effort: `$LOOP_RUN_ID` is already set in your environment (exported by
`run-loop.sh` before it started you), and every issue's own `--issue-run-id`
is `<run_id>_<alias>_<issue_iid>`, built from values already on hand at each
call site. If one of these `emit` calls fails, note it and continue the
issue's own flow exactly as if it had succeeded — it is never a
verification failure or a tool-permission violation, and never a reason to
escalate or stop.

## Step 2: Process issues ONE AT A TIME

For each project alias and each issue in that project's list, in order — never in parallel. Before starting work on a given alias, run `python3 <loop_dir>/bin/loop_config.py project <alias>` once and keep its `project_id`, `local_path`, `target_branch`, `install_cmd`, `lint_cmd`, `test_cmd`, and `instance` values on hand for every step below — `instance` is this alias's own GitLab instance (its own override if `projects.json` set one for it, otherwise the config's default), never the same for every alias when projects span more than one instance. Also run `python3 ~/.encore-skills/skills/gitlab-config/scripts/gitlab_api.py project-info <alias>` once and note its `bundle` field. Every `slack_notify.py` call below for this alias's issues has `<bundle_flag>` inserted immediately after `slack_notify.py`, where `<bundle_flag>` is: the empty string, if `bundle` is `null` (those calls are then unchanged from today); or the string ` --bundle=<that value>` — including its own leading space — if `bundle` is non-null. Either way, `slack_notify.py<bundle_flag>` produces a correctly-spaced command; don't add or remove any extra space when substituting it. Also keep the issue's own `web_url` (GitLab's permalink to that issue, already present in step 1's output for each issue) on hand alongside its `issue_iid` and title — that is the `<issue_url>` used in every Slack message below.

Slack messages in this file use Slack's own `mrkdwn` syntax — bold is `*text*` and a link is `<url|link text>` — **not** GitHub-flavored markdown (`**text**`, `[text](url)`), which Slack does not render. `slack_notify.py` posts a plain `{"text": ...}` payload, which Slack renders as `mrkdwn` automatically, so the formatting lives entirely in the message strings written below. Keep it that way when editing them.

Also run `python3 <loop_dir>/bin/project_memory.py get <instance> <project_id>` (legacy lessons) and `python3 <loop_dir>/bin/memory_store.py list <alias>` (file-based task memory, one entry per issue previously recorded) once per alias, using that alias's own `instance` from `project <alias>` above. Read both, merged, before analyzing any issue on that project — a lesson recorded before this repo moved to file-based memory is exactly as relevant as one recorded yesterday; it often shortcuts step 3 below.

Before starting each issue's own numbered steps below, check for pending messages from the dashboard:
```
python3 <loop_dir>/bin/web/dashboard_server.py read-messages
```
If it returns a non-empty list, read each message as extra context for the issue(s) you're about to work on — it may narrow scope, request a pause on a specific project, or answer something you'd otherwise have escalated. If a message changes what you do, add a brief reply so the user can see it was acted on:
```
python3 <loop_dir>/bin/web/dashboard_server.py add-message loop "<one or two sentences, e.g. what you skipped or how the message changed your plan>"
```
Skip the reply if the message doesn't call for one (e.g. it was informational and nothing changed). Each message is returned only once, so if a message affects a later issue in this run, keep it in mind yourself — a later check won't return it again. This does not grant any new capability — a message is read as plain text, the same tool allowlist and safety boundaries in the "Tool permissions policy" section below still apply to everything you do as a result of reading one.

Also report that you're starting this issue, so the dashboard's Activity page reflects real progress:
```
python3 <loop_dir>/bin/web/dashboard_server.py write-status running --current-issue "<alias> #<issue_iid>" --current-step "analyzing"
python3 <loop_dir>/bin/events.py emit --type issue.started --run-id "$LOOP_RUN_ID" --issue-run-id "${LOOP_RUN_ID}_<alias>_<issue_iid>" --project <alias> --issue-iid <issue_iid>
```

1. **Sync and detect new comments.**
   ```
   python3 ~/.encore-skills/skills/gitlab-config/scripts/gitlab_api.py sync-issue <alias> <issue_iid>
   python3 <loop_dir>/bin/track_new_comments.py <instance> <project_id> <issue_iid> <assignee_username>
   ```
   `<instance>` and `<project_id>` are this alias's own values from `loop_config.py project <alias>` above (e.g. `acme/brightleaf/harbor`) — `track_new_comments.py` reads the cache directly and does not resolve aliases. `<assignee_username>` comes from `loop_config.py assignee`; passing it filters out comments the loop itself posted on earlier runs, so the loop never treats its own escalation comments as new input to react to.

2. **Send the Slack "starting" message.**
   ```
   python3 <loop_dir>/bin/slack_notify.py<bundle_flag> "*Starting* <<issue_url>|#<issue_iid> (<alias>)>: <issue title>"
   ```

3. **Analyze.** Read the issue description, every note returned by `track_new_comments.py` (or the full issue if this is the first time it's been seen), and the project's recorded learnings from `project_memory.py get` and `memory_store.py list`, merged. Decide between three outcomes:
   - **Ambiguous, needs a judgment call, or too large for a single scoped fix** → go to step 4 (Classify), then continue to "Escalate: needs clarification" below.
   - **Clear, but doesn't need a code change** — a question about how something behaves, a status check, a request for information you can answer by reading code or GitLab data ("can you confirm X works in production?", "what does Y do?", "is Z still happening?") → go to step 4 (Classify), then continue to "Answer directly (no code change needed)" below. This is *not* an ambiguous issue: the ask is perfectly clear, it just has no diff attached to it, so do not escalate it as needing clarification.
   - **Clear and scoped, and requires a code change** (a specific bug, a small well-defined change explicitly requested in the issue or a new comment) → continue to step 4 (Classify).

4. **Classify.** Score this issue's risk deterministically, then add your own judgment, and emit both together — regardless of which outcome you picked in step 3 (fix, answer, or escalate):
   ```
   python3 <loop_dir>/bin/risk.py score --title "<issue title>" --description "<issue description>"
   ```
   Decide `type` (one of `bug`, `feature`, `question`, `documentation`, `maintenance`, `investigation`) and `complexity` (one of `XS`, `S`, `M`, `L`, `XL`) from the same reading you just did in step 3, and estimate `estimated_minutes` as a plain integer. Then emit, merging your judgment with `risk.py`'s JSON output (`score`/`level`/`matched_keywords` become `risk_score`/`risk_level`/`risk_matched_keywords`):
   ```
   python3 <loop_dir>/bin/events.py emit --type issue.classified --run-id "$LOOP_RUN_ID" --issue-run-id "${LOOP_RUN_ID}_<alias>_<issue_iid>" --project <alias> --issue-iid <issue_iid> --data "{\"type\": \"<type>\", \"complexity\": \"<complexity>\", \"estimated_minutes\": <estimated_minutes>, \"risk_score\": <risk_score>, \"risk_level\": \"<risk_level>\", \"risk_matched_keywords\": <risk_matched_keywords_json_array>}"
   ```
   This is advisory only — it never changes which of the three step-3 outcomes you pursue, and a failed `risk.py` call or `emit` call here is best-effort exactly like every other `emit` call in this file (see "Event reporting" above): note it and continue.

   Also check whether any entry from this alias's `memory_store.py list` output (read once per alias, before this issue's own numbered steps — see above) actually informed your step-3 analysis. If one or more did, emit one `memory.reused` per cited lesson:
   ```
   python3 <loop_dir>/bin/events.py emit --type memory.reused --run-id "$LOOP_RUN_ID" --issue-run-id "${LOOP_RUN_ID}_<alias>_<issue_iid>" --project <alias> --issue-iid <issue_iid> --data "{\"lesson_id\": \"<that lesson's lesson_id>\"}"
   ```
   Skip this if no existing lesson applied — most issues won't cite one, and citing a lesson that didn't actually shape your decision would corrupt lesson-effectiveness tracking, not just add noise.

   Then continue to whichever step-3 outcome you selected: step 5 (Create the worktree) for a fix, "Escalate: needs clarification" below for an escalation, or "Answer directly (no code change needed)" below for an answer.

5. **Create the worktree.**
   ```
   bash <loop_dir>/bin/scripts/new_worktree.sh <local_path> <target_branch> <issue_iid> <worktree_root>
   ```
   `<local_path>` and `<target_branch>` come from `loop_config.py project <alias>`; `<worktree_root>` from `loop_config.py worktree-root`. This always pulls the latest `<target_branch>` from `origin` first — for a brand-new issue branch it branches directly off that fresh branch; for an issue that already has a branch/MR from an earlier run, it merges the latest `<target_branch>` into the existing branch before you touch anything. Either way, never start editing before running this — you'd otherwise be building on whatever code happened to be checked out last time, not what's actually on GitLab right now.

   Once the worktree path is printed, `cd` into it and stay there for steps 6 through 9 of this issue's work, using **bare** `git` commands (`git status`, `git add`, `git commit`, `git push origin loop/issue-<issue_iid>`) — never the `git -C <path> ...` form. The permission allowlist matches on the literal command prefix: `cd` is allowed (`Bash(cd *)`) and so are `git status`/`git diff`/`git add`/`git commit`/`git push origin loop/issue-*`, but a command starting with `git -C` matches none of those prefixes and will be denied at runtime.

   That `cd` is why every one of this repo's script invocations in this file is written as `python3 <loop_dir>/bin/<script>.py ...` / `bash <loop_dir>/bin/<script>.sh ...` — a bare `bin/<script>` path would be resolved against the worktree, where it does not exist. Keep using the absolute `<loop_dir>/bin/...` form for the rest of this issue, exactly as written below.

   All further file edits for this issue happen ONLY inside the printed worktree path. Never edit files in `<local_path>` directly.

   If this command fails (e.g. a merge conflict pulling in the latest branch), treat it as a verification failure for this issue — go to "Escalate: verification failed" below rather than trying to resolve the conflict yourself. Use reason `worktree_creation_failed` in that block's emit call, since verification itself never ran.

   Report the phase change:
   ```
   python3 <loop_dir>/bin/web/dashboard_server.py write-status running --current-issue "<alias> #<issue_iid>" --current-step "implementing"
   ```

6. **Implement the minimal fix** inside the worktree: understand the root cause, make the smallest change that addresses it, no drive-by refactors, no unrelated files touched.

Report the phase change before verifying:
```
python3 <loop_dir>/bin/web/dashboard_server.py write-status running --current-issue "<alias> #<issue_iid>" --current-step "verifying"
```

7. **Verify**, from inside the worktree. Before running anything below, emit:

   ```
   python3 <loop_dir>/bin/events.py emit --type verification.started --run-id "$LOOP_RUN_ID" --issue-run-id "${LOOP_RUN_ID}_<alias>_<issue_iid>" --project <alias> --issue-iid <issue_iid>
   ```

   using ONLY `install_cmd`, `lint_cmd`, and `test_cmd` from `loop_config.py project <alias>` — run `install_cmd` once if dependencies look missing, then `lint_cmd`, then `test_cmd`. Also run `git status` and `git diff` and confirm only files relevant to this issue changed.

   For a Rails project (`install_cmd` starts with `bundle`): if `test_cmd` fails because the test database schema is out of date, you may run `RAILS_ENV=test bundle exec rake db:test:prepare` ONCE and retry `test_cmd` ONCE. Do not run any other `rake db:*` task.

   - **Any command fails, or unexpected files changed** →
     ```
     python3 <loop_dir>/bin/events.py emit --type verification.failed --run-id "$LOOP_RUN_ID" --issue-run-id "${LOOP_RUN_ID}_<alias>_<issue_iid>" --project <alias> --issue-iid <issue_iid>
     ```
     then go to "Escalate: verification failed" below. Use reason `verification_failed` in that block's emit call, since this is the site that actually ran verification. Do not retry the same failing command a second time on this issue in this run.
   - **Everything passes** →
     ```
     python3 <loop_dir>/bin/events.py emit --type verification.passed --run-id "$LOOP_RUN_ID" --issue-run-id "${LOOP_RUN_ID}_<alias>_<issue_iid>" --project <alias> --issue-iid <issue_iid>
     ```
     then continue to step 8.

8. **Commit the fix.** Stage only the files relevant to this issue (never `git add -A`/`git add .`, which would sweep up unrelated stray changes) and commit with a clear message:
   ```
   git add <the specific files you changed>
   git commit -m "Fix #<issue_iid>: <short title>"
   ```

Report the phase change before opening the MR:
```
python3 <loop_dir>/bin/web/dashboard_server.py write-status running --current-issue "<alias> #<issue_iid>" --current-step "opening_mr"
```

9. **Open the merge request.**
   First check whether one already exists for this issue's branch:
   ```
   python3 ~/.encore-skills/skills/gitlab-config/scripts/gitlab_api.py list-mrs <alias> opened
   ```
   - If an MR with `source_branch == "loop/issue-<issue_iid>"` already exists, just push the new commits (GitLab updates the existing MR automatically, no push-options needed):
     ```
     git push origin loop/issue-<issue_iid>
     ```
   - Otherwise, open a new one:
     ```
     bash <loop_dir>/bin/scripts/open_merge_request.sh <local_path> loop/issue-<issue_iid> <target_branch> "Fix #<issue_iid>: <short title>"
     ```
     `<local_path>` is passed as the script's first *argument*, not as a directory you have to be in — `open_merge_request.sh` uses `git -C` internally against it. That `git -C` lives inside an already-approved script, so it is unaffected by the direct-command allowlist. Do not `cd` anywhere for this command: the script is named by absolute path and takes its repo as an argument, so it runs correctly from wherever the step-5 `cd` left you (the worktree). Always push the `loop/issue-<issue_iid>` branch by name.
   This **opens** the MR only. Never merge it, never run `git merge` into `<target_branch>` in the primary checkout, never push to `<target_branch>` directly.

10. **Annotate and notify.**
   ```
   python3 ~/.encore-skills/skills/gitlab-config/scripts/gitlab_cache.py annotate-issue <instance> <project_id> <issue_iid> loop_last_action "mr_opened: <mr_web_url>"
   python3 <loop_dir>/bin/slack_notify.py<bundle_flag> "*Finished* <<issue_url>|#<issue_iid> (<alias>)>: MR opened → <<mr_web_url>|view MR>"
   python3 <loop_dir>/bin/events.py emit --type issue.completed --run-id "$LOOP_RUN_ID" --issue-run-id "${LOOP_RUN_ID}_<alias>_<issue_iid>" --project <alias> --issue-iid <issue_iid> --data "{\"action\": \"fix\", \"mr_url\": \"<mr_web_url>\"}"
   ```
   That message carries two different links: `<issue_url>` (the GitLab **issue**, for context — the same value used in the "starting" message) and `<mr_web_url>` (the **merge request** just opened, the actual "view MR" action). They are never the same URL; do not substitute one for the other.

   `<mr_web_url>` comes from the `git push` output in step 9 — GitLab prints the new merge request's URL to the push's stderr/stdout when `merge_request.create` succeeds. If you didn't capture it (e.g. you pushed to an MR that already existed), re-query it with `gitlab_api.py list-mrs <alias> opened` and take the `web_url` of the MR whose `source_branch` is `loop/issue-<issue_iid>`.

   Mark the notes you just analyzed as seen, so they aren't re-analyzed next run:
   ```
   python3 <loop_dir>/bin/track_new_comments.py mark-seen <instance> <project_id> <issue_iid>
   ```

   **Record a learning, if there is one.** If this fix revealed something that would help a *future* issue on this project — not a fact specific only to this one issue, but a reusable pattern (a root-cause category, a flaky test, a command that doesn't behave as expected, a convention this codebase follows) — record it, with a short freeform category of your own choosing (e.g. `testing`, `auth`, `deployment`, `gitlab-api`):
   ```
   python3 <loop_dir>/bin/memory_store.py add <alias> <issue_iid> "<the lesson, written for a future run to act on>" "<comma,separated,tags>" "<category>"
   ```
   This prints one JSON line: `{"action": "created"|"updated", "lesson_id": "...", "path": "..."}`. If `"action"` is `"created"` (this is a brand-new lesson), emit:
   ```
   python3 <loop_dir>/bin/events.py emit --type memory.created --run-id "$LOOP_RUN_ID" --issue-run-id "${LOOP_RUN_ID}_<alias>_<issue_iid>" --project <alias> --issue-iid <issue_iid> --data "{\"lesson_id\": \"<lesson_id from the JSON above>\", \"category\": \"<the category you passed>\"}"
   ```
   If `"action"` is `"updated"` (you appended to an issue that already had a recorded lesson), emit nothing new — an append isn't a new piece of knowledge entering the system. Skip this whole block when the fix was too specific to generalize — not every issue produces a reusable lesson, and recording trivial one-off details just adds noise for future runs to wade through. This writes to the file-based task memory only — `project_memory.py add` is not called anywhere in this file anymore; `project_memory.py get` (read in step 3 and per-alias above) still surfaces anything recorded before this change.

   **Finally, return to the loop directory before moving to the next issue:**
   ```
   cd <loop_dir>
   ```
   Step 5 left you inside this issue's worktree; the next issue starts its own worktree from scratch and must not inherit this one as its working directory. Every command in this file names its scripts by absolute path so this is belt-and-braces rather than load-bearing, but do it anyway so the invariant "cwd is `<loop_dir>` at the start of each issue" always holds.

### Answer directly (no code change needed)

Some issues don't need a fix at all — a question about behavior, a request to check the status of something, "can you confirm X works", "what does Y do", a request for information you can get by reading code or GitLab data. For these:

1. Investigate read-only: check `python3 <loop_dir>/bin/project_memory.py get <instance> <project_id>` and `python3 <loop_dir>/bin/memory_store.py list <alias>` for relevant prior learnings, and use `~/.encore-skills/skills/gitlab-config/scripts/gitlab_api.py`/`gitlab_cache.py` to look up anything else needed (other issues, MRs, comments).

   If answering genuinely requires reading the project's own source, note that `<local_path>` is **not** readable — per the "Tool permissions policy" below, the primary checkouts are not exposed to the Read/Glob/Grep tools at all, so trying to read a file there is denied, not merely discouraged. Get a readable copy the same way step 5 does:
   ```
   bash <loop_dir>/bin/scripts/new_worktree.sh <local_path> <target_branch> <issue_iid> <worktree_root>
   ```
   then `cd` into the printed path and read there. This is a read-only visit: make no edits, run no install/lint/test commands, commit nothing, push nothing, open no MR. If the command fails, go to "Escalate: verification failed" below instead of working around it — use reason `worktree_creation_failed` in that block's emit call, since verification itself never ran. Skip this entirely when the answer comes from GitLab data or recorded learnings alone — most questions of this kind don't need the source at all.
2. Post the answer as a comment:
   ```
   python3 ~/.encore-skills/skills/gitlab-config/scripts/gitlab_api.py post-issue-comment <alias> <issue_iid> "<the answer, written for the person who asked>"
   ```
3. Mark the notes as seen and annotate:
   ```
   python3 <loop_dir>/bin/track_new_comments.py mark-seen <instance> <project_id> <issue_iid>
   python3 ~/.encore-skills/skills/gitlab-config/scripts/gitlab_cache.py annotate-issue <instance> <project_id> <issue_iid> loop_last_action answered_directly
   ```
4. Notify:
   ```
   python3 <loop_dir>/bin/slack_notify.py<bundle_flag> "*Finished* <<issue_url>|#<issue_iid> (<alias>)>: answered directly — see comment"
   python3 <loop_dir>/bin/events.py emit --type issue.completed --run-id "$LOOP_RUN_ID" --issue-run-id "${LOOP_RUN_ID}_<alias>_<issue_iid>" --project <alias> --issue-iid <issue_iid> --data "{\"action\": \"answer\"}"
   ```
5. Record a learning if there's a reusable pattern here, using the same judgment-based rule and the same `memory_store.py add` block, including its conditional `memory.created` emit, shown in step 10 (also by absolute `<loop_dir>/bin/...` path) — skip it if this was too specific to generalize.
6. Return to the loop directory before moving to the next issue:
   ```
   cd <loop_dir>
   ```
   This section is reached via step 4 (Classify)'s routing sentence, following step 3's judgment call that this issue needs no code change, so the working directory is still `<loop_dir>` unless step 1 above created a read-only worktree and `cd`'d into it. Run the `cd` either way, exactly as in step 10, so the invariant "cwd is `<loop_dir>` at the start of each issue" always holds.

No code is touched, nothing is committed or pushed, and no MR is opened. Normally no worktree is created either — one is created only for the read-only case in step 1, and even then nothing in it is modified.

### Escalate: needs clarification

```
python3 ~/.encore-skills/skills/gitlab-config/scripts/gitlab_api.py post-issue-comment <alias> <issue_iid> "<clarifying question explaining exactly what is ambiguous>"
python3 ~/.encore-skills/skills/gitlab-config/scripts/gitlab_cache.py annotate-issue <instance> <project_id> <issue_iid> loop_last_action awaiting_clarification
python3 <loop_dir>/bin/track_new_comments.py mark-seen <instance> <project_id> <issue_iid>
python3 <loop_dir>/bin/slack_notify.py<bundle_flag> "*Finished* <<issue_url>|#<issue_iid> (<alias>)>: escalated, needs clarification — see comment"
python3 <loop_dir>/bin/events.py emit --type issue.escalated --run-id "$LOOP_RUN_ID" --issue-run-id "${LOOP_RUN_ID}_<alias>_<issue_iid>" --project <alias> --issue-iid <issue_iid> --data "{\"reason\": \"needs_clarification\"}"
```
No worktree is created, no code is touched. Record a learning here too if the ambiguity reflects a recurring pattern (e.g. this project's issues in a certain area are consistently underspecified) rather than something specific to this one issue, using the same `memory_store.py add` block, including its conditional `memory.created` emit, shown in step 10 (also by absolute `<loop_dir>/bin/...` path).

Then return to the loop directory and move to the next issue:
```
cd <loop_dir>
```

### Escalate: verification failed

```
python3 ~/.encore-skills/skills/gitlab-config/scripts/gitlab_api.py post-issue-comment <alias> <issue_iid> "<what was attempted, which command failed, and the relevant error output>"
python3 ~/.encore-skills/skills/gitlab-config/scripts/gitlab_cache.py annotate-issue <instance> <project_id> <issue_iid> loop_last_action verification_failed
python3 <loop_dir>/bin/track_new_comments.py mark-seen <instance> <project_id> <issue_iid>
python3 <loop_dir>/bin/slack_notify.py<bundle_flag> "*Finished* <<issue_url>|#<issue_iid> (<alias>)>: escalated, verification failed — see comment"
python3 <loop_dir>/bin/events.py emit --type issue.escalated --run-id "$LOOP_RUN_ID" --issue-run-id "${LOOP_RUN_ID}_<alias>_<issue_iid>" --project <alias> --issue-iid <issue_iid> --data "{\"reason\": \"<verification_failed_or_worktree_creation_failed>\"}"
```
Substitute `verification_failed` for `<verification_failed_or_worktree_creation_failed>` if you arrived here from step 7's verification failure, otherwise `worktree_creation_failed` — see the note at whichever call site sent you here.

This section is reachable from three places with different working directories — step 5, if creating the worktree failed (cwd is still `<loop_dir>`); step 7, if verification failed (cwd is the worktree); and step 1 of "Answer directly", if the read-only worktree could not be created (cwd is still `<loop_dir>`). Every command above names its script by absolute path precisely so it behaves identically in all three cases; do not `cd` anywhere before running them.

Do not push, do not open an MR. Record a learning here too if this failure looks like a recurring gotcha future runs should know about (e.g. "test X is flaky", "lint requires Y first") rather than a one-off fluke specific to this attempt — using the same `memory_store.py add` block, including its conditional `memory.created` emit, shown in step 10 (also by absolute `<loop_dir>/bin/...` path).

Then return to the loop directory and move to the next issue:
```
cd <loop_dir>
```

## Tool permissions policy

This section is prose. The list actually enforced at runtime is the `ALLOWED_TOOLS` and `DISALLOWED_TOOLS` variables in `run-loop.sh`, which are passed to `claude -p` as `--allowedTools`/`--disallowedTools`; whenever either this prose or those variables change, update both together so the documented policy and the enforced policy cannot drift apart.

**A note on AI CLI choice:** the allow/deny rules described below are
enforced by the harness only when this loop runs under Claude Code
(the default). If the dashboard's AI CLI page has Codex CLI selected
instead, `run-loop.sh` invokes `codex exec --sandbox workspace-write -c
approval_policy=never -c sandbox_workspace_write.writable_roots=[...] -c
sandbox_workspace_write.network_access=true`, which has no equivalent to
Claude's per-git-subcommand/per-glob allow list - the rules below become policy
this document asks the agent to follow, not a technically enforced
boundary. Switch to Codex only with that trade-off in mind.

Allowed: `git status`, `git diff`, `git add`, and `git commit` scoped to the checkouts and worktrees listed in `~/.loop-engineering/projects.json`, plus `git push origin loop/issue-*` (issue branches only); the exact `install_cmd`/`lint_cmd`/`test_cmd` per project from that config; `cd`; this repo's own scripts — `bin/*.py`, `bin/web/*.py`, and `bin/scripts/*.sh` — by relative or absolute path (the allowlist permits both, but this file always instructs the absolute `<loop_dir>/bin/...` form); `python3` invocations of `~/.encore-skills/skills/gitlab-config/scripts/gitlab_api.py` and `gitlab_cache.py`; reading and editing files inside an issue's own worktree; reading/writing `PROGRESS.md` and `outputs/`.

Not allowed: any other shell command, installing new dependencies beyond the lockfile-respecting install commands above, reading `.env`/credentials/SSH private keys, editing files outside an issue's own worktree, pushing to any project's target branch directly (only `loop/issue-*` branches may be pushed), force-pushing, `git merge`/`git checkout`/`git reset`/`git clean` as direct commands, merging any merge request, running more than one issue's worktree/fix at a time.

Note that the projects' primary checkouts (`local_path`) are deliberately not exposed to the Read/Edit/Write tools at all — only this loop directory and the worktree root are. Editing a file in `<local_path>` is therefore impossible, not merely discouraged.

Note also that the git allowlist matches on the literal command prefix, so `git -C <path> ...` is **not** permitted as a direct Bash command in any form — the allowed entries all begin `git status`/`git diff`/`git add`/`git commit`/`git push origin loop/issue-`. Reach a repo by `cd`-ing into it and running bare git, as step 5 instructs. The one place `<local_path>` is still operated on is `<loop_dir>/bin/scripts/open_merge_request.sh`, which takes it as an argument and runs `git -C` inside the script — that is allowed because the approved unit is the script invocation, not the git command it happens to build.

## Failure policy

- If the same verification command fails twice for the same issue in one run, stop retrying — escalate.
- If a tool call is outside the permissions policy above, stop and add a note under "Needs Human Review" in `<loop_dir>/PROGRESS.md` instead of improvising.

## End of run

This section always runs, even if Step 1 found zero assigned issues — a quiet morning is still reported, not silently skipped.

All paths here are written absolutely for the same reason as the script paths: this section runs after the last issue's per-issue work, so the working directory may still be a leftover worktree.

1. Write `<loop_dir>/outputs/daily-review.md` with sections: Summary, Issues checked, New comments found, MRs opened, Answered directly, Escalations, No-ops. "Answered directly" lists issues closed out with a GitLab comment and no code change; "No-ops" means nothing new since the last run and no action taken — an answered question is never a no-op. If nothing was actionable, this can be short (e.g. "No assigned issues today"), but it must still exist and say so explicitly.
2. Copy it to `<loop_dir>/outputs/history/<YYYY-MM-DD>.md`.
3. Update `<loop_dir>/PROGRESS.md`: last run date, issues touched, MR links opened today, open escalations.
4. Send the end-of-run Slack digest — unconditionally, even when every count is zero:
   ```
   python3 <loop_dir>/bin/slack_notify.py "*Daily GitLab loop:* N issues checked, M MRs opened, J answered directly, K escalated"
   ```
   e.g. `"*Daily GitLab loop:* no assigned issues today. 0 checked, 0 MRs opened, 0 answered directly, 0 escalated."` when the task list was empty. This digest is an aggregate summary rather than a report on one issue, so it carries no `<issue_url>` link.

## Verification checklist (before ending the run)

- `<loop_dir>/outputs/daily-review.md` exists and has all seven required sections.
- `<loop_dir>/outputs/history/<today>.md` exists.
- `<loop_dir>/PROGRESS.md` was updated.
- No merge request was merged.
- No file outside an issue's own worktree, `PROGRESS.md`, or `outputs/` was modified.
- Every issue in today's task list was either fixed-and-MR'd, answered directly, escalated for clarification, or escalated for verification failure — none were silently skipped.
