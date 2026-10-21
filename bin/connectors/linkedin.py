#!/usr/bin/env python3
"""LinkedIn connector: the user's own signed-in browser session (no API token,
no password stored). The session is created by the dashboard's Connect
LinkedIn button (bin/linkedin_login.py) and used by the LinkedIn Autopilot loop."""
import i18n
from connectors import register
from connectors.base import Connector, SOCIAL


@register
class LinkedInConnector(Connector):
    type = "linkedin"
    label = "LinkedIn"
    icon = "hub"
    capabilities = frozenset({SOCIAL})
    fields = ()
    secret_label = None
    auth = "browser_session"
    brand = "linkedin"  # not in LOGOS (Simple Icons dropped it): renders as a lettermark
    category = "other"
    description = "Read your LinkedIn feed, notifications and messages through your own signed-in browser session."
    docs_url = "https://www.linkedin.com/help/linkedin"

    def test(self):
        import linkedin_browser
        import linkedin_session
        account_id = self.account.get("id", "")
        if not linkedin_session.is_connected(account_id):
            return False, i18n.t("Not connected - use Connect LinkedIn")
        try:
            with linkedin_browser.Session(account_id, headless=True) as session:
                session.session_ok()
        except linkedin_browser.BrowserUnavailable:
            return False, i18n.t("Playwright or Google Chrome is not available - install Playwright (python3 -m pip install playwright) and Google Chrome")
        except linkedin_browser.SessionExpired:
            return False, i18n.t("LinkedIn session expired - reconnect")
        except linkedin_browser.ChallengeError:
            return False, i18n.t("LinkedIn showed a security check - open LinkedIn in a normal browser, then reconnect")
        except Exception as exc:  # noqa: BLE001 - test() must never raise
            return False, i18n.t("Could not open Chrome ({kind})", kind=type(exc).__name__)
        return True, i18n.t("Signed in to LinkedIn")
