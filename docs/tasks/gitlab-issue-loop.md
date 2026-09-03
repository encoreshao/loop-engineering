# Daily GitLab Issue Loop

## Goal

Every weekday, check GitLab issues already assigned to the configured user on the configured GitLab instance, pick up anything new (a fresh issue or a new comment on one already being tracked), and then take exactly one of three actions per issue: implement a fix, verify it, and open a merge request when the ask is clear, scoped, and genuinely needs a code change; answer directly with a GitLab comment when the ask is clear but needs no code at all (a question, a status check, a request for information); or post a GitLab comment asking for clarification when the ask is ambiguous. Failing verification also gets a GitLab comment instead of a guess. The loop also gets more capable over time: reusable lessons learned per project (fix patterns, gotchas, root-cause categories) are recorded per issue as markdown task-memory files and read back on later runs — see `bin/memory_store.py` (entries recorded before this format existed are still read via `bin/project_memory.py`).

## Setup

New to this loop? Run `bin/scripts/setup.sh` once — it installs the `gitlab-config` skill this loop depends on (from [encore-skills](https://github.com/encoreshao/encore-skills)) and scaffolds `~/.loop-engineering/projects.json` from the template if you don't have one yet. The dashboard's Skills page (`/skills`) shows a live view of what's installed.

## Scope

Which GitLab projects to track, their local checkout paths, target branches, GitLab username, worktree scratch directory, and per-project install/lint/test commands are **not** hardcoded in this repo — they live in `~/.loop-engineering/projects.json`, since every team member running this loop has their own local checkout paths. See `config/projects.json.template` for the exact format, and `bin/loop_config.py` for how the loop reads it.

The loop never self-assigns issues — it only tracks issues already assigned to the configured `assignee_username`, on the projects listed in that config file.

## Expected output

Each run produces or updates:
- `outputs/daily-review.md` (and an archived copy under `outputs/history/`)
- `PROGRESS.md`
- Slack messages via the webhook at `~/.slack/config.json` (per-issue start/finish + one end-of-run digest, sent every run — including mornings with no assigned issues at all)
- Zero or more GitLab comments and zero or more opened merge requests

## Safety boundary (fixed — does not loosen with time or repeated success)

- **Never merge a merge request.** The loop's job ends at "MR opened, verification passing." Merging is always a manual step for the human.
- Every code change happens inside an isolated git worktree, under the configured `worktree_root`, on a branch named `loop/issue-<iid>`, never on the checkout's target branch.
- An MR is only opened if the project's own configured `test_cmd`/`lint_cmd` pass, and the diff only touches files relevant to the issue.
- Only the command allow-list in `LOOPX_INSTRUCTIONS.md` may run — no arbitrary shell, no dependency upgrades, no reading `.env`/credentials/SSH keys.
- Issues are processed one at a time, sequentially — never multiple worktrees/fixes in parallel in the same run.
- The same verification failure on the same issue is not retried within a run — it escalates via a GitLab comment instead.
- The loop only touches: issues/comments/MRs on the projects listed in `~/.loop-engineering/projects.json`, its own git worktrees (under the configured `worktree_root`), and its own state files (`PROGRESS.md`, `outputs/`).
