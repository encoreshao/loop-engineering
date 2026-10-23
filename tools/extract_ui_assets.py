#!/usr/bin/env python3
"""One-shot generator: dumps the Python dashboard's static UI constants and
f-string page templates into crates/server/assets/, where the Rust server
embeds them (include_bytes via crates/server/build.rs).

    python3 tools/extract_ui_assets.py            # regenerate crates/server/assets/
    python3 tools/extract_ui_assets.py --check    # fail if the checked-in files differ
    python3 tools/extract_ui_assets.py --out DIR  # write somewhere else (tests use this)

Why: bin/web/dashboard_server.py carries ~140 KB of CSS and ~100 KB of JS as
string constants and f-strings. Retyping them by hand would drift, so this
script imports the real module (in a throwaway HOME / LOOP_ENGINEERING_HOME /
LOOP_UPDATE_DIR / LOOP_EVENTS_DIR so nothing real is read) and writes:

  constants.json            every JSON-able module-level constant of the
                            modules in MODULES (tuples become lists, dict order
                            kept); strings of BLOB_THRESHOLD chars or more are
                            written as blobs/<module>/<NAME>.txt and referenced
                            from constants.json as {"$blob": "<path>"}
  blobs/<module>/<NAME>.txt big constants such as _STYLE (the whole stylesheet)
  templates/<name>.tpl      an f-string with every {expression} replaced by a
                            slot marker U+27E6 <python expression> U+27E7;
                            the literal text between slots is exactly what
                            Python would emit (the {{ }} escapes are resolved)
  templates/<name>.slots.txt  the distinct slot expressions of that template,
                            for people filling it in from Rust
  favicon.ico               assets/favicon.ico (served at /favicon.ico)

Which f-strings become templates is declared in TEMPLATES below and in any
tools/ui_assets_targets/*.py file (each defines a TEMPLATES list), so page
ports add their own file instead of editing this one.

A constant whose text contains an absolute path of this checkout or of the
sandbox is machine-specific and is skipped (listed in skipped.txt).
"""
import argparse
import ast
import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "crates" / "server" / "assets"
BLOB_THRESHOLD = 4096
# Per-process random values: never part of the checked-in assets.
VOLATILE = {"_CSRF_TOKEN"}
SLOT_OPEN, SLOT_CLOSE = "⟦", "⟧"

# Modules whose module-level constants are dumped. Module name -> file under bin/.
MODULES = {
    "dashboard_server": "bin/web/dashboard_server.py",
    "update_pages": "bin/web/update_pages.py",
    "brand_logos": "bin/web/brand_logos.py",
    "inbox_pages": "bin/web/inbox_pages.py",
    "i18n": "bin/i18n.py",
}

# (module, function name, template name[, options]). The template is the
# function's largest f-string unless `options` narrows it: a substring that the
# wanted f-string's first literal part must contain, or {"var": local variable
# name, "pick": "longest"|"shortest"} for a string assigned to a local (plain
# strings become slot-less templates).
TEMPLATES = [
    ("dashboard_server", "_render_shell", "shell"),
    ("dashboard_server", "_ai_panel_html", "ai_panel"),
    ("dashboard_server", "render_readme_page", "readme_page"),
]


def sandbox_env():
    """Point every state directory at a throwaway dir before anything imports."""
    scratch = Path(tempfile.mkdtemp(prefix="loopx-ui-assets-"))
    env = {
        "HOME": scratch / "home",
        "LOOP_ENGINEERING_HOME": scratch / "loop-home",
        "LOOP_UPDATE_DIR": scratch / "update",
        "LOOP_EVENTS_DIR": scratch / "events",
        "LOOP_EVALS_DIR": scratch / "evals",
    }
    for key, path in env.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)
    return scratch


