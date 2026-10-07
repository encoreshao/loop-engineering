## What changed and why

<!-- A short description of the change and the problem it solves. -->

## Verification

- [ ] `python3 -m pytest tests/ -q` passes
- [ ] `CHANGELOG.md` has a line under `[Unreleased]` (user-visible changes only)

## Golden eval (agent instruction/tool changes only)

If this PR changes `LOOPX_INSTRUCTIONS.md`, `TOPIC_MONITOR_INSTRUCTIONS.md`,
`INBOX_TRIAGE_INSTRUCTIONS.md`, `loops/*/prompt.md`,
`bin/scripts/build_run_prompt.sh`, or any `_allowed_tools()`, paste the
`python3 bin/loop_cli.py eval --golden` summary from the base commit and
from this branch:

<details><summary>Before</summary>

```
(paste summary)
```

</details>

<details><summary>After</summary>

```
(paste summary)
```

</details>
