#!/usr/bin/env python3
"""Keychain storage for connector secrets (tokens, API keys, webhook URLs).
Separate Keychain service from the mailbox tokens so a connector delete can
never touch a mailbox token. Reuses mail_auth's hardened `security` calls
(secret on stdin, never argv) and its sandbox suffix."""
import mail_auth

SERVICE_BASE = "loop-engineering.connectors"
SecretStoreError = mail_auth.KeychainError


def _service():
    return mail_auth.sandboxed_service(SERVICE_BASE)


def get(ref):
    return mail_auth.keychain_get(ref, service=_service())


def put(ref, secret):
    mail_auth.keychain_set(ref, secret, service=_service())


def delete(ref):
    mail_auth.keychain_delete(ref, service=_service())
