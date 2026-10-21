# tests/test_linkedin_session.py
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import linkedin_session as ls


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOP_ENGINEERING_HOME", str(tmp_path / "le"))
    return tmp_path / "le"


def test_profile_path_follows_loop_engineering_home(home):
    assert ls.profile_dir("li") == home / "linkedin" / "li" / "profile"


def test_profile_path_falls_back_to_default_home(monkeypatch, tmp_path):
    monkeypatch.delenv("LOOP_ENGINEERING_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ls.profile_dir("li") == tmp_path / ".loop-engineering" / "linkedin" / "li" / "profile"


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "UPPER", ".", "-x"])
def test_invalid_ids_are_rejected_before_any_filesystem_access(home, bad):
    for fn in (ls.profile_dir, ls.ensure_profile, ls.is_connected, ls.wipe, ls.mark_connected):
        with pytest.raises(ValueError):
            fn(bad)
    assert not (home / "linkedin").exists()


def test_ensure_profile_is_private(home):
    p = ls.ensure_profile("li")
    assert p.is_dir()
    for d in (home / "linkedin", home / "linkedin" / "li", p):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_connected_only_after_mark(home):
    assert not ls.is_connected("li")
    ls.ensure_profile("li")
    (ls.profile_dir("li") / "Cookies").write_text("x")          # profile files alone do not count
    assert not ls.is_connected("li")
    ls.mark_connected("li")
    assert ls.is_connected("li")


def test_wipe_removes_everything_and_is_idempotent(home):
    ls.ensure_profile("li"); ls.mark_connected("li")
    ls.wipe("li"); ls.wipe("li")
    assert not ls.is_connected("li") and not (home / "linkedin" / "li").exists()


def test_rename_moves_the_session(home):
    ls.ensure_profile("a"); ls.mark_connected("a")
    ls.rename("a", "b")
    assert ls.is_connected("b") and not ls.is_connected("a")
    ls.rename("missing", "c")                                      # nothing to move: no error
    assert not ls.is_connected("c")


def test_mark_disconnected_removes_only_the_marker_and_is_idempotent(home):
    ls.ensure_profile("li")
    (ls.profile_dir("li") / "Cookies").write_text("x")
    ls.mark_connected("li")
    ls.mark_disconnected("li")
    assert not ls.is_connected("li") and (ls.profile_dir("li") / "Cookies").exists()
    ls.mark_disconnected("li")
    ls.mark_disconnected("never-connected")
    with pytest.raises(ValueError):
        ls.mark_disconnected("../x")
