#!/usr/bin/env python3
"""Run stowed shell commands and emit faithful bounded receipts."""

from __future__ import annotations

import sys

# Agent PreToolUse hooks run `lazy-command hook` before every shell tool call.
# Dispatch that path before importing this module's runner dependencies
# (argparse, subprocess, shutil, uuid, ...), which are unused by the hook and
# cost more than the hook itself under load.
if __name__ == "__main__" and sys.argv[1:] == ["hook"]:
    from semantic_command_hook import main as _hook_main

    raise SystemExit(_hook_main())

import argparse
import bisect
import codecs
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "lazy-semantic-command/v1"
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
ERROR = re.compile(r"\b(?:error|fail|failed|failure|fatal|exception|traceback|assertionerror|panic)\b", re.I)
TEST_FILE_HEADER = re.compile(r"(?:/.*|[^\s:]+/.*|[^\s:/]+)\.(?:test|spec)\.[cm]?[jt]sx?:")
INLINE_RAW_LIMIT = 8_000
FOCUSED_VIEW_LIMIT = 7_000
FOCUSED_BODY_LIMIT = 5_800
FOCUSED_STORE_LIMIT = 12_000
FOCUSED_LINE_LIMIT = 5_000
FOCUSED_LINE_BYTE_BUFFER = FOCUSED_LINE_LIMIT * 4 + 4
MAX_FIND_TEXT = 500
MAX_FIND_CONTEXT = 100
LINE_NORMALIZATION_VERSION = 1
COMMAND_ACTIONS = {
    "run",
    "show",
    "s",
    "probe",
    "fallback",
    "install",
    "feedback",
    "shell",
    "sh",
    "hook",
    "compare",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_root(*, prepare: bool = True) -> Path:
    configured = os.environ.get("LAZY_COMMAND_STATE_ROOT")
    root = Path(configured).expanduser() if configured else Path.home() / ".codex" / "state" / "lazy-command"
    if prepare:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
    return root.resolve()


def private_write(path: Path, data: str | bytes) -> None:
    binary = isinstance(data, bytes)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb" if binary else "w", encoding=None if binary else "utf-8") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise SystemExit(f"private artifact mode drifted: {path}")


def digest_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_request(identifier: str) -> tuple[Path, dict[str, Any]]:
    if not re.fullmatch(r"[0-9a-f]{16}", identifier):
        raise SystemExit("invalid semantic-command id")
    run_dir = state_root(prepare=False) / identifier
    request_path = run_dir / "request.json"
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit("semantic-command request unavailable") from error
    if request.get("schema") != SCHEMA or request.get("id") != identifier:
        raise SystemExit("semantic-command request identity mismatch")
    expected = request.get("request_sha256")
    unsigned = {key: value for key, value in request.items() if key != "request_sha256"}
    if expected != digest_json(unsigned):
        raise SystemExit("semantic-command request digest mismatch")
    return run_dir, request


def clean_lines(raw: bytes) -> list[str]:
    """Normalize terminal controls only; preserve content, indentation and empty rows."""
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    return CONTROL.sub("", ANSI.sub("", text)).splitlines()


def classify_command(command: str) -> str:
    """Label actual invocations; mixed commands receive no family-specific projection."""
    from semantic_command_hook import command_argvs

    commands = command_argvs(command)
    if not commands:
        return "Command"
    kinds = set()
    for argv in commands:
        name = Path(argv[0]).name.lower()
        args = tuple(part.lower() for part in argv[1:])
        if name == "codex" and args[:1] == ("exec",):
            kinds.add("Codex exec")
        elif name == "gh":
            kinds.add("GitHub PR" if args[:1] == ("pr",) else "GitHub")
        elif name == "git":
            kinds.add("Git")
        elif name in {"rg", "find"}:
            kinds.add("Search")
        elif (name in {"pytest", "vitest", "jest"} or
              re.fullmatch(r"python3?(?:\.\d+)*", name) and args[:2] == ("-m", "unittest") or
              name in {"cargo", "npm", "bun", "pnpm", "go"} and args[:1] == ("test",)):
            kinds.add("Tests")
        elif name in {"cargo", "npm", "bun", "pnpm", "pip", "go"}:
            kinds.add("Build")
        elif name not in {"cd", ":", "true"}:
            kinds.add("Command")
    return kinds.pop() if len(kinds) == 1 else "Command"


def is_error_line(row: str) -> bool:
    # Successful test names often describe errors. Their names are data, not
    # evidence that this invocation failed; keep the rows, but don't promote them.
    if (re.fullmatch(r"\(pass\) .+ \[\d+(?:\.\d+)?m?s\]", row)
            or re.fullmatch(r"test \S+(?: - should panic)? \.\.\. ok", row)
            or re.fullmatch(r"test\w* \([\w.]+\) \.\.\. ok", row)
            or TEST_FILE_HEADER.fullmatch(row)):
        return False
    if not ERROR.search(row):
        return False
    try:
        json.loads(row)
        return False  # A requested JSON string may merely discuss errors.
    except ValueError:
        pass
    return re.search(
        r"(?:\b0\s+(?:errors?|failed|failures?)\b|\bno\s+(?:errors?|failures?)\b|"
        r"(?:errors?|failures?|failed)\s*[:=]\s*0\b)",
        row,
        re.I,
    ) is None


VIEW_LIMIT = 7_000


def diagnostic_owners(rows: list[str]) -> list[int | None]:
    """Recognize explicit runner section headers, without guessing from prose."""
    owner = None
    owners = []
    for index, row in enumerate(rows):
        if (TEST_FILE_HEADER.fullmatch(row)
                or re.fullmatch(r"\s*Running .+\(.+\)", row)
                or re.fullmatch(r"(?:FAIL|ERROR): \S+ \(.+\)", row)):
            owner = index
        elif re.match(r"\s*\d+ (?:pass|fail)\b|Ran \d+ tests?\b", row):
            owner = None
        owners.append(owner)
    return owners


def repeated_units(rows: list[str]) -> list[tuple[int, int, str]]:
    """Keep identical adjacent blocks local and ordered; never fold JSON records."""
    result = []
    index = 0
    while index < len(rows):
        try:
            json.loads(rows[index])
            structured = True
        except ValueError:
            structured = False
        folded = False
        if rows[index] and not structured:
            for size in range(1, min(32, (len(rows) - index) // 2) + 1):
                block = rows[index:index + size]
                if block != rows[index + size:index + 2 * size]:
                    continue
                if size > 1:
                    contains_json = False
                    for row in block:
                        try:
                            json.loads(row)
                            contains_json = True
                            break
                        except ValueError:
                            pass
                    if contains_json:
                        continue
                end = index + 2 * size
                while rows[end:end + size] == block:
                    end += size
                count = (end - index) // size
                if size == 1:
                    text = block[0] + f"\n[repeat ×{count}]"
                else:
                    text = f"repeat ×{count} · {size} lines\n" + "\n".join(block)
                # A block stays an indivisible view unit, including its count.
                if (size == 1 or len(text) <= 6000) and len(text) < sum(len(row) + 1 for row in block) * count:
                    result.append((index, end, text))
                    index = end
                    folded = True
                    break
        if not folded:
            result.append((index, index + 1, rows[index]))
            index += 1
    return result


def compact_rows(rows: list[str]) -> list[tuple[int, int, str]]:
    """Fold adjacent exact repetitions, without equating distinct JSON records."""
    # Escape notation-shaped source rows before factoring. Expansion recovers
    # these rows, then removes exactly one literal layer; it never reparses data.
    encoded = [
        "literal " + json.dumps(row, ensure_ascii=False)
        if row.startswith(("prefix ", "+ ", "repeat ×", "[repeat ×", "literal "))
        else row
        for row in rows
    ]
    result = repeated_units(encoded)
    # Factor adjacent path prefixes without merging locations or reordering matches.
    # JSON string notation makes whitespace and delimiters reversible in the view.
    def path_row(unit: tuple[int, int, str]) -> str | None:
        start, end, text = unit
        if end != start + 1 or not text or text != text.lstrip() or "\n" in text:
            return None
        try:
            json.loads(text)
            return None
        except (ValueError, TypeError):
            pass
        if re.match(r"^(?:/|\.?\.?/|[\w.-]+/)", text):
            return text
        location = re.match(r"^(.+?):\d+:", text)
        if location and (
            "/" in location.group(1)
            or re.search(r"\.[^/:\s]+$", location.group(1))
        ):
            return text
        return None

    def local_path_prefix(first: str, second: str) -> str:
        first_location = re.match(r"^(.+?):\d+:", first)
        second_location = re.match(r"^(.+?):\d+:", second)
        if (
            first_location
            and second_location
            and first_location.group(1) == second_location.group(1)
        ):
            return first[:first_location.end(1) + 1]
        shared = os.path.commonprefix((first, second))
        return shared[:shared.rfind("/") + 1]

    factored = []
    index = 0
    while index < len(result):
        start, end, text = result[index]
        first = path_row(result[index])
        second = path_row(result[index + 1]) if index + 1 < len(result) else None
        prefix = (
            local_path_prefix(first, second)
            if first is not None and second is not None
            else ""
        )
        stop = index + 2
        if prefix:
            while stop < min(len(result), index + 64):
                candidate = path_row(result[stop])
                if candidate is None or not candidate.startswith(prefix):
                    break
                stop += 1
        group = result[index:stop] if prefix else result[index:index + 1]
        if len(group) >= 3:
            compact = "prefix " + json.dumps(prefix, ensure_ascii=False) + "\n" + "\n".join(
                "+ " + json.dumps(row[2][len(prefix):], ensure_ascii=False) for row in group
            )
            if prefix and len(compact) <= 6000 and len(compact) < sum(len(row[2]) + 1 for row in group):
                factored.append((start, group[-1][1], compact))
                index = stop
                continue
        factored.append(result[index])
        index += 1
    # Factor adjacent phrase prefixes only when every original row is a plain,
    # unindented line. JSON notation keeps prefixes and suffixes reversible.
    def plain_phrase(unit: tuple[int, int, str]) -> str | None:
        start, end, text = unit
        if end != start + 1 or not text or text != text.lstrip() or "\n" in text:
            return None
        try:
            json.loads(text)
            return None
        except (ValueError, TypeError):
            return text

    phrase_factored = []
    index = 0
    while index < len(factored):
        start, _end, text = factored[index]
        first = plain_phrase(factored[index])
        second = plain_phrase(factored[index + 1]) if index + 1 < len(factored) else None
        if first is None or second is None:
            phrase_factored.append(factored[index])
            index += 1
            continue
        shared = os.path.commonprefix((first, second))
        boundary = shared.rfind(" ")
        prefix = shared[:boundary + 1] if boundary >= 0 else ""
        if len(prefix.strip()) < 8:
            phrase_factored.append(factored[index])
            index += 1
            continue
        stop = index + 2
        while stop < min(len(factored), index + 64):
            candidate = plain_phrase(factored[stop])
            if candidate is None or not candidate.startswith(prefix):
                break
            stop += 1
        group = factored[index:stop]
        suffixes = [row[2][len(prefix):] for row in group]
        compact = "prefix " + json.dumps(prefix, ensure_ascii=False) + "\n" + "\n".join(
            "+ " + json.dumps(suffix, ensure_ascii=False) for suffix in suffixes
        )
        if all(suffixes) and len(compact) <= 6000 and len(compact) < sum(len(row[2]) + 1 for row in group):
            phrase_factored.append((start, group[-1][1], compact))
            index = stop
            continue
        phrase_factored.append(factored[index])
        index += 1
    return phrase_factored


def unittest_success_projection(
    command: str, rows: list[str], exit_code: int
) -> tuple[str, dict[str, int]] | None:
    """Compact only a complete, successful unittest progress grammar."""
    if exit_code != 0:
        return None
    from semantic_command_hook import command_argvs

    commands = command_argvs(command)
    if not commands:
        return None
    substantive = [
        argv for argv in commands
        if Path(argv[0]).name.lower() not in {"cd", ":", "true"}
    ]
    if len(substantive) != 1:
        return None
    argv = substantive[0]
    name = Path(argv[0]).name.lower()
    arguments = tuple(part.lower() for part in argv[1:])
    if not re.fullmatch(r"python3?(?:\.\d+)*", name) or arguments[:2] != ("-m", "unittest"):
        return None

    content = [row for row in rows if row]
    if len(content) != 4:
        return None
    progress, separator, ran, complete = content
    if not re.fullmatch(r"[.sx]+", progress) or not re.fullmatch(r"-{70}", separator):
        return None
    ran_match = re.fullmatch(r"Ran (\d+) tests? in [0-9]+(?:\.[0-9]+)?s", ran)
    complete_match = re.fullmatch(r"OK(?: \(([^()]*)\))?", complete)
    if ran_match is None or complete_match is None or int(ran_match.group(1)) != len(progress):
        return None

    reported: dict[str, int] = {}
    if complete_match.group(1):
        for field in complete_match.group(1).split(", "):
            match = re.fullmatch(r"(skipped|expected failures)=(\d+)", field)
            if match is None or match.group(1) in reported or int(match.group(2)) == 0:
                return None
            reported[match.group(1)] = int(match.group(2))
    counts = Counter(progress)
    if reported.get("skipped", 0) != counts["s"]:
        return None
    if reported.get("expected failures", 0) != counts["x"]:
        return None
    summary = [f"passed {counts['.']}"]
    if counts["s"]:
        summary.append(f"skipped {counts['s']}")
    if counts["x"]:
        summary.append(f"expected failures {counts['x']}")
    payload = "\n".join((f"unittest progress {len(progress)} · " + ", ".join(summary), ran, complete))
    return payload, {"unittest_progress": len(progress), "separator": 1}


def project(command: str, raw: bytes, exit_code: int) -> tuple[str, dict[str, int], int, int]:
    """Keep output in order. Bound volume, never infer which values are expendable."""
    rows = clean_lines(raw)
    errors = [i for i, row in enumerate(rows) if is_error_line(row)]
    from git_diff_projection import project_git_diff

    diff_projection = project_git_diff(command, rows, exit_code, VIEW_LIMIT)
    if diff_projection is not None:
        payload, diff_omissions = diff_projection
        return payload, diff_omissions, len(rows), 0
    # Compact layout only when it recovers a complete over-budget JSON document.
    # Token spelling/order and duplicate keys are preserved, not reserialized.
    from json_output_layout import compact_json_layout, validate_json

    try:
        # Valid JSON can contain Unicode separators/control characters inside
        # strings. Terminal cleanup must not rewrite those requested values.
        json_text = raw.decode("utf-8", "replace").replace("\r\n", "\n").removesuffix("\n")
        try:
            validate_json(json_text)
        except (ValueError, RecursionError):
            json_text = "\n".join(rows)  # Retain support for ANSI-colored JSON.
            validate_json(json_text)
        if len(json_text) <= VIEW_LIMIT:
            return json_text, {}, len(rows), len(errors)
        compact_json = compact_json_layout(json_text, VIEW_LIMIT)
        if compact_json is not None:
            return compact_json, {}, len(rows), len(errors)
        units = [(i, i + 1, row) for i, row in enumerate(rows)]
    except (ValueError, RecursionError):
        unittest_projection = unittest_success_projection(command, rows, exit_code)
        if unittest_projection is not None:
            payload, unittest_omissions = unittest_projection
            return payload, unittest_omissions, len(rows), 0
        units = compact_rows(rows)
    folded = 0
    for start, end, text in units:
        if "\n[repeat ×" in text:
            folded += end - start - 1
        elif match := re.match(r"repeat ×(\d+) · (\d+) lines\n", text):
            folded += (int(match[1]) - 1) * int(match[2])
    omitted = {"repeat": folded} if folded else {}
    full = "\n".join(text for _, _, text in units)
    if len(full) <= VIEW_LIMIT:
        return full, omitted, len(rows), len(errors)

    # Keep head/tail and diagnostic neighborhoods. Gaps retain original line numbers;
    # expansion reads the stored command output, never reruns the command.
    important = set()
    for i in errors:
        important.update(range(max(0, i - 3), min(len(rows), i + 5)))
    diagnostic = [
        i for i in range(len(units))
        if any(n in important for n in range(units[i][0], units[i][1]))
    ]
    head = list(range(min(3, len(units))))
    tail = list(reversed(range(max(0, len(units) - 8), len(units))))
    owners = diagnostic_owners(rows)
    starts = [start for start, _, _ in units]
    dependencies = {}
    for i in diagnostic:
        dependencies[i] = {
            bisect.bisect_right(starts, owners[n]) - 1
            for n in range(units[i][0], units[i][1])
            if n in important and owners[n] is not None
        }
    # Terminal state is required even when earlier output contains many errors.
    # Once a failed command has diagnostic context, don't fill spare space with
    # arbitrary successful progress simply because the budget allows it.
    ordinary = [] if exit_code != 0 and errors else list(reversed(range(len(units))))
    priority = list(dict.fromkeys(tail + head + diagnostic + ordinary))
    selected: dict[int, str] = {}
    selected_characters = 0
    selected_blocks = 0
    # Reserve enough per selected block for its possible leading, trailing, or
    # inter-block gap notices instead of charging every selected line.
    gap_block_reserve = 100
    for i in priority:
        if i in selected:
            continue
        text = units[i][2]
        bundle = sorted(({i} | dependencies.get(i, set())) - selected.keys())
        added = set()
        candidate_blocks = selected_blocks
        candidate_characters = selected_characters
        for j in bundle:
            before = j - 1 in selected or j - 1 in added
            after = j + 1 in selected or j + 1 in added
            candidate_blocks += 1 - int(before) - int(after)
            candidate_characters += len(units[j][2]) + 1
            added.add(j)
        if candidate_characters + candidate_blocks * gap_block_reserve <= VIEW_LIMIT:
            for j in bundle:
                selected[j] = units[j][2]
            selected_characters = candidate_characters
            selected_blocks = candidate_blocks
        elif len(text) > VIEW_LIMIT and (i in dependencies or not selected):
            # Reserve terminal state without making a single huge diagnostic
            # disappear. Include its owner and explicit middle-loss marker.
            other_cost = sum(len(units[j][2]) + 1 for j in bundle if j != i)
            available = VIEW_LIMIT - selected_characters - other_cost - candidate_blocks * gap_block_reserve - 1
            marker = "\n[… middle of block omitted …]\n"
            content = min(6000, available - len(marker))
            if content < 200:
                continue
            before = content // 2
            after = content - before
            excerpt = text[:before] + marker + text[-after:]
            for j in bundle:
                selected[j] = excerpt if j == i else units[j][2]
            selected_characters += other_cost + len(excerpt) + 1
            selected_blocks = candidate_blocks
            omitted["characters"] = omitted.get("characters", 0) + len(text) - content
    rendered = []
    next_line = 0
    hidden = 0
    for i in sorted(selected):
        start, end, _ = units[i]
        if start > next_line:
            rendered.append(f"[… lines {next_line + 1}–{start} omitted …]")
            hidden += start - next_line
        rendered.append(selected[i])
        next_line = end
    if next_line < len(rows):
        rendered.append(f"[… lines {next_line + 1}–{len(rows)} omitted …]")
        hidden += len(rows) - next_line
    if hidden:
        omitted["lines"] = hidden
    return "\n".join(rendered), omitted, len(rows), len(errors)


def human_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.1f} MiB"


def render(receipt: dict[str, Any], payload: str) -> str:
    if receipt["exit_code"] != 0:
        status = f"FAIL {receipt['exit_code']}"
    else:
        status = "OK"
    first = f"{status} · {receipt['kind']} · id {receipt['id']}"
    rows = [first]
    if payload:
        rows.append(payload)
    omitted = receipt["omitted"]
    if omitted.get("lines") or omitted.get("characters"):
        rows.append(f"expand: lc s {receipt['id']} --find TEXT (or --lines START:END)")
    if receipt.get("output_limit_reached"):
        rows.append("capture limit reached; command stopped, raw output is incomplete")
    return "\n".join(rows) + "\n"


def execution_cwd(request: dict[str, Any], override: str | None) -> str:
    candidate = Path(override).expanduser() if override else Path(request["cwd"]).expanduser()
    return os.fspath(candidate.resolve())


def emit_result(run_dir: Path, view: str, raw_limit: int | None, baseline: str | None = None) -> None:
    if baseline is not None:
        from receipt_compare import compare
        try:
            compare(baseline, run_dir.name)
        except SystemExit as error:
            # The command has already completed. Preserve its actual exit status
            # and full ordinary receipt if comparison evidence became unavailable.
            print(f"comparison unavailable: {error}", file=sys.stderr)
            sys.stdout.write(view)
        return
    if raw_limit is None:
        sys.stdout.write(view)
        return
    raw = (run_dir / "raw.log").read_bytes()
    if len(raw) <= raw_limit:
        sys.stdout.buffer.write(raw)
        return
    sys.stdout.write(view)
    sys.stdout.write(
        f"output withheld · {human_bytes(len(raw))} exceeds inline limit "
        f"{human_bytes(raw_limit)}\n"
    )


def run(
    identifier: str,
    cwd_override: str | None = None,
    raw_limit: int | None = None,
    baseline: str | None = None,
) -> int:
    run_dir, request = load_request(identifier)
    receipt_path = run_dir / "receipt.json"
    view_path = run_dir / "view.txt"
    if receipt_path.exists() and view_path.exists():
        emit_result(run_dir, view_path.read_text(encoding="utf-8"), raw_limit, baseline)
        return int(json.loads(receipt_path.read_text(encoding="utf-8"))["exit_code"])
    started_path = run_dir / "started.json"
    if started_path.exists():
        raise SystemExit(f"AMBIGUOUS · prior run has no receipt · id {identifier}")
    private_write(started_path, json.dumps({"started_at": utc_now()}, sort_keys=True) + "\n")

    raw_path = run_dir / "raw.log"
    raw_digest = hashlib.sha256()
    output = bytearray()
    limit = int(request.get("max_output_bytes", 64_000_000))
    child: subprocess.Popen[bytes] | None = None

    def stop_child(signum: int, _frame: Any) -> None:
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signum)

    previous_term = signal.signal(signal.SIGTERM, stop_child)
    previous_int = signal.signal(signal.SIGINT, stop_child)
    started = time.monotonic()
    capped = False
    cwd = execution_cwd(request, cwd_override)
    try:
        child = subprocess.Popen(
            ["/bin/bash", "-c", request["command"]],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        assert child.stdout is not None
        with os.fdopen(os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as raw_file:
            while True:
                chunk = child.stdout.read(64 * 1024)
                if not chunk:
                    break
                remaining = limit - len(output)
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                    capped = True
                raw_file.write(chunk)
                raw_digest.update(chunk)
                output.extend(chunk)
                if capped:
                    os.killpg(child.pid, signal.SIGTERM)
                    break
            raw_file.flush()
            os.fsync(raw_file.fileno())
        process_exit = child.wait()
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)

    exit_code = 125 if capped else process_exit
    payload, omitted, output_lines, detected_errors = project(
        request["command"], bytes(output), exit_code
    )
    receipt = {
        "schema": "lazy-semantic-command-receipt/v1",
        "id": identifier,
        "request_sha256": request["request_sha256"],
        "command_sha256": hashlib.sha256(request["command"].encode("utf-8")).hexdigest(),
        "cwd": cwd,
        "output_sha256": raw_digest.hexdigest(),
        "output_bytes": len(output),
        "output_lines": output_lines,
        "line_normalization_version": LINE_NORMALIZATION_VERSION,
        "detected_error_lines": detected_errors,
        "output_limit_reached": capped,
        "exit_code": exit_code,
        "process_exit_code": process_exit,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "kind": classify_command(request["command"]),
        "omitted": omitted,
        "completed_at": utc_now(),
    }
    view = render(receipt, payload)
    private_write(receipt_path, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    private_write(view_path, view)
    emit_result(run_dir, view, raw_limit, baseline)
    return exit_code


def stored_lines(path: Path, needle: str | None = None):
    """Yield captured lines without retaining an oversized line or the whole log."""
    needle_bytes = needle.encode("utf-8") if needle is not None else None
    number = 0
    line_offset = 0
    line_bytes = 0
    content = bytearray()
    oversized = False
    match_tail = b""
    raw_matches = 0

    def add(segment: bytes) -> None:
        nonlocal line_bytes, oversized, match_tail, raw_matches
        line_bytes += len(segment)
        if not oversized:
            if len(content) + len(segment) <= FOCUSED_LINE_BYTE_BUFFER:
                content.extend(segment)
            else:
                oversized = True
                content.clear()
        if needle_bytes is None:
            return
        searchable = match_tail + segment
        # Each segment is bounded by the read chunk. Count in native code rather
        # than one Python iteration per match in a dense log; retain only the
        # unmatched suffix that can participate in a cross-chunk match.
        pieces = searchable.split(needle_bytes)
        raw_matches += len(pieces) - 1
        match_tail = pieces[-1][-(len(needle_bytes) - 1):] if len(needle_bytes) > 1 else b""

    def finish(delimiter: bytes):
        nonlocal number, line_offset, line_bytes, content, oversized, match_tail, raw_matches
        line_bytes += len(delimiter)
        number += 1
        if oversized:
            text = None
        else:
            text = CONTROL.sub("", ANSI.sub("", bytes(content).decode("utf-8", "replace")))
            if len(text) > FOCUSED_LINE_LIMIT:
                text = None
        record = (number, text, line_offset, line_bytes, raw_matches)
        line_offset += line_bytes
        line_bytes = 0
        content = bytearray()
        oversized = False
        match_tail = b""
        raw_matches = 0
        return record

    carry = b""
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            data = carry + chunk
            carry = b""
            position = 0
            while position < len(data):
                boundaries = [
                    (found, delimiter)
                    for delimiter in (b"\r", b"\n", b"\xe2\x80\xa8", b"\xe2\x80\xa9")
                    if (found := data.find(delimiter, position)) >= 0
                ]
                if not boundaries:
                    hold = 2 if data.endswith(b"\xe2\x80") else 1 if data.endswith((b"\r", b"\xe2")) else 0
                    add(data[position:len(data) - hold if hold else None])
                    carry = data[-hold:] if hold else b""
                    break
                boundary, delimiter = min(boundaries, key=lambda item: item[0])
                add(data[position:boundary])
                if delimiter == b"\r" and boundary + 1 == len(data):
                    carry = b"\r"
                    break
                if delimiter == b"\r" and data[boundary:boundary + 2] == b"\r\n":
                    delimiter = b"\r\n"
                yield finish(delimiter)
                position = boundary + len(delimiter)
        if carry == b"\r":
            yield finish(b"\r")
        else:
            add(carry)
        if line_bytes or content or oversized:
            yield finish(b"")


def collect_line_range(
    path: Path, start: int, end: int
) -> list[tuple[int, str | None, int, int, int]]:
    records = []
    stored_characters = 0
    for number, text, offset, byte_length, _matches in stored_lines(path):
        if number >= start:
            if text is None:
                records.append((number, None, offset, byte_length, 0))
            else:
                cost = len(text) + len(str(number)) + 4
                if stored_characters + cost <= FOCUSED_STORE_LIMIT:
                    records.append((number, text, offset, byte_length, 0))
                    stored_characters += cost
        if number == end:
            break
    return records


def focused_line_count(path: Path, receipt: dict[str, Any]) -> int:
    """Trust only line counts produced by this reader's normalization contract."""
    if receipt.get("line_normalization_version") == LINE_NORMALIZATION_VERSION:
        return receipt["output_lines"]
    count = 0
    for count, _text, _offset, _byte_length, _matches in stored_lines(path):
        pass
    return count


def search_line_ranges(
    path: Path, needle: str, context: int
) -> tuple[int, int, int, list[int], list[list[int]], list[tuple[int, str | None, int, int, int]]]:
    total_lines = 0
    total_matches = 0
    matching_lines = 0
    first_match_lines = []
    for number, _text, _offset, _byte_length, count in stored_lines(path, needle):
        total_lines = number
        if not count:
            continue
        total_matches += count
        matching_lines += 1
        if len(first_match_lines) < 1_000:
            first_match_lines.append(number)
    if not first_match_lines:
        return total_lines, total_matches, matching_lines, [], [], []

    ranges: list[list[int]] = []
    for number in first_match_lines:
        start = max(1, number - context)
        end = min(total_lines, number + context)
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], end)
        else:
            ranges.append([start, end])

    records = []
    stored_characters = 0
    range_index = 0
    for number, text, offset, byte_length, count in stored_lines(path, needle):
        while range_index < len(ranges) and number > ranges[range_index][1]:
            range_index += 1
        if range_index >= len(ranges):
            break
        if number < ranges[range_index][0]:
            continue
        if text is None:
            records.append((number, None, offset, byte_length, count))
            continue
        cost = len(text) + len(str(number)) + 4
        if stored_characters + cost <= FOCUSED_STORE_LIMIT:
            records.append((number, text, offset, byte_length, count))
            stored_characters += cost
    return total_lines, total_matches, matching_lines, first_match_lines, ranges, records


def select_numbered_lines(
    identifier: str,
    records: list[tuple[int, str | None, int, int, int]],
    first_relevant: int,
    width: int,
) -> tuple[list[str], set[int], int, int | None]:
    rendered = []
    shown_lines: set[int] = set()
    shown_matches = 0
    previous = first_relevant - 1
    first_omitted = None
    for number, text, offset, byte_length, match_count in records:
        additions = []
        if number > previous + 1:
            additions.append(f"[… lines {previous + 1}–{number - 1} not shown …]")
        if text is None:
            additions.append(
                f"[… line {number} is {byte_length} bytes; "
                f"page it with lc s -r {identifier} --offset {offset} --limit 7000 …]"
            )
        else:
            additions.append(f"{number:>{width}} | {text}")
        candidate = "\n".join([*rendered, *additions])
        if len(candidate) > FOCUSED_BODY_LIMIT:
            first_omitted = number
            break
        rendered.extend(additions)
        previous = number
        if text is not None:
            shown_lines.add(number)
            shown_matches += match_count
    return rendered, shown_lines, shown_matches, first_omitted


def show_lines(identifier: str, path: Path, specification: str, total_lines: int) -> int:
    match = re.fullmatch(r"([1-9]\d*):([1-9]\d*)", specification)
    if match is None:
        raise SystemExit("--lines must be START:END with positive 1-based line numbers")
    start, end = map(int, match.groups())
    if start > end:
        raise SystemExit("--lines START must not exceed END")
    records = [] if start > total_lines else collect_line_range(path, start, end)
    actual_end = min(end, total_lines)
    requested = max(0, actual_end - start + 1)
    width = max(1, len(str(total_lines)))
    body, shown, _matches, first_omitted = select_numbered_lines(
        identifier, records, start, width
    )
    remaining = requested - len(shown)
    header = (
        f"lines {start}:{end} · showing {len(shown)} of {requested} requested · "
        f"{total_lines} captured · limit {FOCUSED_VIEW_LIMIT} chars"
    )
    footer = []
    if remaining:
        next_line = first_omitted or next(
            (number for number in range(start, actual_end + 1) if number not in shown), start
        )
        footer.append(f"[… {remaining} requested lines not shown …]")
        footer.append(
            f"narrow: lc s {identifier} --lines {next_line}:{min(actual_end, next_line + 19)}"
        )
    result = "\n".join([header, *body, *footer])
    if len(result) > FOCUSED_VIEW_LIMIT:
        raise SystemExit("focused line view exceeded its internal character limit")
    sys.stdout.write(result + "\n")
    return 0


def show_find(identifier: str, path: Path, needle: str, context: int) -> int:
    if not needle:
        raise SystemExit("--find text must not be empty")
    if "\n" in needle or "\r" in needle:
        raise SystemExit("--find text must be a single line")
    if len(needle) > MAX_FIND_TEXT:
        raise SystemExit(f"--find text must not exceed {MAX_FIND_TEXT} characters")
    if context < 0 or context > MAX_FIND_CONTEXT:
        raise SystemExit(f"--context must be between 0 and {MAX_FIND_CONTEXT}")
    total_lines, total_matches, matching_lines, match_lines, ranges, records = (
        search_line_ranges(path, needle, context)
    )
    width = max(1, len(str(total_lines)))
    first_relevant = 1
    body, shown, shown_matches, first_omitted = select_numbered_lines(
        identifier, records, first_relevant, width
    )
    remaining_matches = total_matches - shown_matches
    context_lines = sum(end - start + 1 for start, end in ranges)
    remaining_context = context_lines - len(shown)
    header = (
        f"find {json.dumps(needle, ensure_ascii=False)} · {total_matches} matches on "
        f"{matching_lines} lines · showing {len(shown)} of {context_lines} context lines · "
        f"{total_lines} captured · context {context} · limit {FOCUSED_VIEW_LIMIT} chars"
    )
    footer = []
    if remaining_matches:
        footer.append(f"[… {remaining_matches} matches not shown …]")
    if remaining_context:
        footer.append(f"[… {remaining_context} selected context lines not shown …]")
    if remaining_matches or remaining_context:
        unshown_match = next((number for number in match_lines if number not in shown), None)
        if unshown_match is not None:
            narrow_start = max(1, unshown_match - context)
            narrow_end = min(total_lines, unshown_match + context)
        else:
            unshown_context = first_omitted or next(
                (
                    number
                    for start, end in ranges
                    for number in range(start, end + 1)
                    if number not in shown
                ),
                None,
            )
            narrow_start = unshown_context
            narrow_end = min(total_lines, unshown_context + 19) if unshown_context else None
        if narrow_start is None:
            footer.append("narrow: reduce --context or select a later range with --lines")
        else:
            footer.append(
                f"narrow: lc s {identifier} --lines "
                f"{narrow_start}:{narrow_end}"
            )
    result = "\n".join([header, *body, *footer])
    if len(result) > FOCUSED_VIEW_LIMIT:
        raise SystemExit("focused search view exceeded its internal character limit")
    sys.stdout.write(result + "\n")
    return 0


def show(
    identifier: str,
    raw: bool,
    offset: int | None = None,
    limit: int | None = None,
    lines: str | None = None,
    find: str | None = None,
    context: int | None = None,
) -> int:
    modes = int(raw) + int(lines is not None) + int(find is not None)
    if modes > 1:
        raise SystemExit("--raw, --lines, and --find are mutually exclusive")
    if (lines is not None or find is not None) and (offset is not None or limit is not None):
        raise SystemExit("--lines/--find cannot be combined with --offset/--limit")
    if context is not None and find is None:
        raise SystemExit("--context requires --find")
    if find is not None and any(separator in find for separator in ("\n", "\r", "\u2028", "\u2029")):
        raise SystemExit("--find text must be a single line")
    run_dir, request = load_request(identifier)
    focused = lines is not None or find is not None
    path = run_dir / ("raw.log" if raw or focused else "view.txt")
    if not path.exists():
        raise SystemExit("semantic-command result is not settled")
    if focused:
        try:
            receipt = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SystemExit("focused expansion requires a settled semantic-command receipt") from error
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema") != "lazy-semantic-command-receipt/v1"
            or receipt.get("id") != identifier
            or receipt.get("request_sha256") != request["request_sha256"]
            or receipt.get("output_bytes") != path.stat().st_size
            or not isinstance(receipt.get("output_lines"), int)
            or receipt["output_lines"] < 0
        ):
            raise SystemExit("focused expansion receipt does not match the captured output")
    if lines is not None:
        return show_lines(identifier, path, lines, focused_line_count(path, receipt))
    if find is not None:
        return show_find(identifier, path, find, context if context is not None else 3)
    if offset is not None or limit is not None:
        if not raw:
            raise SystemExit("--offset/--limit require --raw")
        if (offset is not None and offset < 0) or (limit is not None and limit < 1):
            raise SystemExit("offset must be nonnegative and limit must be positive")
        start = offset or 0
        size = min(limit or INLINE_RAW_LIMIT, INLINE_RAW_LIMIT)
        with path.open("rb") as stream:
            stream.seek(start)
            page = stream.read(size)
        sys.stdout.buffer.write(page)
        sys.stdout.buffer.flush()
        end = start + len(page)
        if end < path.stat().st_size:
            print(f"\nnext: lc s -r {identifier} --offset {end} --limit {size}", file=sys.stderr)
    elif raw:
        sys.stdout.buffer.write(path.read_bytes())
    else:
        sys.stdout.write(path.read_text(encoding="utf-8"))
    return 0


