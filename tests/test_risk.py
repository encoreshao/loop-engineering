import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import risk

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(args):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "risk.py"), *args],
        capture_output=True,
        text=True,
    )


def test_score_no_keywords_matched_is_zero_low():
    result = risk.score("Fix a typo in the README")

    assert result["score"] == 0
    assert result["level"] == "LOW"
    assert result["matched_keywords"] == []


def test_score_single_keyword_case_insensitive():
    result = risk.score("Add SECURITY headers to the login page")

    assert result["score"] == 30
    assert result["level"] == "MEDIUM"
    assert result["matched_keywords"] == ["security"]


def test_score_multiple_keywords_sum():
    result = risk.score("Payment authentication database migration for prod rollout")

    # payment +40, authentication +30, database migration +30 = 100
    assert result["score"] == 100
    assert result["level"] == "CRITICAL"
    assert set(result["matched_keywords"]) == {"payment", "authentication", "database migration"}


def test_score_clamps_negative_total_to_zero():
    result = risk.score("Update the documentation for a test-only change")

    # documentation -20, test-only change -10 = -30, clamped to 0
    assert result["score"] == 0
    assert result["level"] == "LOW"


def test_score_level_thresholds():
    assert risk._level_for(0) == "LOW"
    assert risk._level_for(29) == "LOW"
    assert risk._level_for(30) == "MEDIUM"
    assert risk._level_for(59) == "MEDIUM"
    assert risk._level_for(60) == "HIGH"
    assert risk._level_for(79) == "HIGH"
    assert risk._level_for(80) == "CRITICAL"


def test_cli_score_prints_json():
    result = _run_cli(["score", "--title", "Add authentication", "--description", "for the login flow"])

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["level"] == "MEDIUM"
    assert "authentication" in payload["matched_keywords"]


def test_cli_missing_title_errors():
    result = _run_cli(["score", "--description", "some text"])

    assert result.returncode != 0
    assert "--title" in result.stderr


def test_cli_missing_description_errors():
    result = _run_cli(["score", "--title", "some title"])

    assert result.returncode != 0
    assert "--description" in result.stderr
