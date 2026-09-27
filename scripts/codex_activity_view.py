#!/usr/bin/env python3
"""Project verbose Codex thread activity into a bounded content-free audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


SAFE_NAME = re.compile(r"[^a-zA-Z0-9_.:-]+")


def bounded_int(value: str, *, minimum: int, maximum: int, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{label} must be an integer") from error
    if parsed < minimum or parsed > maximum:
        raise argparse.ArgumentTypeError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return parsed


def canonical_timestamp(value: str) -> str:
    rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def timestamp_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed - epoch
    return (
        delta.days * 86_400_000
        + delta.seconds * 1000
        + delta.microseconds // 1000
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument(
        "--db",
        type=Path,
        default=Path.home() / ".codex" / "thread_history_1.sqlite",
    )
    root.add_argument("--output-dir", type=Path, required=True)
    root.add_argument("--thread-id")
    since = root.add_mutually_exclusive_group()
    since.add_argument(
        "--since-days",
        type=lambda value: bounded_int(
            value, minimum=1, maximum=3650, label="since-days"
        ),
    )
    since.add_argument("--since-at", type=canonical_timestamp)
    root.add_argument("--until-at", type=canonical_timestamp)
    root.add_argument(
        "--large-output-chars",
        type=lambda value: bounded_int(
            value, minimum=1024, maximum=10_000_000, label="large-output-chars"
        ),
        default=32_768,
    )
    root.add_argument(
        "--max-groups",
        type=lambda value: bounded_int(
            value, minimum=1, maximum=100, label="max-groups"
        ),
        default=20,
    )
    return root


def private_file(path: Path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def safe_name(value: Any, *, fallback: str = "unknown") -> str:
    rendered = SAFE_NAME.sub("-", str(value or "").strip()).strip("-")
    return rendered[:120] or fallback


def json_chars(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def tool_result_metrics(value: Any) -> dict[str, int]:
    """Separate model-visible semantic content from transport/UI envelopes."""

    total = json_chars(value)
    if not isinstance(value, dict) or not isinstance(value.get("content"), list):
        return {"payload_chars": total, "envelope_chars": 0, "result_chars": total}
    payload = 0
    binary = 0
    for block in value["content"]:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("text"), str):
            payload += len(block["text"])
        if isinstance(block.get("data"), str):
            binary += len(block["data"])
        resource = block.get("resource")
        if isinstance(resource, dict):
            if isinstance(resource.get("text"), str):
                payload += len(resource["text"])
            if isinstance(resource.get("blob"), str):
                binary += len(resource["blob"])
    structured = value.get("structuredContent")
    if payload == 0 and structured is not None:
        payload = json_chars(structured)
    useful = payload + binary
    return {
        "payload_chars": useful,
        "envelope_chars": max(0, total - useful),
        "result_chars": total,
    }


def command_labels(command: str, actions: list[dict[str, Any]]) -> set[str]:
    lower = command.lower()
    labels: set[str] = set()
    if re.search(r"\bsleep\s", lower):
        labels.add("wait:sleep")
    if re.search(r"\bdate(?:\s|$)", lower):
        labels.add("observe:clock")
    if re.search(r"\bgit\s+(?:-c\s+\S+\s+)*status\b", lower):
        labels.add("vcs:status")
    if re.search(r"\bgit\s+(?:-c\s+\S+\s+)*diff\b", lower):
        labels.add("vcs:diff")
    if re.search(r"\bgit\s+(?:-c\s+\S+\s+)*(?:add|commit|clean)\b", lower):
        labels.add("vcs:mutation")
    if re.search(r"\b(?:sed|rg|find|head|tail|nl)\b", lower):
        labels.add("observe:text-or-files")
    if re.search(r"\b(?:unittest|pytest|npm\s+test|npm\s+run\s+check)\b", lower):
        labels.add("verify:tests")
    if re.search(r"\b(?:validate|verification|diff\s+--check|py_compile|typecheck)\b", lower):
        labels.add("verify:static")
    if re.search(r"\bgh\s+(?:issue|pr|api|repo)\b", lower):
        labels.add("remote:github")
    if re.search(r"(?:chrome-is-running|installed-browsers|check-extension|browser-client)", lower):
        labels.add("browser:diagnostic")
    if "desktop_cursor.py" in lower:
        labels.add("desktop:control")
    for action in actions:
        action_type = safe_name(action.get("type"))
        if action_type != "unknown":
            labels.add(f"action:{action_type}")
    if not labels:
        labels.add("command:other")
    return labels


def add_group(
    groups: dict[str, dict[str, int]],
    name: str,
    *,
    chars: int,
    duration_ms: int,
    failed: bool,
    envelope_chars: int = 0,
) -> None:
    group = groups[name]
    group["runs"] += 1
    group["payload_chars"] += chars
    group["duration_ms"] += duration_ms
    group["failed_runs"] += int(failed)
    group["max_payload_chars"] = max(group["max_payload_chars"], chars)
    group["envelope_chars"] += envelope_chars


def ranked(groups: dict[str, dict[str, int]], limit: int) -> list[dict[str, Any]]:
    rows = [{"name": name, **values} for name, values in groups.items()]
    rows.sort(
        key=lambda row: (
            row["payload_chars"], row["duration_ms"], row["runs"], row["name"]
        ),
        reverse=True,
    )
    return rows[:limit]


def load_items(
    db: Path,
    *,
    thread_id: str | None,
    since_days: int | None,
    since_at: str | None,
    until_at: str | None,
) -> list[tuple[str, str, int]]:
    if not db.is_file():
        raise SystemExit(f"Codex thread-history database not found: {db}")
    uri = f"file:{quote(str(db.resolve()))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        clauses: list[str] = []
        parameters: list[Any] = []
        if thread_id:
            clauses.append("thread_id = ?")
            parameters.append(thread_id)
        if since_days:
            clauses.append("created_at_ms >= (strftime('%s','now', ?) * 1000)")
            parameters.append(f"-{since_days} days")
        if since_at:
            clauses.append("created_at_ms >= ?")
            parameters.append(timestamp_ms(since_at))
        if until_at:
            clauses.append("created_at_ms < ?")
            parameters.append(timestamp_ms(until_at))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = (
            "SELECT item_type, item_json, created_at_ms FROM thread_items"
            + where
            + " ORDER BY created_at_ms, rollout_ordinal"
        )
        return list(connection.execute(query, parameters))
    finally:
        connection.close()


def build_report(
    items: list[tuple[str, str, int]],
    *,
    selector: dict[str, Any],
    large_output_chars: int,
    max_groups: int,
) -> dict[str, Any]:
    item_types: Counter[str] = Counter()
    command_actions: Counter[str] = Counter()
    command_fingerprints: Counter[str] = Counter()
    command_groups: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "runs": 0,
            "payload_chars": 0,
            "duration_ms": 0,
            "failed_runs": 0,
            "max_payload_chars": 0,
            "envelope_chars": 0,
        }
    )
    tool_groups: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "runs": 0,
            "payload_chars": 0,
            "duration_ms": 0,
            "failed_runs": 0,
            "max_payload_chars": 0,
            "envelope_chars": 0,
        }
    )
    commands = Counter()
    tools = Counter()
    file_changes = Counter()
    first_ms: int | None = None
    last_ms: int | None = None

    for item_type, item_text, created_at_ms in items:
        item_types[item_type] += 1
        first_ms = created_at_ms if first_ms is None else min(first_ms, created_at_ms)
        last_ms = created_at_ms if last_ms is None else max(last_ms, created_at_ms)
        try:
            item = json.loads(item_text)
        except json.JSONDecodeError:
            continue
        if item_type == "commandExecution":
            command = str(item.get("command") or "")
            output_chars = len(str(item.get("aggregatedOutput") or ""))
            duration_ms = int(item.get("durationMs") or 0)
            failed = item.get("status") != "completed"
            actions = item.get("commandActions") or []
            if not isinstance(actions, list):
                actions = []
            actions = [action for action in actions if isinstance(action, dict)]
            commands["runs"] += 1
            commands["payload_chars"] += output_chars
            commands["duration_ms"] += duration_ms
            commands["failed_runs"] += int(failed)
            commands["zero_payload_runs"] += int(output_chars == 0)
            commands["large_payload_runs"] += int(output_chars >= large_output_chars)
            fingerprint = hashlib.sha256(command.encode("utf-8")).hexdigest()
            command_fingerprints[fingerprint] += 1
            labels = command_labels(command, actions)
            commands["wait_runs"] += int("wait:sleep" in labels)
            commands["wait_duration_ms"] += duration_ms if "wait:sleep" in labels else 0
            for action in actions:
                command_actions[safe_name(action.get("type"))] += 1
            for label in labels:
                add_group(
                    command_groups,
                    label,
                    chars=output_chars,
                    duration_ms=duration_ms,
                    failed=failed,
                )
        elif item_type in {"mcpToolCall", "dynamicToolCall"}:
            server = safe_name(item.get("server") or item.get("namespace"))
            tool = safe_name(item.get("tool"))
            payload = item.get("result")
            if payload is None:
                payload = item.get("contentItems")
            metrics = tool_result_metrics(payload)
            payload_chars = metrics["payload_chars"]
            duration_ms = int(item.get("durationMs") or 0)
            failed = item.get("status") not in {"completed", "success"}
            group_name = f"{server}:{tool}"
            tools["runs"] += 1
            tools["payload_chars"] += payload_chars
            tools["duration_ms"] += duration_ms
            tools["failed_runs"] += int(failed)
            tools["zero_payload_runs"] += int(payload_chars == 0)
            tools["large_payload_runs"] += int(payload_chars >= large_output_chars)
            tools["result_chars"] += metrics["result_chars"]
            tools["envelope_chars"] += metrics["envelope_chars"]
            tools["large_envelope_runs"] += int(metrics["envelope_chars"] >= large_output_chars)
            add_group(
                tool_groups,
                group_name,
                chars=payload_chars,
                duration_ms=duration_ms,
                failed=failed,
                envelope_chars=metrics["envelope_chars"],
            )
        elif item_type == "fileChange":
            changes = item.get("changes") or []
            if not isinstance(changes, list):
                changes = []
            diffs = [
                change.get("diff")
                for change in changes
                if isinstance(change, dict) and isinstance(change.get("diff"), str)
            ]
            diff_chars = sum(len(diff) for diff in diffs)
            failed = item.get("status") not in {"completed", "success"}
            file_changes["runs"] += 1
            file_changes["files"] += len(changes)
            file_changes["payload_chars"] += diff_chars
            file_changes["failed_runs"] += int(failed)
            file_changes["zero_payload_runs"] += int(diff_chars == 0)
            file_changes["large_payload_runs"] += int(diff_chars >= large_output_chars)
            file_changes["max_payload_chars"] = max(
                file_changes["max_payload_chars"], diff_chars
            )

    repeated = sorted(
        (
            {"command_sha256": fingerprint, "runs": count}
            for fingerprint, count in command_fingerprints.items()
            if count > 1
        ),
        key=lambda row: (row["runs"], row["command_sha256"]),
        reverse=True,
    )
    commands["exact_repeat_runs"] = sum(row["runs"] - 1 for row in repeated)
    commands["unique_commands"] = len(command_fingerprints)
    tool_and_command_payload_chars = commands["payload_chars"] + tools["payload_chars"]
    total_payload_chars = tool_and_command_payload_chars + file_changes["payload_chars"]
    opportunities: list[dict[str, Any]] = []
    if commands["large_payload_runs"]:
        opportunities.append(
            {
                "code": "large-command-output",
                "occurrences": commands["large_payload_runs"],
                "mechanism": "bounded semantic command view",
            }
        )
    if tools["large_payload_runs"]:
        opportunities.append(
            {
                "code": "large-tool-output",
                "occurrences": tools["large_payload_runs"],
                "mechanism": "owner projection or semantic delta",
            }
        )
    if tools["large_envelope_runs"]:
        opportunities.append(
            {
                "code": "large-tool-envelope",
                "occurrences": tools["large_envelope_runs"],
                "mechanism": "tool-result projection that omits transport and UI metadata",
            }
        )
    if commands["wait_runs"]:
        opportunities.append(
            {
                "code": "manual-waiting",
                "occurrences": commands["wait_runs"],
                "duration_ms": commands["wait_duration_ms"],
                "mechanism": "exact wake or event-backed resume",
            }
        )
    if commands["exact_repeat_runs"]:
        opportunities.append(
            {
                "code": "exact-command-repetition",
                "occurrences": commands["exact_repeat_runs"],
                "mechanism": "idempotent transition command",
            }
        )
    if commands["failed_runs"]:
        opportunities.append(
            {
                "code": "failed-command-attention",
                "occurrences": commands["failed_runs"],
                "mechanism": "preflight plus explicit effect settlement",
            }
        )
    if file_changes["large_payload_runs"]:
        opportunities.append(
            {
                "code": "large-file-change",
                "occurrences": file_changes["large_payload_runs"],
                "mechanism": "private artifact plus content-free change receipt",
            }
        )

    return {
        "schema": "codex-activity-view/v3",
        "selector": selector,
        "content_policy": {
            "raw_commands": "omitted",
            "command_arguments": "omitted",
            "tool_arguments": "omitted",
            "tool_semantic_content": "counted_only",
            "tool_transport_and_ui_envelopes": "counted_separately",
            "file_change_paths": "omitted",
            "file_change_diffs": "counted_only",
            "conversation_content": "omitted",
        },
        "window": {
            "first_created_at_ms": first_ms,
            "last_created_at_ms": last_ms,
            "items": len(items),
        },
        "item_types": dict(sorted(item_types.items())),
        "attention": {
            "semantic_payload_chars": total_payload_chars,
            "tool_and_command_payload_chars": tool_and_command_payload_chars,
            "file_change_payload_chars": file_changes["payload_chars"],
            "tool_envelope_chars": tools["envelope_chars"],
            "estimated_3000_character_pages": round(total_payload_chars / 3000, 1),
            "opportunities": opportunities,
        },
        "commands": {
            **dict(commands),
            "action_types": dict(sorted(command_actions.items())),
            "groups": ranked(command_groups, max_groups),
            "repeated_fingerprints": repeated[:max_groups],
        },
        "tools": {**dict(tools), "groups": ranked(tool_groups, max_groups)},
        "file_changes": dict(file_changes),
    }


def render_view(report: dict[str, Any]) -> str:
    commands = report["commands"]
    tools = report["tools"]
    file_changes = report["file_changes"]
    attention = report["attention"]
    lines = [
        "# Codex activity view",
        "",
        "This projection omits command text, arguments, tool payloads, and conversation content.",
        "",
        f"- Items: {report['window']['items']}",
        f"- Command runs: {commands.get('runs', 0)}",
        f"- Tool runs: {tools.get('runs', 0)}",
        f"- Semantic payload characters: {attention['semantic_payload_chars']}",
        f"- Tool/command payload characters: {attention['tool_and_command_payload_chars']}",
        f"- File-change diff characters: {attention['file_change_payload_chars']}",
        f"- File-change runs: {file_changes.get('runs', 0)}",
        f"- Tool transport/UI envelope characters: {attention['tool_envelope_chars']}",
        f"- Approximate 3,000-character pages: {attention['estimated_3000_character_pages']}",
        f"- Exact repeated command runs: {commands.get('exact_repeat_runs', 0)}",
        f"- Manual wait runs: {commands.get('wait_runs', 0)}",
        f"- Failed command runs: {commands.get('failed_runs', 0)}",
        "",
        "## Deterministic opportunities",
        "",
    ]
    for opportunity in attention["opportunities"]:
        extra = ""
        if "duration_ms" in opportunity:
            extra = f", {opportunity['duration_ms']} ms"
        lines.append(
            f"- `{opportunity['code']}`: {opportunity['occurrences']} occurrence(s){extra}; "
            f"use {opportunity['mechanism']}."
        )
    if not attention["opportunities"]:
        lines.append("- No thresholded opportunity was observed.")
    lines.extend(["", "## Largest semantic groups", ""])
    for group in commands.get("groups", [])[:10]:
        lines.append(
            f"- command `{group['name']}`: {group['runs']} runs, "
            f"{group['payload_chars']} payload chars, {group['duration_ms']} ms"
        )
    for group in tools.get("groups", [])[:10]:
        lines.append(
            f"- tool `{group['name']}`: {group['runs']} runs, "
            f"{group['payload_chars']} semantic chars, {group['envelope_chars']} envelope chars, "
            f"{group['duration_ms']} ms"
        )
    lines.append("")
    return "\n".join(lines)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / "activity.json"
    view_path = args.output_dir / "view.md"
    receipt_path = args.output_dir / "receipt.json"
    if any(path.exists() for path in (report_path, view_path, receipt_path)):
        raise SystemExit("refusing to overwrite Codex activity-view artifacts")
    selector = {
        "thread_id": args.thread_id,
        "since_days": args.since_days,
        "since_at": args.since_at,
        "until_at": args.until_at,
        "large_output_chars": args.large_output_chars,
    }
    if args.since_at and args.until_at and timestamp_ms(args.since_at) >= timestamp_ms(args.until_at):
        raise SystemExit("since-at must be earlier than until-at")
    items = load_items(
        args.db,
        thread_id=args.thread_id,
        since_days=args.since_days,
        since_at=args.since_at,
        until_at=args.until_at,
    )
    report = build_report(
        items,
        selector=selector,
        large_output_chars=args.large_output_chars,
        max_groups=args.max_groups,
    )
    report_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    view_text = render_view(report)
    receipt = {
        "schema": "codex-activity-receipt/v3",
        "activity_sha256": sha256_text(report_text),
        "view_sha256": sha256_text(view_text),
        "items": report["window"]["items"],
        "payload_chars_counted": report["attention"]["tool_and_command_payload_chars"],
        "semantic_chars_counted": report["attention"]["semantic_payload_chars"],
        "file_change_chars_counted": report["attention"]["file_change_payload_chars"],
        "tool_envelope_chars_counted": report["attention"]["tool_envelope_chars"],
        "raw_content_emitted": False,
    }
    receipt_text = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    for path, value in (
        (report_path, report_text),
        (view_path, view_text),
        (receipt_path, receipt_text),
    ):
        with private_file(path) as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise SystemExit(f"private artifact mode drifted: {path}")
    print(json.dumps({"receipt": str(receipt_path), **receipt}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
