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
`project_memory.py`, `memory_store.py`). `bin/web/` — the dashboard web server
(`dashboard_server.py`) alone. `bin/scripts/` — one-shot shell scripts
(`setup.sh`, `setup-nginx.sh`, `uninstall.sh`, `new_worktree.sh`,
`open_merge_request.sh`). Moving a script between these means updating, in
the same change: any `LOOP_DIR`/`sys.path` self-location math inside the
script itself (it's relative-path-depth-sensitive — see
`bin/web/dashboard_server.py`'s `LOOP_DIR` and its explicit `sys.path`
insert for `loop_config`/`project_memory`, and `bin/scripts/setup.sh`'s
`LOOP_DIR`), every hardcoded path to it in `LOOPX_INSTRUCTIONS.md`,
`run-loop.sh` (including its `--allowedTools` glob — a glob's `*` doesn't
cross a `/`, so each directory needs its own pattern), `README.md`, and any
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
`bin/memory_store.py`'s `DEFAULT_MEMORY_ROOT`, and
`bin/web/dashboard_server.py`'s `CUSTOM_INSTRUCTIONS_PATH`. Leave it
unset and every one of those falls back to the real path, which is exactly
why it must always be set before running anything in dev/verification.
Run this way, `dashboard_server.py` is a plain foreground process — no
`launchd`, no `KeepAlive` — kill it whenever you're done. Same idea for the
loop scripts themselves (`run-loop.sh`, `bin/*.py`): run them with
`LOOP_ENGINEERING_HOME` set to a scratch directory, never against the real
config, when the point is to exercise the code rather than actually act on
the user's real projects.

**`bin/scripts/*.sh` do NOT read `LOOP_ENGINEERING_HOME` at all** —
`install.sh`'s `DIR` and `uninstall.sh`'s `PROJECT_DIR` (which it
`rm -rf`s) both default straight to the real `$HOME/.loop-engineering`
unless you pass their own `--dir`/`--project-dir` flag explicitly. Setting
`LOOP_ENGINEERING_HOME` before running these does nothing to protect you.
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
launchctl kickstart -k gui/$(id -u)/com.hermes.loop-engineering-dashboard
sleep 1
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8420/<page>
```

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
