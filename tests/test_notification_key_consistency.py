import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin" / "web"))
import dashboard_server  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER_PATHS = [
    REPO_ROOT / "bin" / "gitlab_loop_runner.py",
    REPO_ROOT / "bin" / "topic_monitor_runner.py",
]

NOTIFICATION_KEY_LITERAL = re.compile(r'notification_key=["\']([a-z_]+)["\']')


def _notification_keys_used_in(path):
    return set(NOTIFICATION_KEY_LITERAL.findall(path.read_text()))


def test_every_runner_notification_key_is_registered_in_dashboard_server():
    used_keys = set()
    for path in RUNNER_PATHS:
        used_keys |= _notification_keys_used_in(path)

    assert used_keys, "expected at least one notification_key= call site in the runners"
    assert used_keys <= set(dashboard_server._BLOCK_TEMPLATE_NOTIFICATION_KEYS.keys())
