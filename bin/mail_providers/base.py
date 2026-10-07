"""Shared plumbing for the Inbox Triage mail providers. A provider exposes
exactly PUBLIC_METHODS - nothing that could deliver mail."""
import re

import mail_http

PUBLIC_METHODS = {"profile_address", "fetch_new", "ensure_labels", "apply_label", "create_reply_draft",
                  "search_recent"}

_PLAIN_ADDRESS = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
MAX_SEARCH_ADDRESSES = 10


def plain_addresses(addresses):
    """Lower-cased, de-duplicated plain email addresses, at most
    MAX_SEARCH_ADDRESSES. Anything else is dropped: these come from calendar
    invites (any outsider can create one) and are pasted into a search query."""
    out = []
    for address in addresses or []:
        address = str(address).strip().lower()
        if _PLAIN_ADDRESS.match(address) and address not in out:
            out.append(address)
    return out[:MAX_SEARCH_ADDRESSES]


class BaseProvider:
    DEFAULT_BASE_URL = ""

    def __init__(self, access_token, refresh=None, base_url=None, account=""):
        self._token = access_token
        self._refresh = refresh
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._account = account

    def _call(self, method, path, **kwargs):
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        try:
            return mail_http.request_json(method, url, token=self._token, **kwargs)
        except mail_http.AuthExpired:
            if self._refresh is None:
                raise
            self._token = self._refresh()
            return mail_http.request_json(method, url, token=self._token, **kwargs)
