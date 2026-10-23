#!/usr/bin/env python3
"""Capture the Python dashboard's pages as the golden oracle for the Rust port.

    python3 tests/golden_ui/capture.py [--only ID[,ID...]] [--list]

For every page in pages.json and every language it starts the Python dashboard
(launch_python.py) on a throwaway copy of bin/ plus tests/golden_ui/fixture
(+ the page's overlay), with its own HOME / LOOP_ENGINEERING_HOME /
LOOP_UPDATE_DIR / LOOP_EVENTS_DIR, a frozen clock and a pinned CSRF token, and
writes the responses under tests/golden_ui/expected/. Nothing real is touched.

Big <style>/<script> bodies are stored once in expected/_blobs/ and referenced
as <!--@blob:HASH--> so the checked-in oracle stays small; the Rust test expands
them again before comparing.
"""
import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
EXPECTED = HERE / "expected"
LANGS = ["en", "ja", "zh", "fr"]
BLOCK = re.compile(r"<(style|script)>(.*?)</\1>", re.S)
BLOB_MIN = 1024


def normalize(text):
    """The only normalization (mirrored in crates/server/tests/golden_ui.rs):
    the server's own port in 127.0.0.1:<port> URLs, and the sandbox home path
    (<scratch>/userhome becomes /USERHOME)."""
    text = re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:PORT", text)
    # The sandbox HOME (<scratch>/userhome) is random per run: Skills prints paths under it.
    return re.sub(r"[^\s<>\"'&]*/userhome", "/USERHOME", text)


def compact(text, blobs):
    def swap(m):
        inner = m.group(2)
        if len(inner) < BLOB_MIN:
            return m.group(0)
        digest = hashlib.sha256(inner.encode()).hexdigest()[:16]
        blobs[digest] = inner
        return f"<{m.group(1)}><!--@blob:{digest}--></{m.group(1)}>"
    return BLOCK.sub(swap, text)


def copy_tree(src, dst):
    if src.exists():
        shutil.copytree(src, dst, dirs_exist_ok=True)


