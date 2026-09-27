#!/usr/bin/env python3
"""Run one argv-safe command and retain a bounded semantic output view."""

from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
from collections import deque
from pathlib import Path


ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
SIGNAL = re.compile(
    r"\b(?:error|errors|fail|failed|failure|warning|warnings|traceback|"
    r"assertionerror|exception|expected|actual|passed|tests?|summary|digest|sha256|"
    r"complete|completed|success|succeeded)\b",
    re.IGNORECASE,
)
SUCCESS_LINE = re.compile(r"^(?:OK|PASS|PASSED|PASSING)$", re.IGNORECASE)


def exclusive(path: Path, *, binary: bool):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "wb" if binary else "w", encoding=None if binary else "utf-8")


def positive(value: str, *, minimum: int, maximum: int, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{label} must be an integer") from error
    if parsed < minimum or parsed > maximum:
        raise argparse.ArgumentTypeError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return parsed


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--output-dir", type=Path, required=True)
    root.add_argument("--cwd", type=Path)
    root.add_argument(
        "--tail-lines",
        type=lambda value: positive(value, minimum=1, maximum=100, label="tail-lines"),
        default=8,
    )
    root.add_argument(
        "--signal-lines",
        type=lambda value: positive(value, minimum=1, maximum=200, label="signal-lines"),
        default=24,
    )
    root.add_argument(
        "--max-line-chars",
        type=lambda value: positive(value, minimum=80, maximum=4096, label="max-line-chars"),
        default=500,
    )
    root.add_argument("--view-mode", choices=("signal", "prefix"), default="signal")
    root.add_argument(
        "--max-view-lines",
        type=lambda value: positive(value, minimum=1, maximum=2000, label="max-view-lines"),
        default=40,
    )
    root.add_argument(
        "--max-view-chars",
        type=lambda value: positive(value, minimum=1000, maximum=500_000, label="max-view-chars"),
        default=8_000,
    )
    root.add_argument(
        "--max-output-bytes",
        type=lambda value: positive(
            value, minimum=1024, maximum=1_000_000_000, label="max-output-bytes"
        ),
        default=64_000_000,
    )
    root.add_argument("command", nargs=argparse.REMAINDER)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise SystemExit("a command argv is required after --")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = args.output_dir / "raw.log"
    view_path = args.output_dir / "view.txt"
    receipt_path = args.output_dir / "receipt.json"
    if any(path.exists() for path in (raw_path, view_path, receipt_path)):
        raise SystemExit("refusing to overwrite bounded-command artifacts")

    command_digest = hashlib.sha256(
        json.dumps(command, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    output_digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    line_count = 0
    byte_count = 0
    signal_rows: list[tuple[int, str]] = []
    prefix_rows: list[tuple[int, str]] = []
    prefix_chars = 0
    output_limit_reached = False
    tail_rows: deque[tuple[int, str]] = deque(maxlen=args.tail_lines)

    def admit(line: str) -> None:
        nonlocal line_count, prefix_chars
        line_count += 1
        cleaned = CONTROL.sub("", ANSI.sub("", line.rstrip("\r"))).strip()
        if len(cleaned) > args.max_line_chars:
            cleaned = cleaned[: args.max_line_chars] + " …[line truncated]"
        row = (line_count, cleaned)
        tail_rows.append(row)
        if (
            args.view_mode == "prefix"
            and len(prefix_rows) < args.max_view_lines
            and prefix_chars + len(cleaned) + 1 <= args.max_view_chars
        ):
            prefix_rows.append(row)
            prefix_chars += len(cleaned) + 1
        if (
            cleaned
            and (SIGNAL.search(cleaned) or SUCCESS_LINE.fullmatch(cleaned))
            and len(signal_rows) < args.signal_lines
        ):
            signal_rows.append(row)

    with exclusive(raw_path, binary=True) as raw:
        process = subprocess.Popen(
            command,
            cwd=args.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            start_new_session=True,
        )
        if process.stdout is None:
            raise SystemExit("command output pipe was unavailable")
        while True:
            chunk = process.stdout.read(64 * 1024)
            if not chunk:
                break
            remaining = args.max_output_bytes - byte_count
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
                output_limit_reached = True
            raw.write(chunk)
            output_digest.update(chunk)
            byte_count += len(chunk)
            pending += decoder.decode(chunk)
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                admit(line)
            while len(pending) > args.max_line_chars * 4:
                admit(pending[: args.max_line_chars * 4])
                pending = pending[args.max_line_chars * 4 :]
            if output_limit_reached:
                os.killpg(process.pid, signal.SIGTERM)
                break
        pending += decoder.decode(b"", final=True)
        if pending:
            admit(pending)
        raw.flush()
        os.fsync(raw.fileno())
        process_exit_code = process.wait()
        exit_code = 125 if output_limit_reached else process_exit_code

    if args.view_mode == "prefix":
        candidate_priority = list(prefix_rows)
    else:
        # Preserve the newest tail first, then spend the remaining view budget
        # on diagnostic lines. Output is still rendered chronologically.
        candidate_priority = list(reversed(tail_rows)) + list(signal_rows)
    seen: set[int] = set()
    selected_priority: list[tuple[int, str]] = []
    for row in candidate_priority:
        if row[0] in seen:
            continue
        seen.add(row[0])
        if len(selected_priority) < args.max_view_lines:
            selected_priority.append(row)

    def selected_rows() -> list[tuple[int, str]]:
        return sorted(selected_priority)

    def row_text(rows: list[tuple[int, str]]) -> str:
        return "".join(f"{index:08d}: {line}\n" for index, line in rows)

    candidate_count = len(seen)
    view_limit_reached = candidate_count > len(selected_priority)
    selected = selected_rows()
    receipt = {
        "version": 1,
        "exit_code": exit_code,
        "command_sha256": command_digest,
        "output_sha256": output_digest.hexdigest(),
        "output_bytes": byte_count,
        "output_byte_limit": args.max_output_bytes,
        "output_limit_reached": output_limit_reached,
        "process_exit_code": process_exit_code,
        "output_lines": line_count,
        "selected_lines": len(selected),
        "omitted_lines": max(0, line_count - len(selected)),
        "omitted_candidate_lines": max(0, candidate_count - len(selected)),
        "signal_limit_reached": len(signal_rows) == args.signal_lines,
        "line_character_limit": args.max_line_chars,
        "view_mode": args.view_mode,
        "view_line_limit": args.max_view_lines,
        "view_character_limit": args.max_view_chars,
        "view_limit_reached": view_limit_reached,
        "selected_payload_characters": sum(len(line) for _, line in selected),
        "view_characters": 0,
    }

    def render_view() -> str:
        return (
            "# Bounded command view\n\n"
            + json.dumps(receipt, sort_keys=True)
            + "\n\n"
            + row_text(selected_rows())
        )

    view_text = render_view()
    while len(view_text) > args.max_view_chars and selected_priority:
        selected_priority.pop()
        receipt["view_limit_reached"] = True
        selected = selected_rows()
        receipt["selected_lines"] = len(selected)
        receipt["omitted_lines"] = max(0, line_count - len(selected))
        receipt["omitted_candidate_lines"] = max(0, candidate_count - len(selected))
        receipt["selected_payload_characters"] = sum(len(line) for _, line in selected)
        view_text = render_view()
    for _ in range(8):
        measured = len(render_view())
        if receipt["view_characters"] == measured:
            break
        receipt["view_characters"] = measured
    view_text = render_view()
    if len(view_text) > args.max_view_chars:
        raise SystemExit("bounded view metadata exceeds max-view-chars")
    with exclusive(receipt_path, binary=False) as receipt_file:
        json.dump(receipt, receipt_file, indent=2, sort_keys=True)
        receipt_file.write("\n")
        receipt_file.flush()
        os.fsync(receipt_file.fileno())
    with exclusive(view_path, binary=False) as view:
        view.write(view_text)
        view.flush()
        os.fsync(view.fileno())

    for path in (raw_path, view_path, receipt_path):
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise SystemExit(f"private artifact mode drifted: {path}")
    print(json.dumps({
        "exit_code": exit_code,
        "receipt": str(receipt_path),
        "view": str(view_path),
        "raw": str(raw_path),
        "output_lines": line_count,
        "selected_lines": len(selected_rows()),
        "omitted_lines": receipt["omitted_lines"],
        "output_sha256": receipt["output_sha256"],
        "view_mode": receipt["view_mode"],
    }, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
