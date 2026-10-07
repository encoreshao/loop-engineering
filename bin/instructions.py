"""Built-in and custom agent instructions, one place for both.

Built-in instructions live in <repo>/instructions/<loop>.md. Custom ones live
under the per-machine home: a global ~/.loop-engineering/instructions.md that
applies to every loop (edited on the dashboard's Settings page), plus optional
~/.loop-engineering/instructions/<loop>.md files layered on top for one loop.
Custom text only ever adds to the built-in file, never replaces it."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOOPS = ("gitlab-issue", "topic-monitor", "inbox-triage")


def _home(home=None):
    if home is None:
        home = Path(os.environ.get("LOOP_ENGINEERING_HOME", str(Path.home() / ".loop-engineering")))
    return Path(home)


def _check(loop):
    if loop not in LOOPS:
        raise ValueError(f"unknown loop: {loop!r}")


def builtin_path(loop, repo_root=None):
    _check(loop)
    if repo_root is None:
        repo_root = REPO_ROOT
    return Path(repo_root) / "instructions" / f"{loop}.md"


def custom_paths(loop, home=None):
    """[global file, per-loop file], in the order they are applied."""
    _check(loop)
    home = _home(home)
    return [home / "instructions.md", home / "instructions" / f"{loop}.md"]


def read_custom(loop, home=None):
    parts = []
    for path in custom_paths(loop, home):
        try:
            text = path.read_text().strip()
        except OSError:
            continue
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def compose(loop, home=None, repo_root=None):
    text = builtin_path(loop, repo_root).read_text()
    custom = read_custom(loop, home)
    if custom:
        text = f"{text.rstrip()}\n\n## Custom instructions\n\nThe user's own instructions, on top of (never in place of) everything above:\n\n{custom}\n"
    return text
