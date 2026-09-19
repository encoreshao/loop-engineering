# Loop X Engineering — agent guide

Read [`README.md`](README.md) for what this project is. [`TASK.md`](TASK.md)
indexes every scheduled task this repo runs; read the task's own spec under
`docs/tasks/` (currently just
[`docs/tasks/gitlab-issue-loop.md`](docs/tasks/gitlab-issue-loop.md)) and
[`LOOPX_INSTRUCTIONS.md`](LOOPX_INSTRUCTIONS.md) before touching the loop's
own decision logic — those are the loop's actual spec, not this file. This
file is operational conventions learned the hard way while building the
dashboard and tooling around it; follow them without being asked.

## `bin/` is split by kind, not flat

`bin/*.py` — the loop's own small Python CLI helpers (`loop_config.py`,
`slack_notify.py`, `list_assigned_issues.py`, `track_new_comments.py`,
`project_memory.py`, `memory_store.py`, `loop_scheduler.py`,
`loops_config.py`). `bin/web/` — the dashboard web server
(`dashboard_server.py`) alone. `bin/scripts/` — one-shot shell scripts
(`setup.sh`, `setup-nginx.sh`, `uninstall.sh`, `new_worktree.sh`,
`open_merge_request.sh`). Moving a script between these means updating, in
the same change: any `LOOP_DIR`/`sys.path` self-location math inside the
script itself (it's relative-path-depth-sensitive — see
`bin/web/dashboard_server.py`'s `LOOP_DIR` and its explicit `sys.path`
insert for `loop_config`/`project_memory`, and `bin/scripts/setup.sh`'s
`LOOP_DIR`), every hardcoded path to it in `LOOPX_INSTRUCTIONS.md`,
`run-loop-now.sh`, `bin/gitlab_loop_runner.py` (including its `_allowed_tools()`
glob — a glob's `*` doesn't cross a `/`, so each directory needs its own
pattern; this list lived in `run-loop.sh` as `ALLOWED_TOOLS` until the
per-issue runner moved it into Python), `README.md`, and any
installed `launchd/*.plist`'s absolute `Program`/`ProgramArguments` path
(both the source file here and the live copy in
`~/Library/LaunchAgents/`, which needs a real `launchctl unload`+`load`,
not just `kickstart -k`, since the plist's own path changed).

## Development mode: never touch the real `~/.loop-engineering` or real daemons

Day-to-day development and verification happens entirely inside this
checked-out repo directory, against a disposable sandbox — never against
the real `~/.loop-engineering` (this machine's live `projects.json`,
`topics.json`, `ai_cli.json`, `instructions.md`, run history) and never by
starting, stopping, or kickstarting the real installed `launchd` agents
(`com.hermes.loop-engineering*`). Those are live, personal, possibly-in-use
state; a dev/verification step has no business touching them.

To run a sandboxed dev instance of the dashboard from the current directory:

```bash
export LOOP_ENGINEERING_HOME=$(mktemp -d)
python3 bin/web/dashboard_server.py 18420 &   # any free port, never the live one
sleep 1
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:18420/<page>
kill %1
```

`LOOP_ENGINEERING_HOME` overrides the default `~/.loop-engineering` base
directory everywhere it's resolved — `bin/loop_config.py`,
`bin/topic_config.py`, `bin/ai_cli_config.py`'s `DEFAULT_CONFIG_PATH`,
`bin/memory_store.py`'s `DEFAULT_MEMORY_ROOT`, `bin/loops_config.py`'s
`DEFAULT_CONFIG_PATH`, `bin/loop_scheduler.py`'s `DEFAULT_STATE_PATH`, and
`bin/web/dashboard_server.py`'s `CUSTOM_INSTRUCTIONS_PATH`. Leave it
unset and every one of those falls back to the real path, which is exactly
why it must always be set before running anything in dev/verification.
`bin/events.py` is a deliberate exception: it always writes to
`<repo_root>/outputs/events/` regardless of `LOOP_ENGINEERING_HOME`,
because events are per-checkout run history (same category as
`outputs/daily-review.md`/`outputs/history/`), not per-machine config
like `projects.json`.
Run this way, `dashboard_server.py` is a plain foreground process — no
`launchd`, no `KeepAlive` — kill it whenever you're done. Same idea for the
loop scripts themselves (`run-loop-now.sh`, `bin/*.py`): run them with
`LOOP_ENGINEERING_HOME` set to a scratch directory, never against the real
config, when the point is to exercise the code rather than actually act on
the user's real projects.

**`bin/scripts/*.sh` do NOT read `LOOP_ENGINEERING_HOME` at all** —
`install.sh`'s `DIR` and `uninstall.sh`'s `PROJECT_DIR` (which it
`rm -rf`s) both default straight to the real `$HOME/.loop-engineering`
unless you pass their own `--dir`/`--project-dir` flag explicitly. Setting
`LOOP_ENGINEERING_HOME` before running these does nothing to protect you.
This bit us for real twice: the second time, the actual command that
deleted the live `~/.loop-engineering` was the ordinary, CLAUDE.md-mandated
`python3 -m pytest tests/ -q` — several pre-existing `tests/test_uninstall.py`
cases invoked the real `bin/scripts/uninstall.sh` without `--project-dir`,
so on any machine that also has a real install, running the test suite
deleted it as a side effect. `uninstall.sh` now refuses to delete the
default `$HOME/.loop-engineering` path whenever it's invoked from a
*different* on-disk location than that path (a dev clone's own copy of the
script, run bare) and `--project-dir` wasn't passed explicitly — see the
`PROJECT_DIR_GIVEN`/`LOOP_DIR` check right before its `rm -rf`. That guard
is the real backstop; treat every test in `tests/test_uninstall.py` and
`tests/test_install.py` that invokes the real script as required to pass
an explicit `--project-dir`/`--dir` regardless — never rely on the guard
alone to justify a new test skipping it.
Never run `install.sh` or `uninstall.sh` bare for dev/verification — always
pass `--dir`/`--project-dir` (and `--launch-agents-dir` if it touches
launchd) pointed at a scratch directory, e.g.:

```bash
scratch=$(mktemp -d)
bin/scripts/uninstall.sh --project-dir "$scratch/.loop-engineering" \
  --launch-agents-dir "$scratch/LaunchAgents" --skip-nginx --skip-hosts --skip-service
```

The test suite already does this correctly (`tests/test_install.py`,
`tests/test_uninstall.py` always pass explicit `--dir`/`--project-dir`
under `tmp_path`) — the risk is only in ad-hoc manual runs of these
scripts outside pytest.

The one exception: confirming a reviewed, already-merged change is
actually live on this machine's real install. Do that only when explicitly
asked to — never as the routine way to check a change works — using the
real daemon:

```bash
bin/scripts/restart-daemons.sh
sleep 1
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8420/<page>
```

`restart-daemons.sh` wraps `launchctl kickstart -k gui/$(id -u)/<label>` for
every one of this repo's launchd agents that is currently loaded, restarting
only the dashboard by default — it never loads, enables, or removes an
agent, and it deliberately does **not** restart `com.hermes.loop-engineering`
(the unified scheduler) unless `--with-scheduler` is also passed, since
kickstarting the scheduler forces an immediate poll and, if any registered
loop is overdue, a real unscheduled run against live GitLab/Slack right now.
Pass `--with-scheduler` only when that's actually the point of the check.

(`8420` is `dashboard_server.py`'s own local-dev default. A machine installed via `bin/scripts/install.sh` may be running on a different port picked at first install — check the actual port in `launchd/com.hermes.loop-engineering-dashboard.plist`'s `ProgramArguments` before assuming 8420.) `dashboard_server.py` does **not** hot-reload, so this is the only way to see a code change reflected on the real daemon.

A passing test suite proves the Python logic is correct; a sandboxed dev
run proves the server behaves correctly end-to-end. Neither proves the
*real* daemon is serving the new code — only the exception above does, and
it's rare.

## Test-driven, no exceptions

Every script under `bin/` change gets a test in the matching
`tests/test_*.py` first — watch it fail for the right reason, then make it
pass. Run the full suite before considering anything done:

```bash
python3 -m pytest tests/ -q
```

Prefer real subprocesses/tmp dirs over mocks where practical (see
`tests/test_new_worktree.py`, which drives a real local git repo). Where a
test needs to fake something async or a background process
(`subprocess.Popen`, `launchctl`), monkeypatch that call directly rather
than mocking the function under test.

## Dependency injection, resolved at call time — not def time

Module-level constants (`STATUS_PATH`, `LAUNCHD_DIR`, `SKILLS_ROOT`, etc.)
are always passed as `None`-default function arguments and resolved
*inside the function body*:

```python
def read_status(status_path=None):
    if status_path is None:
        status_path = STATUS_PATH
    ...
```

Never `def read_status(status_path=STATUS_PATH)` — that default is bound
once at import time, so a test's `monkeypatch.setattr(ds, "STATUS_PATH",
tmp_path)` silently has no effect and the "unit test" can reach the real
repo's real files. This bit us once already (see
`_resolve_runner`'s docstring) — don't reintroduce it.

## Every state-changing route needs CSRF + a live-daemon check

Every `POST` handler in `do_POST` starts with `self._csrf_ok(body)` (403 via
`self._forbidden()` on failure) before doing anything else. The token is a
per-process secret embedded only in pages this server itself renders —
never weaken this to "POST-only," which is not a real CSRF defense (see
the comment above `do_POST`).

## Adding a Material Symbols icon

Roboto and the Material Symbols Outlined icon font are loaded from Google
Fonts at request time (`<link>` tags built in `_render_shell`), not
self-hosted — there's no local font file to regenerate. Adding a new icon
means adding its glyph name to `_MATERIAL_SYMBOLS_ICON_NAMES` in
`bin/web/dashboard_server.py`, keeping the list alphabetically sorted:
Google's `icon_names=` parameter subsets the served font to exactly that
list, so a glyph name used in markup but missing from this constant renders
as tofu/missing glyph.

## A loading spinner's motion is functional, not decorative

Purely decorative animations (`.pulse-dot`, the topbar progress sliver)
are gated behind `@media (prefers-reduced-motion: no-preference)`, and
should stay that way. A *loading* spinner is different: its motion is the
only signal a page is still working, so `.md-spinner`'s animation is
deliberately **not** gated — under `prefers-reduced-motion`, it would
otherwise look permanently frozen rather than just calmer. Don't move it
back inside that media block.

## Flexbox: watch for the shrink-and-clip trap

A flex child with `flex-shrink: 0` forces *all* the shrinkage onto its
sibling(s). If that sibling also has `overflow: hidden`, it can be crushed
to near-zero width and its content effectively disappears — even though
every `display`/visibility property on it is technically correct. This
bit the collapsed sidebar once (the brand icon vs. the toggle button
competing for a 64px rail); if a collapsed/narrow layout ever "loses" an
element that has `display` set correctly, check the flex-shrink math on
its container before touching `display` again.

## A bare `bundle exec rubocop` lint_cmd silently breaks under `worktree_root`

Any `worktree_root` nested under a dot-directory (the scaffolded default,
`~/.loop-engineering/worktrees`, is one) trips a real RuboCop quirk:
`TargetFinder#hidden_path?` (`lib/rubocop/target_finder.rb`) treats any
scan root whose path contains a hidden (dot-prefixed) directory component
as a "hidden path," and for that case only, skips every *file-level*
`AllCops: Exclude` entry — directory-level excludes like `vendor/**/*`
still get pruned normally, but per-file excludes (`Gemfile`, `Capfile`,
`bin/*`, a specific old migration) do not. So a project's own worktree,
scanned with a bare `bundle exec rubocop`, reports pre-existing "offenses"
in files the project explicitly excludes — offenses a normal checkout of
the same commit never shows, since it isn't under a dot-directory. This
showed up for real as a loop-posted GitLab comment second-guessing a lint
"baseline" that didn't actually exist; nothing was wrong with the target
project's code, config, or the loop's decision logic — only with how the
configured `lint_cmd` string happened to be phrased.

