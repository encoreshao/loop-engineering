#!/usr/bin/env python3
"""Deterministic keyword-based risk scorer for issue text - see
docs/superpowers/specs/2026-09-05-quality-risk-design.md. Pure and
disk-free like bin/health.py: score() takes a plain string, no event
log access, no LLM call. Risk is advisory only this sprint - nothing
here changes what the loop does with any issue; the score is
computed and reported alongside the agent's own type/complexity
judgment in an issue.classified event (see LOOPX_INSTRUCTIONS.md)."""
import json
import sys

RISK_KEYWORDS = {
    "database migration": 30,
    "authentication": 30,
    "security": 30,
    "production configuration": 20,
    "payment": 40,
    "dependency update": 20,
    "test-only change": -10,
    "documentation": -20,
}

_LEVEL_THRESHOLDS = (
    (80, "CRITICAL"),
    (60, "HIGH"),
    (30, "MEDIUM"),
    (0, "LOW"),
)


def _level_for(value):
    for threshold, level in _LEVEL_THRESHOLDS:
        if value >= threshold:
            return level
    return "LOW"  # unreachable: the last threshold is 0 and value is always >= 0


def score(text):
    """{"score": int, "level": "LOW"|"MEDIUM"|"HIGH"|"CRITICAL",
    "matched_keywords": [...]}. Case-insensitive substring match of
    `text` against RISK_KEYWORDS; score is the sum of every matched
    keyword's point value, clamped to a minimum of 0 (a documentation-
    only match should read as 0/LOW, not negative). matched_keywords
    lists only the keywords that actually matched, in RISK_KEYWORDS'
    definition order."""
    lowered = text.lower()
    matched = [keyword for keyword in RISK_KEYWORDS if keyword in lowered]
    raw_score = sum(RISK_KEYWORDS[keyword] for keyword in matched)
    clamped_score = max(0, raw_score)
    return {
        "score": clamped_score,
        "level": _level_for(clamped_score),
        "matched_keywords": matched,
    }


def _parse_flag(argv, name):
    if name not in argv:
        return None
    idx = argv.index(name)
    if idx + 1 >= len(argv):
        return None
    return argv[idx + 1]


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] != "score":
        print("Usage: risk.py score --title T --description D", file=sys.stderr)
        sys.exit(1)

    title = _parse_flag(argv, "--title")
    description = _parse_flag(argv, "--description")
    if title is None:
        print("risk.py score: --title is required", file=sys.stderr)
        sys.exit(1)
    if description is None:
        print("risk.py score: --description is required", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(score(f"{title} {description}")))


if __name__ == "__main__":
    main()