def build_sandbox(scratch, overlay):
    fixture = HERE / "fixture"
    checkout, home, userhome, update = (scratch / n for n in ("checkout", "home", "userhome", "update"))
    shutil.copytree(ROOT / "bin", checkout / "bin", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (checkout / "assets").mkdir()
    shutil.copyfile(ROOT / "assets" / "favicon.ico", checkout / "assets" / "favicon.ico")
    for name, dst in (("checkout", checkout), ("home", home), ("userhome", userhome), ("update", update),
                      ("events", scratch / "events"), ("evals", scratch / "evals")):
        copy_tree(fixture / name, dst)
        if overlay:
            copy_tree(HERE / "overlays" / overlay / name, dst)
        dst.mkdir(parents=True, exist_ok=True)
    for name in ("events", "evals"):
        (scratch / name).mkdir(exist_ok=True)
    for path in scratch.rglob("*"):
        if path.is_file() and "bin" not in path.relative_to(scratch).parts[:2]:
            text = None
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                pass
            if text and "@PID@" in text:
                path.write_text(text.replace("@PID@", str(os.getpid())))
    clock = (HERE / "clock.txt").read_text().strip().replace("Z", "+00:00")
    from datetime import datetime
    stamp = datetime.fromisoformat(clock).timestamp() - 3600
    for path in list(checkout.rglob("*")) + list(home.rglob("*")) + list(update.rglob("*")):
        if path.is_file() and "bin" not in path.relative_to(scratch).parts[:2]:
            os.utime(path, (stamp, stamp))
    return checkout


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, overlay):
        self.scratch = Path(tempfile.mkdtemp(prefix="loopx-golden-"))
        checkout = build_sandbox(self.scratch, overlay)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.scratch / "userhome"),
            "LOOP_ENGINEERING_HOME": str(self.scratch / "home"),
            "LOOP_UPDATE_DIR": str(self.scratch / "update"),
            "LOOP_EVENTS_DIR": str(self.scratch / "events"),
            "LOOP_EVALS_DIR": str(self.scratch / "evals"),
            "TZ": "UTC",
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.port = free_port()
        self.proc = subprocess.Popen([sys.executable, str(HERE / "launch_python.py"), "serve", str(checkout), str(self.port)],
                                     env=env, cwd=checkout, stdout=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                if self.proc.poll() is not None:
                    raise SystemExit("the Python dashboard exited at startup")
                time.sleep(0.1)
        raise SystemExit("the Python dashboard did not start")

    def get(self, path, lang):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("GET", path, headers={"Cookie": f"loop_lang={lang}"})
        resp = conn.getresponse()
        body = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, headers, body

    def close(self):
        self.proc.terminate()
        self.proc.wait()
        shutil.rmtree(self.scratch, ignore_errors=True)


def dump_shell_variants():
    scratch = Path(tempfile.mkdtemp(prefix="loopx-golden-shell-"))
    try:
        checkout = build_sandbox(scratch, None)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(scratch / "userhome"),
               "LOOP_ENGINEERING_HOME": str(scratch / "home"), "LOOP_UPDATE_DIR": str(scratch / "update"),
               "LOOP_EVENTS_DIR": str(scratch / "events"), "LOOP_EVALS_DIR": str(scratch / "evals"),
               "TZ": "UTC", "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"}
        out = scratch / "shell"
        subprocess.run([sys.executable, str(HERE / "launch_python.py"), "shell", str(checkout),
                        str(HERE / "shell_variants.json"), str(out)], env=env, cwd=checkout, check=True)
        return {p.name: p.read_text(encoding="utf-8") for p in sorted(out.iterdir())}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def extension(content_type):
    if "html" in content_type:
        return "html"
    if "json" in content_type:
        return "json"
    if content_type.startswith("text/"):
        return "txt"
    return "bin"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="comma separated page ids (others keep their expected files)")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    pages = json.loads((HERE / "pages.json").read_text())["pages"]
    if args.list:
        for p in pages:
            print(p["id"], p["path"], p.get("overlay", ""))
        return
    only = set(args.only.split(",")) if args.only else None
    selected = [p for p in pages if only is None or p["id"] in only]
    manifest_path = EXPECTED / "manifest.json"
    old = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"pages": []}
    previous = {p["id"]: p for p in old["pages"]}
    blobs = {}
    if only is None:
        shutil.rmtree(EXPECTED, ignore_errors=True)
    (EXPECTED / "_blobs").mkdir(parents=True, exist_ok=True)
    results = {}
    by_overlay = {}
    for p in selected:
        by_overlay.setdefault(p.get("overlay"), []).append(p)
    for overlay, group in by_overlay.items():
        server = Server(overlay)
        try:
            for p in group:
                entry = {"id": p["id"], "path": p["path"], "overlay": p.get("overlay"), "results": {}}
                for lang in p.get("langs", LANGS):
                    status, headers, body = server.get(p["path"], lang)
                    ctype = headers.get("content-type", "")
                    rec = {"status": status, "content_type": ctype, "location": headers.get("location")}
                    if body:
                        ext = extension(ctype)
                        name = f"{lang}/{p['id']}.{ext}"
                        (EXPECTED / lang).mkdir(exist_ok=True)
                        if ext in ("html", "json", "txt"):
                            text = normalize(body.decode("utf-8"))
                            if ext == "html":
                                text = compact(text, blobs)
                            (EXPECTED / name).write_bytes(text.encode("utf-8"))
                        else:
                            (EXPECTED / name).write_bytes(body)
                        rec["file"] = name
                    entry["results"][lang] = rec
                results[p["id"]] = entry
        finally:
            server.close()
    for digest, text in blobs.items():
        (EXPECTED / "_blobs" / f"{digest}.txt").write_bytes(text.encode("utf-8"))
    merged = [results.get(p["id"]) or previous.get(p["id"]) for p in pages]
    merged = [m for m in merged if m]
    manifest = {"clock": (HERE / "clock.txt").read_text().strip(), "csrf_token": "golden-csrf-token", "pages": merged}
    if only is None:
        shell = dump_shell_variants()
        (EXPECTED / "shell").mkdir(exist_ok=True)
        shell_blobs = {}
        for name, text in shell.items():
            (EXPECTED / "shell" / name).write_bytes(compact(normalize(text), shell_blobs).encode("utf-8"))
        for digest, text in shell_blobs.items():
            (EXPECTED / "_blobs" / f"{digest}.txt").write_bytes(text.encode("utf-8"))
    manifest_path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")
    print(f"captured {sum(len(m['results']) for m in merged)} responses for {len(merged)} pages")


if __name__ == "__main__":
    main()
