#!/usr/bin/env python3
"""Route a loop's notification to the connectors listed in its `notify`
field (see loops_config), or to today's default Slack webhook
(slack_notify) when the loop lists none. Never raises: every target yields
one (connector_id, ok, message) result. Messages are machine/log status,
kept English, and never contain connector secrets."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mail_http  # noqa: E402

DEFAULT_ID = "slack-default"
# Connector types that render Slack mrkdwn links; others get 'label (url)'.
_SLACK_LINK_TYPES = frozenset({"slack", "webhook"})
_SLACK_LINK = re.compile(r"<(https?://[^|>\s]+)(?:\|([^>]*))?>")


def _flatten_links(text):
    """Slack `<url|label>` as plain 'label (url)' for chat services that
    would show the markup literally."""
    return _SLACK_LINK.sub(lambda m: f"{m.group(2)} ({m.group(1)})" if m.group(2) else m.group(1), text)


def _default_loop_lookup(name):
    import loops_config
    return loops_config.get_loop(name)


def _default_loader(account_id):
    import connectors_config
    return connectors_config.load_connector(account_id)


def _default_sender(text, blocks):
    import slack_notify
    slack_notify.post_message(text, blocks=blocks)


def _describe(exc):
    """Class name (or HTTP status) only - exception text can carry a webhook
    URL or token."""
    status = getattr(exc, "status", None)
    if isinstance(exc, mail_http.MailHTTPError) and isinstance(status, int):
        return f"HTTP {status}"
    return type(exc).__name__


def notify(loop_name, text, blocks=None, loop_lookup=None, loader=None, default_sender=None):
    if loop_lookup is None:
        loop_lookup = _default_loop_lookup
    if loader is None:
        loader = _default_loader
    if default_sender is None:
        default_sender = _default_sender
    try:
        ids = loop_lookup(loop_name).get("notify")
    except Exception:
        ids = None
    if not isinstance(ids, list) or not ids:
        try:
            default_sender(text, blocks)
            return [(DEFAULT_ID, True, "sent")]
        except Exception as exc:
            return [(DEFAULT_ID, False, _describe(exc))]

    results = []
    for account_id in ids:
        try:
            conn = loader(account_id)
            if "notify" not in conn.capabilities:
                results.append((account_id, False, "not a notify connector"))
                continue
            conn.send(text if getattr(conn, "type", "") in _SLACK_LINK_TYPES else _flatten_links(text),
                      blocks=blocks)
            results.append((account_id, True, "sent"))
        except KeyError:
            results.append((account_id, False, "unknown connector"))
        except Exception as exc:
            try:
                import connectors_config
                is_cfg = isinstance(exc, connectors_config.ConnectorConfigError)
            except Exception:
                is_cfg = False
            results.append((account_id, False, "connector config error" if is_cfg else _describe(exc)))
    return results


def main(argv=None, notify_fn=None):
    if argv is None:
        argv = sys.argv[1:]
    if notify_fn is None:
        notify_fn = notify
    if len(argv) != 2:
        print("Usage: notify.py <loop_name> <text>", file=sys.stderr)
        return 1
    results = notify_fn(argv[0], argv[1])
    for account_id, ok, message in results:
        print(f"{account_id}: {'ok' if ok else 'FAILED'}: {message}")
    return 0 if any(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