def load_targets():
    templates = list(TEMPLATES)
    for path in sorted((ROOT / "tools" / "ui_assets_targets").glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"ui_assets_targets_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        templates.extend(getattr(module, "TEMPLATES", []))
    return templates


def jsonable(value):
    """The JSON form of a constant, or raise TypeError when it has none."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, (tuple, list)):
        return [jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(jsonable(v) for v in value)
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise TypeError("non-string dict key")
        return {k: jsonable(v) for k, v in value.items()}
    raise TypeError(type(value).__name__)


def contains_machine_path(value, needles):
    text = json.dumps(value, ensure_ascii=False)
    return any(n in text for n in needles)


def dump_constants(out, modules, needles):
    constants, skipped = {}, []
    for mod_name, module in modules.items():
        entries = {}
        for name, value in vars(module).items():
            if name.startswith("__") or name in VOLATILE or callable(value) or isinstance(value, type(sys)):
                continue
            try:
                data = jsonable(value)
            except (TypeError, RecursionError):
                continue
            if contains_machine_path(data, needles):
                skipped.append(f"{mod_name}.{name}")
                continue
            if isinstance(data, str) and len(data) >= BLOB_THRESHOLD:
                rel = f"blobs/{mod_name}/{name}.txt"
                write_text(out / rel, data)
                data = {"$blob": rel}
            entries[name] = data
        constants[mod_name] = entries
    write_text(out / "constants.json", json.dumps(constants, ensure_ascii=False, indent=1) + "\n")
    write_text(out / "skipped.txt", "".join(f"{s}\n" for s in sorted(skipped)))


def fstring_template(node):
    """(template text, ordered distinct slot expressions) for an ast.JoinedStr."""
    parts, slots = [], []
    for value in node.values:
        if isinstance(value, ast.Constant):
            text = value.value
            assert SLOT_OPEN not in text and SLOT_CLOSE not in text, "slot marker inside literal text"
            parts.append(text)
        else:
            assert isinstance(value, ast.FormattedValue)
            expr = ast.unparse(value.value)
            if value.conversion != -1:
                expr += "!" + chr(value.conversion)
            if value.format_spec is not None:
                spec = ast.unparse(value.format_spec)
                expr += ":" + spec
            if expr not in slots:
                slots.append(expr)
            parts.append(f"{SLOT_OPEN}{expr}{SLOT_CLOSE}")
    return "".join(parts), slots


def string_nodes(root):
    """Every f-string and plain string literal under `root`."""
    return [n for n in ast.walk(root)
            if isinstance(n, ast.JoinedStr) or (isinstance(n, ast.Constant) and isinstance(n.value, str))]


def node_size(node):
    """Characters of literal text (an f-string counts its slots as 50 each)."""
    if isinstance(node, ast.JoinedStr):
        return sum(len(v.value) if isinstance(v, ast.Constant) else 50 for v in node.values)
    return len(node.value)


def find_string(source, function, options):
    """The f-string (or plain string) a target names. `options` is None (the
    function's largest f-string), a substring the f-string's first literal must
    contain, or a dict: {"var": name, "pick": "longest"|"shortest"} for the
    string(s) assigned to a local variable."""
    if not isinstance(options, dict):
        options = {"contains": options}
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == function):
            continue
        if options.get("var"):
            assigns = [n for n in ast.walk(node) if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == options["var"] for t in n.targets)]
            candidates = [c for a in assigns for c in string_nodes(a.value)]
            candidates = [c for c in candidates if node_size(c) > 0]
            if not candidates:
                raise SystemExit(f"{function}: no string assigned to {options['var']}")
            pick = min if options.get("pick") == "shortest" else max
            return pick(candidates, key=node_size)
        candidates = [n for n in ast.walk(node) if isinstance(n, ast.JoinedStr)]
        selector = options.get("contains")
        if selector:
            candidates = [
                n for n in candidates
                if n.values and isinstance(n.values[0], ast.Constant) and selector in n.values[0].value
            ]
        if not candidates:
            raise SystemExit(f"{function}: no f-string found")
        pick = min if options.get("pick") == "shortest" else max
        return pick(candidates, key=lambda n: len(n.values))
    raise SystemExit(f"function {function} not found")


def as_template(node):
    if isinstance(node, ast.JoinedStr):
        return fstring_template(node)
    assert SLOT_OPEN not in node.value and SLOT_CLOSE not in node.value
    return node.value, []


def dump_templates(out):
    for target in load_targets():
        module, function, name = target[:3]
        options = target[3] if len(target) > 3 else None
        source = (ROOT / MODULES[module]).read_text(encoding="utf-8")
        text, slots = as_template(find_string(source, function, options))
        write_text(out / "templates" / f"{name}.tpl", text)
        listing = "".join(f"{expr}\n" for expr in slots)
        write_text(out / "templates" / f"{name}.slots.txt", listing)


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def generate(out):
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    scratch = sandbox_env()
    sys.path[:0] = [str(ROOT / "bin"), str(ROOT / "bin" / "web")]
    modules = {name: importlib.import_module(name) for name in MODULES}
    needles = [str(ROOT), str(scratch), os.path.realpath(scratch)]
    dump_constants(out, modules, needles)
    dump_templates(out)
    shutil.copyfile(ROOT / "assets" / "favicon.ico", out / "favicon.ico")
    shutil.rmtree(scratch, ignore_errors=True)


def tree(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in sorted(Path(path).rglob("*")) if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--check", action="store_true", help="compare a fresh run with --out instead of writing it")
    args = parser.parse_args()
    if not args.check:
        generate(args.out)
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "assets"
        generate(fresh)
        have, want = tree(args.out), tree(fresh)
    drift = sorted(k for k in set(have) | set(want) if have.get(k) != want.get(k))
    if drift:
        print("UI assets are stale; run tools/extract_ui_assets.py. Differing files:", *drift, sep="\n  ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
