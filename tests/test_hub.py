import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin" / "web"))
import hub


def _views():
    return (
        hub.HubView("a", "Alpha", lambda **kw: "<p>A</p>"),
        hub.HubView("b", "Beta", lambda **kw: "<p>B</p>", refresh=True),
    )


def _hub():
    return hub.Hub("demo", "/demo", "Demo", "<i></i>", _views())


def test_resolve_view_returns_requested():
    assert hub.resolve_view(_hub(), "b").key == "b"


def test_resolve_view_none_returns_first():
    assert hub.resolve_view(_hub(), None).key == "a"


def test_hub_unknown_view_falls_back_to_first():
    assert hub.resolve_view(_hub(), "nope").key == "a"


def test_tab_strip_marks_active_and_links_with_view_param():
    out = hub.hub_tab_strip_html("/demo", _views(), "b", translate=lambda s: s)
    assert "href='/demo?view=a'" in out
    assert "href='/demo?view=b'" in out
    assert "aria-current='page'" in out.split("view=b")[1].split("</a>")[0]


def test_tab_strip_escapes_translated_label():
    views = (
        hub.HubView("a", "Alpha", lambda **kw: ""),
        hub.HubView("x", "L'été", lambda **kw: ""),
    )
    out = hub.hub_tab_strip_html("/demo", views, "x", translate=lambda s: s)
    assert "L&#x27;été" in out


def test_tab_strip_single_view_renders_nothing():
    views = (hub.HubView("x", "Only", lambda **kw: ""),)
    assert hub.hub_tab_strip_html("/demo", views, "x", translate=lambda s: s) == ""


def test_tab_strip_nav_has_aria_label_not_tablist_role():
    out = hub.hub_tab_strip_html("/demo", _views(), "a", translate=lambda s: "T:" + s)
    assert "role='tablist'" not in out
    assert "aria-label='T:Page sections'" in out
