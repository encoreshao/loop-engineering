"""Inbox Triage mail providers - see
docs/superpowers/specs/2026-09-27-inbox-triage-design.md."""


def get_provider(inbox, access_token, refresh=None):
    from mail_providers.gmail import GmailProvider
    from mail_providers.outlook import OutlookProvider
    providers = {"gmail": GmailProvider, "outlook": OutlookProvider}
    cls = providers.get(inbox.get("provider"))
    if cls is None:
        raise ValueError(f"Unknown provider {inbox.get('provider')!r}")
    return cls(access_token, refresh=refresh, account=inbox.get("account", ""))
