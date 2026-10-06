import mail_auth


def test_swallowed_keychain_hit_is_still_recorded(_no_real_keychain):
    """The guard's AssertionError can be swallowed by production
    `except Exception`; the recorder must still see the hit (the fixture's
    teardown fails the test if it is non-empty). This test provokes a hit on
    purpose, so it clears the recorder at the end to pass."""
    try:
        mail_auth._security(["find-generic-password", "-s", "x"])
    except Exception:
        pass
    assert len(_no_real_keychain) == 1
    _no_real_keychain.clear()
