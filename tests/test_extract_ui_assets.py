"""The checked-in crates/server/assets/ must be exactly what tools/extract_ui_assets.py
produces from bin/web/*.py today, so the Rust server never serves CSS/JS/templates
that drifted from the Python dashboard. Fix a failure by re-running the tool."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "extract_ui_assets.py"


def test_checked_in_ui_assets_match_a_fresh_extraction():
    result = subprocess.run([sys.executable, str(TOOL), "--check"], capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_extraction_is_deterministic(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    for out in (first, second):
        subprocess.run([sys.executable, str(TOOL), "--out", str(out)], check=True, cwd=ROOT)
    files = sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file())
    assert files == sorted(p.relative_to(second) for p in second.rglob("*") if p.is_file())
    for rel in files:
        assert (first / rel).read_bytes() == (second / rel).read_bytes(), rel


def test_extraction_leaves_no_machine_paths_behind():
    text = "".join(p.read_text(errors="ignore") for p in (ROOT / "crates/server/assets").rglob("*.json"))
    assert str(ROOT) not in text