def probe(identifier: str) -> int:
    """Return success only when the sandbox can settle this command receipt."""
    run_dir, _request = load_request(identifier)
    path = run_dir / f".probe.{os.getpid()}"
    try:
        private_write(path, b"")
        path.unlink()
    except OSError:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return 1
    return 0


def fallback(identifier: str, cwd_override: str | None = None) -> int:
    """Run the untouched command once when receipt storage is unavailable."""
    _run_dir, request = load_request(identifier)
    os.chdir(execution_cwd(request, cwd_override))
    os.execv("/bin/bash", ["/bin/bash", "-c", request["command"]])
    raise AssertionError("exec returned")


def feedback(workspace: Path | None, since_hours: float | None, top: int) -> int:
    target = str(workspace.expanduser().resolve()) if workspace else None
    cutoff = None
    if since_hours is not None:
        cutoff = datetime.now(timezone.utc).timestamp() - since_hours * 3600
    kinds: Counter[str] = Counter()
    omissions: Counter[str] = Counter()
    candidates = []
    raw_bytes = 0
    visible_bytes = 0
    failures = 0
    capped = 0
    scanned = 0
    malformed = 0
    for receipt_path in state_root().glob("*/receipt.json"):
        scanned += 1
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            request = json.loads(receipt_path.with_name("request.json").read_text(encoding="utf-8"))
            completed = datetime.fromisoformat(str(receipt["completed_at"])).timestamp()
        except (KeyError, OSError, ValueError, json.JSONDecodeError):
            malformed += 1
            continue
        if cutoff is not None and completed < cutoff:
            continue
        observed_cwd = receipt.get("cwd", request.get("cwd", ""))
        if target is not None and str(Path(str(observed_cwd)).expanduser().resolve()) != target:
            continue
        output_size = int(receipt.get("output_bytes", 0))
        view_path = receipt_path.with_name("view.txt")
        try:
            view_size = view_path.stat().st_size
        except OSError:
            view_size = 0
        raw_bytes += output_size
        visible_bytes += view_size
        kind = str(receipt.get("kind") or "Command")
        kinds[kind] += 1
        failures += int(int(receipt.get("exit_code", 0)) != 0)
        capped += int(bool(receipt.get("output_limit_reached")))
        for name, count in (receipt.get("omitted") or {}).items():
            if isinstance(count, int) and not isinstance(count, bool):
                omissions[str(name)] += count
        candidates.append(
            {
                "id": str(receipt.get("id", receipt_path.parent.name)),
                "kind": kind,
                "rawBytes": output_size,
                "visibleBytes": view_size,
                "exitCode": int(receipt.get("exit_code", 0)),
            }
        )
    candidates.sort(key=lambda row: (-row["rawBytes"], row["id"]))
    reduction = 0.0 if raw_bytes == 0 else round(100 * (1 - visible_bytes / raw_bytes), 3)
    view = {
        "schema": "lazy-semantic-command-feedback/v1",
        "workspaceSha256": hashlib.sha256(target.encode()).hexdigest() if target else None,
        "sinceHours": since_hours,
        "runs": sum(kinds.values()),
        "rawBytes": raw_bytes,
        "visibleBytes": visible_bytes,
        "reductionPercent": reduction,
        "failures": failures,
        "outputLimitReached": capped,
        "byKind": dict(sorted(kinds.items())),
        "omissions": dict(sorted(omissions.items())),
        "largestCandidates": candidates[:top],
        "scannedReceipts": scanned,
        "malformedReceipts": malformed,
        "rawBodiesEmitted": False,
        "commandBodiesEmitted": False,
        "workspacePathsEmitted": False,
    }
    print(json.dumps(view, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


def install_short_alias() -> Path:
    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    alias = bin_dir / "lc"
    target = Path(__file__).resolve()
    if alias.exists() or alias.is_symlink():
        if alias.is_symlink() and alias.resolve() == target:
            return alias
        # A link to another (possibly moved or deleted) semantic_command.py is
        # an earlier lazy-command install; repoint it at this clone.
        if not (alias.is_symlink() and Path(os.readlink(alias)).name == target.name):
            raise SystemExit(f"short alias already exists and is not managed by lazy-command: {alias}")
    temporary = alias.with_name(f".{alias.name}.{os.getpid()}.tmp")
    temporary.symlink_to(target)
    os.replace(temporary, alias)
    return alias


def alias_path_suffix(alias: Path) -> str:
    search_dirs = {
        Path(entry).expanduser().resolve()
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry
    }
    if alias.parent.resolve() in search_dirs:
        return ""
    return f" · lc off PATH: {alias.parent}"


def is_lazy_command_hook(command: object) -> bool:
    if not isinstance(command, str):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return len(argv) == 2 and argv[1] == "hook" and Path(argv[0]).name in {"lazy-command", "semantic_command.py"}


def install(workspace: Path | None, user: bool) -> int:
    if user:
        config_dir = Path.home() / ".codex"
        scope = "user-level"
        alias = install_short_alias()
    else:
        assert workspace is not None
        workspace = workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise SystemExit("workspace does not exist or is not a directory")
        config_dir = workspace / ".codex"
        scope = "workspace-local"
    config_dir.mkdir(mode=0o755, exist_ok=True)
    toml_path = config_dir / "config.toml"
    if toml_path.exists():
        toml = toml_path.read_text(encoding="utf-8")
        declares_hooks = re.search(r"(?m)^\s*\[hooks\]\s*(?:#.*)?$", toml) or re.search(
            r"(?m)^\s*\[\[hooks\.(?!state(?:\.|\]\]))", toml
        )
        if declares_hooks:
            raise SystemExit("workspace already declares TOML hooks; merge manually")
    path = config_dir / "hooks.json"
    if path.exists():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise SystemExit("existing hooks.json is invalid") from error
    else:
        document = {"hooks": {}}
    if not isinstance(document, dict) or not isinstance(document.get("hooks"), dict):
        raise SystemExit("existing hooks.json has an unsupported shape")
    pre = document["hooks"].setdefault("PreToolUse", [])
    if not isinstance(pre, list):
        raise SystemExit("existing PreToolUse hooks have an unsupported shape")
    source = Path(__file__).resolve()
    candidate = shutil.which("lazy-command")
    executable = candidate if candidate and Path(candidate).resolve() == source else str(source)
    command = f"{shlex.quote(executable)} hook"
    # Drop hook entries left by an earlier install from another location (a
    # moved clone or an older copy) so a reinstall never runs a dead hook.
    for group in pre:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            group["hooks"] = [
                handler for handler in group["hooks"]
                if not (isinstance(handler, dict) and handler.get("command") != command
                        and is_lazy_command_hook(handler.get("command")))
            ]
    pre[:] = [group for group in pre if not (isinstance(group, dict) and group.get("hooks") == [])]
    for group in pre:
        if not isinstance(group, dict):
            continue
        for handler in group.get("hooks", []):
            if isinstance(handler, dict) and handler.get("command") == command:
                suffix = f" · alias {alias}{alias_path_suffix(alias)}" if user else ""
                print(f"already installed · {path}{suffix}")
                return 0
    pre.append({
        "matcher": "Bash",
        "hooks": [{
            "type": "command",
            "command": command,
            "timeout": 10,
            "statusMessage": "Compressing noisy command",
        }],
    })
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(rendered)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    path.chmod(0o644)
    suffix = f" · alias {alias}{alias_path_suffix(alias)}" if user else ""
    print(f"installed · {path} · {scope}{suffix} · restart/new session required")
    return 0


def shell_command(
    command: str, cwd: str | None, raw_limit: int | None = None, baseline: str | None = None,
) -> int:
    """Run one command through the receipt path when tool hooks are unavailable."""
    if not command.strip():
        raise SystemExit("shell command must not be empty")
    if "\0" in command:
        raise SystemExit("shell command must not contain NUL")
    resolved_cwd = Path(cwd or os.getcwd()).expanduser().resolve()
    if not resolved_cwd.is_dir():
        raise SystemExit("shell cwd is not a directory")
    if baseline is not None:
        from receipt_compare import validate_baseline
        validate_baseline(baseline, command, os.fspath(resolved_cwd))
    from semantic_command_hook import store

    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "permission_mode": "dontAsk",
        "cwd": os.fspath(resolved_cwd),
        "session_id": "lazy-command-shell",
        "turn_id": "direct",
        "tool_use_id": f"direct-{uuid.uuid4().hex}",
        "tool_input": {
            "command": command,
            "workdir": os.fspath(resolved_cwd),
        },
    }
    identifier = store(payload, command)
    return run(identifier, os.fspath(resolved_cwd), raw_limit, baseline)


def shell_text(parts: list[str]) -> str:
    if parts[:1] == ["--"]:
        parts = parts[1:]
    if len(parts) == 1:
        return parts[0]
    return shlex.join(parts)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=__doc__,
        usage="%(prog)s [-C PATH] [-r | --since ID] COMMAND\n       %(prog)s ACTION ...",
        epilog=(
            "shortcut: run a command as `lc 'COMMAND'`; add -r for bounded raw "
            "output, --since ID for an explicit receipt delta, or -C PATH for another directory"
        ),
    )
    commands = root.add_subparsers(dest="action", required=True)
    compare_parser = commands.add_parser("compare", help="compare two stored results without rerunning")
    compare_parser.add_argument("before")
    compare_parser.add_argument("after")
    compare_parser.add_argument("--context", type=int, default=3)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("id")
    run_parser.add_argument("--cwd")
    show_parser = commands.add_parser("show", aliases=["s"])
    show_parser.add_argument("id")
    show_parser.add_argument("-r", "--raw", action="store_true")
    show_parser.add_argument("--offset", type=int, help="start at this raw byte offset")
    show_parser.add_argument("--limit", type=int, help="raw page bytes (maximum 8000)")
    show_parser.add_argument("--lines", metavar="START:END", help="show inclusive captured lines")
    show_parser.add_argument("--find", metavar="TEXT", help="find literal text in captured output")
    show_parser.add_argument("--context", type=int, help="context lines for --find (0-100; default 3)")
    probe_parser = commands.add_parser("probe")
    probe_parser.add_argument("id")
    probe_parser.add_argument("--cwd")
    fallback_parser = commands.add_parser("fallback")
    fallback_parser.add_argument("id")
    fallback_parser.add_argument("--cwd")
    install_parser = commands.add_parser("install")
    install_scope = install_parser.add_mutually_exclusive_group(required=True)
    install_scope.add_argument("--workspace", type=Path)
    install_scope.add_argument("--user", action="store_true")
    feedback_parser = commands.add_parser("feedback")
    feedback_parser.add_argument("--workspace", type=Path)
    feedback_parser.add_argument("--since-hours", type=float)
    feedback_parser.add_argument("--top", type=int, default=5)
    shell_parser = commands.add_parser("shell", aliases=["sh"])
    shell_parser.add_argument("-C", "--cwd")
    shell_view = shell_parser.add_mutually_exclusive_group()
    shell_view.add_argument("-r", "--raw", action="store_true")
    shell_view.add_argument("--since", help="show changes from this receipt; command still runs once")
    shell_parser.add_argument("--raw-limit", type=int, default=INLINE_RAW_LIMIT)
    shell_parser.add_argument("command", nargs=argparse.REMAINDER)
    commands.add_parser("hook")
    return root


