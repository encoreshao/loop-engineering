#!/usr/bin/env python3
"""Extract AI usage/cost from this loop's own CLI invocation and compute
cost metrics from the event log (outputs/events/*.jsonl) - see
docs/superpowers/specs/2026-09-04-cost-tracking-design.md. Claude only:
`claude -p --output-format json` returns a precise total_cost_usd and full
token breakdown in one JSON object; Codex's success-path usage schema is
unverified and OpenAI doesn't hand back a ready dollar cost anyway, so
usage extraction is skipped entirely when the configured AI CLI is Codex -
see the spec's "Investigation" section for why."""
import json
import sys


def extract_claude_usage(parsed_json):
    """Pull {"provider", "model", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens", "duration_ms", "cost_usd"}
    out of a parsed `claude -p --output-format json` result dict. Returns
    None if `parsed_json` doesn't look like real Claude JSON (missing
    "total_cost_usd" or "usage" entirely) - this is the signal callers use
    to skip emitting usage data rather than emit a partial/guessed dict.
    `model` is the single key of "modelUsage" when there's exactly one
    (the common case); None if "modelUsage" is missing, empty, or has more
    than one key (a multi-model session - rare, not worth guessing which
    one to report)."""
    if "total_cost_usd" not in parsed_json or "usage" not in parsed_json:
        return None

    usage = parsed_json["usage"]
    model_usage = parsed_json.get("modelUsage") or {}
    model = next(iter(model_usage)) if len(model_usage) == 1 else None

    return {
        "provider": "anthropic",
        "model": model,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "cache_write_tokens": usage.get("cache_creation_input_tokens"),
        "duration_ms": parsed_json.get("duration_ms"),
        "cost_usd": parsed_json["total_cost_usd"],
    }


def extract_result_text(parsed_json):
    """Return parsed_json["result"] if present and a string, else a fixed
    placeholder - never raises, never returns None."""
    result = parsed_json.get("result")
    return result if isinstance(result, str) else "(no result text in CLI output)"


def _read_cli_output_file(path):
    """Read the file at `path` and try to parse it as JSON. Returns
    (parsed_dict_or_None, raw_text) - parsed is None if the content isn't
    valid JSON at all (e.g. a Codex JSONL stream, or a truncated/corrupt
    file), or isn't a JSON object. Returns (None, "") if the file cannot be
    read (missing, permission denied, etc.)."""
    try:
        with open(path) as f:
            raw_text = f.read()
    except OSError:
        return None, ""
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return None, raw_text
    if not isinstance(parsed, dict):
        return None, raw_text
    return parsed, raw_text


def _parse_flag(argv, name):
    if name not in argv:
        return None
    idx = argv.index(name)
    if idx + 1 >= len(argv):
        return None
    return argv[idx + 1]


def _cmd_extract_result_text(argv):
    path = _parse_flag(argv, "--cli-output-file")
    if not path:
        print("extract-result-text: --cli-output-file is required", file=sys.stderr)
        return 1
    parsed, raw_text = _read_cli_output_file(path)
    if parsed is None:
        print(raw_text)
    else:
        print(extract_result_text(parsed))
    return 0


def _cmd_usage_json(argv):
    path = _parse_flag(argv, "--cli-output-file")
    if not path:
        print("usage-json: --cli-output-file is required", file=sys.stderr)
        return 1
    parsed, _raw_text = _read_cli_output_file(path)
    if parsed is None:
        print("usage-json: CLI output is not valid Claude JSON, skipping usage extraction", file=sys.stderr)
        return 0
    usage = extract_claude_usage(parsed)
    if usage is None:
        print("usage-json: CLI output is missing total_cost_usd/usage, skipping usage extraction", file=sys.stderr)
        return 0
    print(json.dumps(usage))
    return 0


def main():
    if len(sys.argv) < 2:
        print("Usage: cost.py extract-result-text --cli-output-file F | cost.py usage-json --cli-output-file F", file=sys.stderr)
        sys.exit(1)

    command, argv = sys.argv[1], sys.argv[2:]
    if command == "extract-result-text":
        sys.exit(_cmd_extract_result_text(argv))
    elif command == "usage-json":
        sys.exit(_cmd_usage_json(argv))
    else:
        print(f"Usage: unknown command '{command}' (expected extract-result-text|usage-json)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
