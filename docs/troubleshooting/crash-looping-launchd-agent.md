# A `com.hermes.loop-engineering*` launchd agent keeps writing to its log

## Symptom

`~/.loop-engineering/outputs/history/*.log` (`dashboard.err.log`,
`launchd.err.log`) keeps growing — new lines keep appearing even though you
haven't touched the machine.

## Diagnosis

```bash
launchctl list | grep hermes
```

```
-  2  com.hermes.loop-engineering-dashboard
-  78 com.hermes.loop-engineering
```

The second column is the job's last exit code. `78` is `EX_CONFIG`
(misconfiguration); a non-zero code here means the job failed on its last
run. There are only two `com.hermes.loop-engineering*` daemons now — the
always-on dashboard, and the unified scheduler
(`bin/loop_scheduler.py`, see
`docs/superpowers/specs/2026-09-14-unified-loop-scheduler-design.md`) that
polls `~/.loop-engineering/loops.json` and runs whichever registered loop
(the GitLab issue loop, the topic monitor loop) is due. There is no
separate per-loop daemon anymore. `com.hermes.loop-engineering-dashboard`
is the one to watch closely: its plist sets `KeepAlive`, so if it exits at
all — crash or otherwise — launchd immediately relaunches it, and a broken
dashboard crash-loops forever, appending a fresh error line to
`dashboard.err.log` on every relaunch. `com.hermes.loop-engineering` is a
`StartInterval` poll loop instead (no `KeepAlive`), so a failure there
only writes once per poll (every 15 minutes by default), not continuously.

To confirm which one is actively looping and see the real error, tail its
stderr log and watch it grow, or inspect the job directly:

```bash
tail -f ~/.loop-engineering/outputs/history/dashboard.err.log

launchctl print gui/$(id -u)/com.hermes.loop-engineering-dashboard | grep -iE "state|last exit|program|stdout|stderr"
```

## Common root cause: `~/.loop-engineering` isn't actually a full clone

Both agents' plists (`launchd/*.plist.template`, rendered by
`bin/scripts/install.sh`) point `ProgramArguments`/`StandardOutPath`/
`StandardErrorPath` at paths under `~/.loop-engineering`. If that directory
was never fully populated by `install.sh` (e.g. it only contains an
`outputs/` folder, with no `bin/`), every one of these jobs fails
immediately:

```
python3: can't open file '/Users/encore/.loop-engineering/bin/web/dashboard_server.py': [Errno 2] No such file or directory
```

Check whether the install is actually there:

```bash
ls ~/.loop-engineering
# Expected: bin/, launchd/, config/, LOOPX_INSTRUCTIONS.md, outputs/, ...
# If you only see outputs/, the clone step never completed.
```

## Fix

**Option A — finish the install (recommended):** re-run the installer so
`~/.loop-engineering` becomes a real clone with the dashboard restarted on
the current code:

```bash
curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/install.sh | bash
```

If it's already a clone but just stale/broken, use `--upgrade` instead (see
`README.md`'s install section).

**Option B — stop the crash-loop without installing yet.** `kill`ing a PID
does nothing useful here — `KeepAlive` respawns it instantly. Unload the
job from launchd instead:

```bash
launchctl bootout gui/$(id -u)/com.hermes.loop-engineering-dashboard
launchctl bootout gui/$(id -u)/com.hermes.loop-engineering

# Verify:
launchctl list | grep hermes   # should print nothing
```

(`bootout` is the modern replacement for `launchctl unload` — use one or
the other, don't mix them on the same job.)

Reload later, once the install is fixed, with:

```bash
launchctl load -w ~/Library/LaunchAgents/com.hermes.loop-engineering-dashboard.plist
launchctl load -w ~/Library/LaunchAgents/com.hermes.loop-engineering.plist
```