Fix it at the `lint_cmd` layer in `~/.loop-engineering/projects.json`, not
by moving `worktree_root`: pass an explicit path argument, e.g.
`bundle exec rubocop .` instead of bare `bundle exec rubocop`. An explicit
path argument makes `TargetFinder` take `process_explicit_path`/the
`target_files_in_dir(arg)` branch with a *relative* base dir ("."), which
isn't subject to `hidden_path?`, so per-file Excludes apply correctly
again — confirmed directly by running both forms back-to-back in the same
worktree and diffing the file counts and offenses. This isn't
rubocop-specific in principle (any tool with its own "am I scanning a
dotfiles-style hidden path" heuristic could do the same thing), so if a
future project's `lint_cmd` shows a similar "offenses only in the loop's
worktree, never locally" gap, suspect this same class of bug before
suspecting the project's config.

## An untracked config file can flip lint results, and RuboCop's cache won't notice

`new_worktree.sh` copies `.ruby-version` into every fresh worktree because
it's gitignored and `git worktree add` only checks out tracked files. The
same is true of `config/database.yml` (per-developer DB credentials) — and
for kurrant.web specifically, its absence doesn't just break
`bundle exec rspec` (`RuntimeError: Could not load database configuration`);
it also makes `bundle exec rubocop` report bogus offenses. rubocop-rails'
`Rails/BulkChangeTable` cop (`DatabaseTypeResolvable#database_from_yaml`)
reads `config/database.yml` to detect the DB adapter; when the file is
missing, the cop can't tell whether `bulk: true` even applies and quietly
disables itself — which makes every pre-existing
`# rubocop:disable Rails/BulkChangeTable` comment in old migrations look
like a `Lint/RedundantCopDisableDirective` offense. This looked exactly
like real, slowly-shrinking lint debt (a run once reported 42 offenses,
a later run 5) and got escalated as "please confirm whether this baseline
is acceptable" — but it was never project debt at all: the same commit,
rubocop'd from a normal checkout that has `database.yml`, reports zero
offenses. Confirmed directly: reproduced a worktree under a dot-directory
path with `database.yml` deliberately absent (42, then 5, offenses,
matching the real runs), then copied `database.yml` in and reran — same
result, still 5 — before realizing why: see the cache paragraph below.
`new_worktree.sh` now copies `config/database.yml` the same way it copies
`.ruby-version`, which fixes this at the source.

Don't stop at "copy the file," though — RuboCop's result cache
(`~/.cache/rubocop_cache` by default, global and shared across every
project and every worktree on the machine) keys each cached result on
`file path + file mode + effective-config signature + file content digest`
(`ResultCache#file_checksum`). None of that includes `config/database.yml`,
so a cop whose *result* depends on that file's presence (like
`Rails/BulkChangeTable` above) can get its wrong answer cached and keep
serving it forever, even after the missing file is fixed — confirmed by
rerunning the same worktree with `--cache false`: 0 offenses, immediately,
with no other change. Because a loop worktree's path is deterministic per
issue (`worktree_root/<repo>-issue-<iid>`), a run made **before** the
`database.yml` copy fix could have already poisoned the cache for that
exact path, and a later run — even after the fix — would keep reading the
stale cached offenses rather than recomputing. `kurrant.web`'s `lint_cmd`
in `~/.loop-engineering/projects.json` now passes `--cache false` for this
reason. Weigh this against the loop's runtime budget if a future project's
lint step is slow enough that full-project caching actually matters —
but for any cop (in any project) that reads external state outside what
RuboCop's own cache key covers, prefer disabling the cache over trying to
guess which cached entries are stale.

## Git hygiene for this repo

- `outputs/` (`daily-review.md`, `messages.json`, `history/*.md`), `.claude/`,
  and `.superpowers/` are gitignored — they're personal/live/session state,
  never project source. If you find real GitLab issue content or internal
  project names about to be committed, stop and ask; this repo has already
  had its history scrubbed once for exactly that (`git filter-repo --path
  outputs --invert-paths`).
- Never bake a personal username into a shared identifier (launchd labels
  are `com.hermes.*`, not `com.<developer>.*`). This doesn't apply to real
  OS paths like `/Users/<you>/...` — those aren't a style choice.
- Before any history-rewriting operation (`git filter-repo`, rebase across
  many commits), take a `git bundle create <path> --all` backup first, and
  copy any live untracked runtime files elsewhere — a rewrite's checkout
  step can otherwise wipe them from the working tree.
- Only commit when explicitly asked. Stage precisely the files the current
  task touched, not `-A`/`.` — this repo's own live run state
  (`PROGRESS.md`, `outputs/`) is often sitting modified in the working tree
  from the loop's own runs and isn't part of whatever you were just asked
  to do.