def command_argv(argv: list[str]) -> list[str]:
    if not argv or argv[0] in COMMAND_ACTIONS or argv[0] in {"-h", "--help"}:
        return argv
    return ["sh", *argv]


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(command_argv(arguments))
    if args.action == "compare":
        from receipt_compare import compare
        return compare(args.before, args.after, args.context)
    if args.action == "run":
        return run(args.id, args.cwd)
    if args.action in {"show", "s"}:
        return show(args.id, args.raw, args.offset, args.limit, args.lines, args.find, args.context)
    if args.action == "probe":
        return probe(args.id)
    if args.action == "fallback":
        return fallback(args.id, args.cwd)
    if args.action == "install":
        return install(args.workspace, args.user)
    if args.action == "feedback":
        if args.since_hours is not None and not 0 < args.since_hours <= 24 * 3650:
            raise SystemExit("since-hours must be greater than zero and at most 87600")
        if not 0 <= args.top <= 20:
            raise SystemExit("top must be from 0 to 20")
        return feedback(args.workspace, args.since_hours, args.top)
    if args.action in {"shell", "sh"}:
        if not 1 <= args.raw_limit <= 1_000_000:
            raise SystemExit("raw-limit must be from 1 to 1000000 bytes")
        return shell_command(
            shell_text(args.command),
            args.cwd,
            args.raw_limit if args.raw else None,
            args.since,
        )
    from semantic_command_hook import main as hook_main
    return hook_main()


if __name__ == "__main__":
    raise SystemExit(main())
