#!/usr/bin/env python3
"""Connector type registry."""
from connectors import base  # noqa: F401  (re-export for callers)

CONNECTOR_TYPES = {}


def register(cls):
    CONNECTOR_TYPES[cls.type] = cls
    return cls


def get_type(name):
    _load_all()
    return CONNECTOR_TYPES[name]


def _load_all():
    # Imported lazily so `import connectors` stays cheap and cycle-free.
    from connectors import (  # noqa: F401
        github, gitlab, jira, linear, mailbox, rss, slack, webhook,
    )
