#!/usr/bin/env python3
"""Run the Python dashboard against a sandboxed fixture checkout with a frozen
clock and a pinned CSRF token, either as a server or to dump render_shell variants.

    launch_python.py serve <checkout> <port>
    launch_python.py shell <checkout> <variants.json> <outdir>

Only ever called by capture.py with a scratch checkout (a copy of bin/ plus the
fixture), never with the real repo. Refuses to run if the module resolved its
LOOP_DIR anywhere but the given checkout.
"""
import datetime as _dt
import json
import sys
import time as _time
from pathlib import Path

FIXED_NOW = _dt.datetime.fromisoformat((Path(__file__).parent / "clock.txt").read_text().strip().replace("Z", "+00:00"))
CSRF = "golden-csrf-token"


class _FixedDatetime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        base = FIXED_NOW if tz is None else FIXED_NOW.astimezone(tz)
        return cls(*base.timetuple()[:6], base.microsecond, base.tzinfo if tz else None)

    @classmethod
    def utcnow(cls):
        return cls(*FIXED_NOW.timetuple()[:6])


_dt.datetime = _FixedDatetime
_time.time = lambda: FIXED_NOW.timestamp()


def load(checkout):
    checkout = Path(checkout).resolve()
    sys.path[:0] = [str(checkout / "bin"), str(checkout / "bin" / "web")]
    import dashboard_server as ds
    assert Path(ds.LOOP_DIR).resolve() == checkout, f"LOOP_DIR {ds.LOOP_DIR} is not the sandbox {checkout}"
    ds._CSRF_TOKEN = CSRF
    # Settings: never probe a login shell or run launchctl in the sandbox. The AI CLI
    # `claude` counts as installed (codex not); `launchctl list` is the fixture file
    # <checkout>/outputs/launchctl_list.txt (UiState::explicit pins the same in Rust).
    ds._cli_available = lambda name, *a, **k: name == "claude"
    list_file = checkout / "outputs" / "launchctl_list.txt"
    _orig_loaded = ds._loaded_by_label
    ds._loaded_by_label = lambda launchctl_output=None: _orig_loaded(
        list_file.read_text() if list_file.exists() else "")
    return ds


def main():
    mode, checkout = sys.argv[1], sys.argv[2]
    ds = load(checkout)
    if mode == "serve":
        server = ds.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[3])), ds.DashboardHandler)
        server.serve_forever()
    elif mode == "shell":
        out = Path(sys.argv[4])
        out.mkdir(parents=True, exist_ok=True)
        for variant in json.loads(Path(sys.argv[3]).read_text())["variants"]:
            for lang in ("en", "ja", "zh", "fr"):
                ds.i18n.set_language(lang)
                badge = ds._status_badge_markup(variant["status"])
                html = ds._render_shell(variant["title"], variant["active_page"], badge, variant["body"],
                                        refresh=variant.get("refresh", False),
                                        refresh_note=variant.get("refresh_note", False),
                                        lazy_refresh=variant.get("lazy_refresh", False))
                (out / f"{variant['id']}.{lang}.html").write_text(html, encoding="utf-8")


main()
