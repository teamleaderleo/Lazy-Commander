"""Explicit, read-only comparison of two settled command receipts."""

from __future__ import annotations

import difflib
import hashlib
import itertools
import json
from pathlib import Path

from semantic_command import VIEW_LIMIT, clean_lines, load_request


DIFF_BYTES_LIMIT = 256_000
DIFF_LINES_LIMIT = 2_000


def settled(identifier: str) -> tuple[Path, dict, dict]:
    directory, request = load_request(identifier)
    try:
        receipt = json.loads((directory / "receipt.json").read_text())
        if not (
            receipt["schema"] == "lazy-semantic-command-receipt/v1"
            and receipt["id"] == identifier
            and receipt["request_sha256"] == request["request_sha256"]
            and isinstance(receipt["cwd"], str)
            and type(receipt["exit_code"]) is int
            and type(receipt["output_limit_reached"]) is bool
        ):
            raise ValueError("invalid receipt")
        with (directory / "raw.log").open("rb"):
            pass
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit("comparison requires settled, valid receipts with raw output") from error
    return directory, request, receipt


def validate_baseline(identifier: str, command: str, cwd: str) -> None:
    """Reject unusable baselines before a caller executes a new command."""
    _directory, request, receipt = settled(identifier)
    if request["command"] != command or receipt["cwd"] != cwd:
        raise SystemExit("comparison requires the same command and execution directory")
    if receipt.get("output_limit_reached"):
        raise SystemExit("comparison baseline is incomplete: capture limit reached")


def captured(path: Path) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def compare(before: str, after: str, context: int = 3) -> int:
    """Compare evidence only; never execute a stored command or infer equivalence."""
    if not 0 <= context <= 20:
        raise SystemExit("context must be from 0 to 20")
    old_dir, old_request, old = settled(before)
    new_dir, new_request, new = settled(after)
    try:
        old_path, new_path = old_dir / "raw.log", new_dir / "raw.log"
        old_size, old_digest = captured(old_path)
        new_size, new_digest = captured(new_path)
    except (OSError, ValueError) as error:
        raise SystemExit("comparison requires two settled receipts with raw output") from error
    if (old_request["command"] != new_request["command"] or old["cwd"] != new["cwd"]):
        raise SystemExit("comparison requires the same command and execution directory")
    header = f"compare {before} -> {after} · exit {old['exit_code']} -> {new['exit_code']}"
    if old.get("output_limit_reached") or new.get("output_limit_reached"):
        raise SystemExit(header + "\ncomparison refused: capture limit reached; output is incomplete")
    if old_size == new_size and old_digest == new_digest:
        print(header + f" · output unchanged ({new_size} bytes)")
        return 0

    print(header + f" · output changed ({old_size} -> {new_size} bytes)")
    recovery = f"expand: lc s {after}"
    # Bound CPU and memory as well as model-visible text. SequenceMatcher can be
    # quadratic on repetitive input, so large logs use focused stored expansion.
    if max(old_size, new_size) > DIFF_BYTES_LIMIT:
        print(f"text diff skipped: capture exceeds {DIFF_BYTES_LIMIT} bytes\n" + recovery)
        return 0
    try:
        old_rows = clean_lines(old_path.read_bytes())
        new_rows = clean_lines(new_path.read_bytes())
    except OSError as error:
        raise SystemExit("comparison raw output became unavailable") from error
    if max(len(old_rows), len(new_rows)) > DIFF_LINES_LIMIT:
        print(f"text diff skipped: capture exceeds {DIFF_LINES_LIMIT} lines\n" + recovery)
        return 0
    if old_rows == new_rows:
        print("raw bytes changed; terminal-normalized text is unchanged")
        for identifier in (before, after):
            print(f"raw: lc s -r {identifier} --offset 0 --limit 7000")
        return 0
    budget = VIEW_LIMIT - len(header) - len(recovery) - 300
    used = 0
    diff = difflib.unified_diff(
        old_rows, new_rows, fromfile=before, tofile=after, n=context, lineterm=""
    )
    # The local compare header already names both sides and their exit statuses.
    for row in itertools.islice(diff, 2, None):
        if used + len(row) + 1 > budget:
            print("[diff truncated at a complete line; expand stored output for remaining changes]")
            print(f"raw: lc s -r {after} --offset 0 --limit 7000")
            break
        print(row)
        used += len(row) + 1
    return 0
