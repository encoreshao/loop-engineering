from mail_providers.base import BaseProvider


class OutlookProvider(BaseProvider):
    DEFAULT_BASE_URL = "https://graph.microsoft.com/v1.0"
