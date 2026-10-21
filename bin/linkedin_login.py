#!/usr/bin/env python3
"""Opens a visible Chrome window on LinkedIn's login page, using the
connector's persistent profile, and waits for the user to sign in (2FA
included). Spawned by the dashboard's Connect LinkedIn button.
Usage: linkedin_login.py <account_id>"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import connectors_config  # noqa: E402
import linkedin_browser  # noqa: E402
import linkedin_session  # noqa: E402

LOGIN_URL = "https://www.linkedin.com/login"
WAIT_MS = 600_000


def login(account_id, launcher=None):
    profile = linkedin_session.ensure_profile(account_id)
    try:
        pw, ctx = (launcher or linkedin_browser._playwright_launcher)(str(profile), False)
    except linkedin_browser.BrowserUnavailable:
        print(
            "Could not start Chrome - is Playwright installed "
            "(python3 -m pip install playwright) and Google Chrome available?",
            file=sys.stderr,
        )
        return False
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_URL)
        page.wait_for_url("**/feed/**", timeout=WAIT_MS)
        page.wait_for_timeout(3000)            # let the session cookies flush to the profile
        linkedin_session.mark_connected(account_id)
        return True
    except Exception:  # noqa: BLE001 - timeout, window closed, Chrome missing
        return False
    finally:
        for close in (ctx.close, pw.stop):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or not connectors_config.is_valid_id(argv[0]):
        print("usage: linkedin_login.py <account_id>", file=sys.stderr)
        return 2
    return 0 if login(argv[0]) else 1


if __name__ == "__main__":
    sys.exit(main())
