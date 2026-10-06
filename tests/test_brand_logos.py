import re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin" / "web"))
import brand_logos as bl

REQUIRED = {"gitlab", "github", "slack", "feishu", "dingtalk", "wecom", "microsoftteams", "discord",
            "googlechat", "telegram", "notion", "jira", "linear", "rss", "gmail", "microsoftoutlook", "webhook"}


def test_every_required_brand_renders_svg_or_lettermark():
    for key in REQUIRED:
        out = bl.brand_logo_svg(key, label=key.title())
        assert out.startswith("<svg") or "brand-lettermark" in out, key
        assert "aria-hidden='true'" in out


def test_known_logo_is_static_svg_path():
    out = bl.brand_logo_svg("gitlab")
    assert re.match(r"^<svg class='brand-logo' viewBox='0 0 24 24' width='28' height='28' fill='(#[0-9A-Fa-f]{6}|currentColor)' aria-hidden='true' focusable='false'><path d='[^'<>]+'/></svg>$", out)


def test_logo_lookup_ignores_unknown_and_escapes_lettermark():
    out = bl.brand_logo_svg("nope", label="<b>Evil</b> Co")
    assert "<b>" not in out and "brand-lettermark" in out


def test_size_and_class_are_sanitized():
    out = bl.brand_logo_svg("gitlab", size="9999' onload='x", cls="x' onload='y")
    assert "onload" not in out and "width='96'" in out and "class='brand-logo'" in out


def test_paths_contain_only_path_characters():
    for key, (d, color) in bl.LOGOS.items():
        assert re.fullmatch(r"[0-9MmLlHhVvCcSsQqTtAaZzEe.,\- ]+", d), key
        assert re.fullmatch(r"#[0-9A-Fa-f]{6}", color), key


def test_module_records_source_and_license():
    assert "Simple Icons" in bl.__doc__ and "CC0" in bl.__doc__


def test_dark_brands_use_current_color_and_lettermark_has_brand_var():
    assert "fill='currentColor'" in bl.brand_logo_svg("github")
    assert "fill='currentColor'" in bl.brand_logo_svg("notion")
    out = bl.brand_logo_svg("webhook", label="Web Hook")
    assert "--brand:#" in out and ">WH<" in out


def test_lettermark_size_matches_svg_box():
    out = bl.brand_logo_svg("webhook", label="Web Hook", size=32)
    assert "width:32px" in out and "height:32px" in out
    clamped = bl.brand_logo_svg("webhook", label="Web Hook", size="9999")
    assert "width:96px" in clamped and "height:96px" in clamped


def test_class_with_trailing_newline_falls_back():
    out = bl.brand_logo_svg("gitlab", cls="brand-logo\n")
    assert "class='brand-logo'" in out and "\n" not in out
    out = bl.brand_logo_svg("webhook", cls="x\n")
    assert "class='brand-logo brand-lettermark'" in out
