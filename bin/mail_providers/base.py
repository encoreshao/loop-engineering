"""Shared plumbing for the Inbox Triage mail providers. A provider exposes
exactly PUBLIC_METHODS - nothing that could deliver mail."""
import mail_http

PUBLIC_METHODS = {"profile_address", "fetch_new", "ensure_labels", "apply_label", "create_reply_draft"}


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
