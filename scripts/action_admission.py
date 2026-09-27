#!/usr/bin/env python3
"""Admit a Codex action to one bounded execution or observation route."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


SCRIPT_DIR = Path(__file__).resolve().parent
SAFE_PROJECTION_SCRIPTS = {
    (SCRIPT_DIR / "browser_result_view.py").resolve(),
    (SCRIPT_DIR / "context_view.py").resolve(),
    (SCRIPT_DIR / "json_view.py").resolve(),
    (SCRIPT_DIR / "safe_search.py").resolve(),
    (SCRIPT_DIR / "owner_profiles.py").resolve(),
}
REQUEST_SCHEMA = "lazy-action-request/v1"
DECISION_SCHEMA = "lazy-action-decision/v1"
REPLAY_SCHEMA = "lazy-admission-replay/v1"
ALLOWED_KINDS = {
    "command",
    "browser-observation",
    "tool-observation",
    "wait",
    "effect-retry",
}
ALLOWED_EFFECT_STATES = {"none", "verified-no-effect", "ambiguous", "committed"}
TOP_KEYS = {"schema", "kind", "argv", "cwd", "operation", "scope", "effect_state"}
SCOPE_KEYS = {
    "owner_query_available",
    "semantic_delta_available",
    "selector_bounded",
    "expected_payload_chars",
    "expected_items",
    "visual_semantics",
    "exact_wake_at",
    "observation_only",
}
SAFE_NAME = re.compile(r"[^a-zA-Z0-9_.:-]+")


def safe_name(value: Any, *, fallback: str = "unknown") -> str:
    rendered = SAFE_NAME.sub("-", str(value or "").strip()).strip("-")
    return rendered[:120] or fallback


def sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def private_file(path: Path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def write_private(path: Path, value: str) -> None:
    with private_file(path) as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise SystemExit(f"private artifact mode drifted: {path}")


def require_bool(scope: dict[str, Any], key: str) -> None:
    if key in scope and not isinstance(scope[key], bool):
        raise ValueError(f"scope.{key} must be a boolean")


def validate_request(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("request must be a JSON object")
    unknown = set(raw) - TOP_KEYS
    if unknown:
        raise ValueError(f"unknown request keys: {', '.join(sorted(unknown))}")
    if raw.get("schema") != REQUEST_SCHEMA:
        raise ValueError(f"schema must be {REQUEST_SCHEMA}")
    kind = raw.get("kind")
    if kind not in ALLOWED_KINDS:
        raise ValueError("kind is not supported")
    scope = raw.get("scope") or {}
    if not isinstance(scope, dict):
        raise ValueError("scope must be an object")
    unknown_scope = set(scope) - SCOPE_KEYS
    if unknown_scope:
        raise ValueError(f"unknown scope keys: {', '.join(sorted(unknown_scope))}")
    for key in (
        "owner_query_available",
        "semantic_delta_available",
        "selector_bounded",
        "visual_semantics",
        "observation_only",
    ):
        require_bool(scope, key)
    for key, maximum in (("expected_payload_chars", 100_000_000), ("expected_items", 1_000_000)):
        if key in scope:
            value = scope[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > maximum:
                raise ValueError(f"scope.{key} must be an integer from 0 to {maximum}")
    if "exact_wake_at" in scope:
        value = scope["exact_wake_at"]
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError("scope.exact_wake_at must be a bounded timestamp string")
    argv = raw.get("argv")
    cwd = raw.get("cwd")
    if kind == "command":
        if not isinstance(argv, list) or not argv or len(argv) > 256:
            raise ValueError("command argv must contain 1 to 256 strings")
        if any(not isinstance(part, str) or not part or len(part) > 4096 for part in argv):
            raise ValueError("each command argv entry must be a non-empty bounded string")
    elif argv is not None:
        raise ValueError("argv is only valid for command requests")
    if cwd is not None:
        if kind != "command":
            raise ValueError("cwd is only valid for command requests")
        if not isinstance(cwd, str) or len(cwd) > 4096 or not Path(cwd).is_absolute():
            raise ValueError("cwd must be a bounded absolute path")
    operation = raw.get("operation")
    if operation is not None and (not isinstance(operation, str) or len(operation) > 200):
        raise ValueError("operation must be a bounded string")
    effect_state = raw.get("effect_state")
    if effect_state is not None and effect_state not in ALLOWED_EFFECT_STATES:
        raise ValueError("effect_state is not supported")
    if kind == "effect-retry" and effect_state is None:
        raise ValueError("effect-retry requires effect_state")
    return {
        "schema": REQUEST_SCHEMA,
        "kind": kind,
        "argv": list(argv) if argv else None,
        "cwd": cwd,
        "operation": operation,
        "scope": dict(scope),
        "effect_state": effect_state,
    }


def command_text(argv: list[str]) -> str:
    executable = Path(argv[0]).name.lower()
    if executable in {"bash", "sh", "zsh", "dash"} and len(argv) >= 3 and argv[1] in {"-c", "-lc"}:
        return argv[2].lower()
    return " ".join(argv).lower()


def bounded_sed(text: str) -> bool:
    ranges = re.findall(r"(?:^|\s)['\"]?(\d+),(\d+)p['\"]?(?:\s|$)", text)
    return bool(ranges) and sum(int(end) - int(start) + 1 for start, end in ranges) <= 600


def bounded_tail_stage(text: str) -> bool:
    bounds = re.findall(r"\b(?:head|tail)\s+(?:-n\s*)?-?(\d+)\b", text)
    return bool(bounds) and max(int(value) for value in bounds) <= 600


def safe_read_shell(text: str) -> bool:
    without_null_redirects = re.sub(r"\d?>\s*/dev/null", "", text)
    if ">" in without_null_redirects:
        return False
    mutation = re.compile(
        r"\b(?:rm|mv|cp|touch|mkdir|rmdir|chmod|chown|truncate|dd|tee|install|"
        r"python\d*|node|npm|npx|make|cargo|go)\b|"
        r"\bgit\s+(?:-c\s+\S+\s+)*(?:add|commit|clean|reset|checkout|switch|merge|rebase)\b"
    )
    return not mutation.search(text)


def observation_safe_command(argv: list[str], text: str) -> bool:
    try:
        exact_executable = Path(argv[0]).expanduser().resolve()
    except OSError:
        exact_executable = Path(argv[0])
    if exact_executable in SAFE_PROJECTION_SCRIPTS:
        if exact_executable.name != "owner_profiles.py" or (len(argv) >= 2 and argv[1] == "run"):
            return True
    if (
        Path(argv[0]).name.lower() in {"python", "python3", "python3.14"}
        and len(argv) >= 2
        and Path(argv[1]).expanduser().resolve() in SAFE_PROJECTION_SCRIPTS
    ):
        return True
    executable = Path(argv[0]).name.lower()
    if executable in {
        "cat",
        "date",
        "du",
        "find",
        "grep",
        "head",
        "jq",
        "ls",
        "nl",
        "rg",
        "sed",
        "sha256sum",
        "stat",
        "tail",
        "true",
        "wc",
    }:
        return True
    if executable == "sqlite3":
        return "-readonly" in argv
    if executable == "git":
        return not bool(
            re.search(r"\bgit\s+(?:-c\s+\S+\s+)*(?:add|commit|clean|reset|checkout|switch|merge|rebase)\b", text)
        )
    if executable == "gh":
        return not bool(re.search(r"\b(?:create|edit|close|reopen|merge|delete|comment)\b", text))
    if executable in {"python", "python3", "python3.14", "pytest", "npm", "npx", "cargo", "go"}:
        return bool(
            re.search(r"\b(?:unittest|pytest|npm\s+test|npm\s+run\s+(?:check|typecheck|lint)|cargo\s+(?:test|check)|go\s+test)\b", text)
        )
    if executable in {"bash", "sh", "zsh", "dash"}:
        recognized_observation = bool(
            re.search(
                r"\b(?:cat|date|du|find|git|grep|head|jq|ls|nl|rg|sed|sha256sum|stat|tail|wc|"
                r"unittest|pytest|npm\s+test|npm\s+run\s+(?:check|typecheck|lint))\b",
                text,
            )
        )
        return recognized_observation and safe_read_shell(text)
    return False


def decide_command(request: dict[str, Any]) -> tuple[str, list[str], bool, dict[str, Any]]:
    argv = request["argv"]
    text = command_text(argv)
    scope = request["scope"]
    reasons: set[str] = set()
    next_step: dict[str, Any] = {}
    sleep_match = re.search(r"\bsleep\s+([0-9]+(?:\.[0-9]+)?)", text)
    if sleep_match:
        reasons.add("manual-sleep-detected")
        next_step = {"kind": "exact-wake", "delay_seconds": float(sleep_match.group(1))}
        if scope.get("exact_wake_at"):
            next_step["wake_at"] = scope["exact_wake_at"]
        return "exact-wake", sorted(reasons), False, next_step

    if scope.get("observation_only") and not observation_safe_command(argv, text):
        reasons.add("observation-only-command-not-proven-safe")
        return "reconcile-effect", sorted(reasons), False, {"kind": "operator-decision"}

    first_match = re.match(r"\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*([^\s;&|]+)", text)
    first_command = Path(first_match.group(1)).name if first_match else "unknown"
    if first_command in {"jq", "sha256sum"}:
        reasons.add("text-transform-output-variable")
        return "bounded-read", sorted(reasons), True, {"kind": "execute-once"}
    if first_command in {"git", "gh", "ssh", "verify"}:
        reasons.add("version-remote-or-verifier-output-variable")
        return "bounded-command", sorted(reasons), True, {"kind": "execute-once"}

    if re.search(r"\b(?:unittest|pytest|npm\s+test|npm\s+run\s+check|cargo\s+test|go\s+test)\b", text):
        reasons.add("verification-output-variable")
        return "bounded-command", sorted(reasons), True, {"kind": "execute-once"}
    if re.search(r"\b(?:gh\s+(?:issue|pr|api)|npm\s+(?:install|run)|cargo\s+(?:build|check)|make\b)\b", text):
        reasons.add("remote-or-build-output-variable")
        return "bounded-command", sorted(reasons), True, {"kind": "execute-once"}

    broad_read = bool(
        re.search(
            r"(?:^|[\s;|&])(?:cat|find|rg|grep|sed|head|tail|nl)(?=\s|$)",
            text,
        )
    )
    git_diff_unbounded = bool(
        re.search(r"\bgit\s+(?:-c\s+\S+\s+)*diff\b", text)
        and not re.search(r"--(?:stat|check|name-only|name-status|numstat)\b", text)
    )
    explicitly_bounded = bool(scope.get("selector_bounded"))
    explicitly_bounded = explicitly_bounded or bool(
        re.search(r"\b(?:rg|grep)\b[^\n]*(?:--max-count(?:=|\s)|\s-m\s+\d+)", text)
    )
    explicitly_bounded = explicitly_bounded or bounded_sed(text) or bounded_tail_stage(text)
    if broad_read or git_diff_unbounded:
        if scope.get("owner_query_available"):
            reasons.add("owner-query-precedes-raw-read")
            return "owner-query", sorted(reasons), False, {"kind": "bounded-observation"}
        if not safe_read_shell(text):
            reasons.add("read-mixed-with-non-read-effect")
            return "scoped-observation", sorted(reasons), False, {"kind": "narrow-request"}
        if explicitly_bounded and scope.get("expected_payload_chars", 0) <= 12_000:
            reasons.add("selector-bounded-text-needs-character-cap")
            return "bounded-read", sorted(reasons), True, {"kind": "execute-once"}
        reasons.add("read-only-output-needs-hard-cap")
        return "bounded-read", sorted(reasons), True, {"kind": "execute-once"}

    if re.search(r"\b(?:sqlite3|journalctl|docker\s+logs|kubectl\s+logs)\b", text):
        reasons.add("query-output-variable")
        return "bounded-command", sorted(reasons), True, {"kind": "execute-once"}
    if Path(argv[0]).name.lower() in {"bash", "sh", "zsh", "dash"} and re.search(r"[;&|]", text):
        reasons.add("compound-shell-output-unknown")
        return "bounded-command", sorted(reasons), True, {"kind": "execute-once"}
    reasons.add("known-small-or-caller-bounded")
    return "direct", sorted(reasons), True, {"kind": "execute-once"}


def decide_request(request: dict[str, Any]) -> dict[str, Any]:
    kind = request["kind"]
    scope = request["scope"]
    operation = safe_name(request.get("operation"))
    if kind == "command":
        route, reasons, executable, next_step = decide_command(request)
    elif kind == "wait":
        route, reasons, executable = "exact-wake", ["wait-is-state-not-work"], False
        next_step = {"kind": "exact-wake"}
        if scope.get("exact_wake_at"):
            next_step["wake_at"] = scope["exact_wake_at"]
    elif kind == "effect-retry":
        state = request["effect_state"]
        if state == "ambiguous":
            route, reasons, executable = "reconcile-effect", ["ambiguous-effect-must-not-retry"], False
            next_step = {"kind": "reconcile"}
        elif state == "verified-no-effect":
            route, reasons, executable = "direct", ["verified-no-effect-may-retry-once"], True
            next_step = {"kind": "effect-once"}
        else:
            route, reasons, executable = "effect-settled", [f"effect-state-{state}"], False
            next_step = {"kind": "complete-or-advance"}
    elif kind == "browser-observation":
        lower_operation = operation.lower()
        raw_operations = {
            "domsnapshot",
            "accessibility-tree",
            "body-text",
            "raw-dom",
            "visible-dom",
            "browser-evaluate",
        }
        if scope.get("owner_query_available"):
            route, reasons, executable = "owner-query", ["owner-query-precedes-browser-read"], False
        elif lower_operation in raw_operations and scope.get("semantic_delta_available"):
            route, reasons, executable = "semantic-delta", ["raw-browser-read-has-semantic-delta"], False
        elif lower_operation == "screenshot" and scope.get("visual_semantics"):
            route, reasons, executable = "direct", ["visual-semantics-require-image"], True
        elif scope.get("selector_bounded") and scope.get("expected_payload_chars", 0) <= 12_000:
            route, reasons, executable = "direct", ["browser-selector-and-payload-bounded"], True
        else:
            route, reasons, executable = "scoped-observation", ["browser-read-not-semantically-bounded"], False
        next_step = {"kind": "bounded-observation"}
    else:
        lower_operation = operation.lower()
        if scope.get("owner_query_available") or re.search(r"(?:search|fetch|list|read-thread|list-threads)", lower_operation):
            route, reasons, executable = "owner-query", ["tool-owner-query-available"], False
        elif scope.get("semantic_delta_available"):
            route, reasons, executable = "semantic-delta", ["tool-semantic-delta-available"], False
        elif scope.get("expected_payload_chars", 0) > 12_000 or scope.get("expected_items", 0) > 100:
            route, reasons, executable = "bounded-tool-observation", ["tool-result-projected-large"], False
        else:
            route, reasons, executable = "direct", ["tool-request-bounded"], True
        next_step = {"kind": "bounded-observation" if not executable else "execute-once"}
    decision = {
        "schema": DECISION_SCHEMA,
        "request_sha256": sha256_json(request),
        "kind": kind,
        "operation_class": operation,
        "route": route,
        "original_action_executable": executable,
        "reason_codes": reasons,
        "next": next_step,
        "authority": "unchanged",
        "raw_content_emitted": False,
    }
    return decision


def load_request(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 1_000_000:
        raise SystemExit("request exceeds the 1 MB bound")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return validate_request(raw)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise SystemExit(f"invalid action request: {error}") from error


def split_history_command(command: str) -> list[str]:
    try:
        parsed = shlex.split(command)
    except ValueError:
        parsed = [command or "unknown"]
    return [(part or "unknown")[:4096] for part in (parsed[:256] or ["unknown"])]


def json_chars(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def load_history(db: Path, since_days: int) -> list[tuple[str, str]]:
    if not db.is_file():
        raise SystemExit(f"Codex thread-history database not found: {db}")
    uri = f"file:{quote(str(db.resolve()))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return list(
            connection.execute(
                """SELECT item_type, item_json FROM thread_items
                   WHERE created_at_ms >= (strftime('%s','now', ?) * 1000)
                   ORDER BY created_at_ms, rollout_ordinal""",
                (f"-{since_days} days",),
            )
        )
    finally:
        connection.close()


def nested_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(nested_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(nested_text(item) for item in value)
    return ""


def history_request(item_type: str, item: dict[str, Any]) -> tuple[dict[str, Any], int] | None:
    if item_type == "commandExecution":
        output = str(item.get("aggregatedOutput") or "")
        return validate_request(
            {
                "schema": REQUEST_SCHEMA,
                "kind": "command",
                "argv": split_history_command(str(item.get("command") or "unknown")),
                "cwd": item.get("cwd") if isinstance(item.get("cwd"), str) and Path(item["cwd"]).is_absolute() else None,
                "scope": {},
            }
        ), len(output)
    if item_type not in {"mcpToolCall", "dynamicToolCall"}:
        return None
    server = safe_name(item.get("server") or item.get("namespace")).lower()
    tool = safe_name(item.get("tool")).lower()
    arguments = nested_text(item.get("arguments")).lower()
    payload = item.get("result")
    if payload is None:
        payload = item.get("contentItems")
    chars = json_chars(payload)
    if "node_repl" in server and tool == "js":
        if re.search(r"domsnapshot|get_visible_dom|accessibility|innertext|alltextcontents|document\.body", arguments):
            operation = "raw-dom"
            semantic = True
        elif "documentation" in arguments:
            operation = "browser-documentation"
            semantic = False
        else:
            operation = "browser-evaluate"
            semantic = True
        return validate_request(
            {
                "schema": REQUEST_SCHEMA,
                "kind": "browser-observation",
                "operation": operation,
                "scope": {"semantic_delta_available": semantic},
            }
        ), chars
    owner = bool(re.search(r"(?:search|fetch|list|read_thread|list_threads)", tool))
    return validate_request(
        {
            "schema": REQUEST_SCHEMA,
            "kind": "tool-observation",
            "operation": f"{server}:{tool}",
            "scope": {"owner_query_available": owner},
        }
    ), chars


def replay_history(db: Path, *, since_days: int, large_chars: int) -> dict[str, Any]:
    route_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    large_by_route: Counter[str] = Counter()
    large_payload_chars_by_route: Counter[str] = Counter()
    missed_large_fingerprints: list[str] = []
    total_payload_chars = 0
    projected_payload_chars = 0
    large_items = 0
    caught_large = 0
    small_interventions = 0
    wait_routes = 0
    considered = 0
    route_quality: dict[str, Counter[str]] = {}
    reason_quality: dict[str, Counter[str]] = {}
    for item_type, item_text in load_history(db, since_days):
        try:
            item = json.loads(item_text)
        except json.JSONDecodeError:
            continue
        projected = history_request(item_type, item)
        if projected is None:
            continue
        request, payload_chars = projected
        decision = decide_request(request)
        considered += 1
        total_payload_chars += payload_chars
        route = decision["route"]
        route_counts[route] += 1
        kind_counts[request["kind"]] += 1
        quality = route_quality.setdefault(route, Counter())
        quality["actions"] += 1
        quality["payload_chars"] += payload_chars
        for reason in decision["reason_codes"]:
            reason_row = reason_quality.setdefault(reason, Counter())
            reason_row["actions"] += 1
            reason_row["payload_chars"] += payload_chars
        intervention = route != "direct"
        automatically_executable = route in {"direct", "bounded-command", "bounded-read"}
        deterministic_alternate = route in {"owner-query", "semantic-delta", "bounded-tool-observation"}
        if intervention:
            projected_payload_chars += payload_chars
        if route == "exact-wake":
            wait_routes += 1
        is_large = payload_chars >= large_chars
        if is_large:
            large_items += 1
            quality["large_actions"] += 1
            for reason in decision["reason_codes"]:
                reason_quality[reason]["large_actions"] += 1
            large_by_route[route] += 1
            large_payload_chars_by_route[route] += payload_chars
            if intervention:
                caught_large += 1
            else:
                missed_large_fingerprints.append(decision["request_sha256"])
        elif intervention:
            small_interventions += 1
            quality["small_interventions"] += 1
            quality["automatic_small_projections"] += int(automatically_executable)
            quality["deterministic_alternate_small"] += int(deterministic_alternate)
            quality["blocked_small_transitions"] += int(
                not automatically_executable and not deterministic_alternate
            )
            for reason in decision["reason_codes"]:
                reason_quality[reason]["small_interventions"] += 1
                reason_quality[reason]["automatic_small_projections"] += int(automatically_executable)
                reason_quality[reason]["deterministic_alternate_small"] += int(deterministic_alternate)
                reason_quality[reason]["blocked_small_transitions"] += int(
                    not automatically_executable and not deterministic_alternate
                )
    return {
        "schema": REPLAY_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selector": {"since_days": since_days, "large_payload_chars": large_chars},
        "content_policy": {
            "commands": "hashed_only",
            "arguments": "omitted",
            "tool_results": "counted_only",
            "conversation_content": "omitted",
        },
        "actions_considered": considered,
        "kind_counts": dict(sorted(kind_counts.items())),
        "route_counts": dict(sorted(route_counts.items())),
        "payload": {
            "total_chars": total_payload_chars,
            "chars_requiring_projection": projected_payload_chars,
            "large_items": large_items,
            "large_items_caught": caught_large,
            "large_items_missed": large_items - caught_large,
            "large_catch_rate": round(caught_large / large_items, 4) if large_items else 1.0,
            "large_by_route": dict(sorted(large_by_route.items())),
            "large_payload_chars_by_route": dict(sorted(large_payload_chars_by_route.items())),
        },
        "attention": {
            "exact_wait_routes": wait_routes,
            "small_interventions": small_interventions,
            "automatic_small_projections": sum(
                values.get("automatic_small_projections", 0) for values in route_quality.values()
            ),
            "deterministic_alternate_small": sum(
                values.get("deterministic_alternate_small", 0) for values in route_quality.values()
            ),
            "blocked_small_transitions": sum(
                values.get("blocked_small_transitions", 0) for values in route_quality.values()
            ),
            "missed_large_request_sha256": missed_large_fingerprints[:50],
        },
        "route_quality": {
            name: dict(sorted(values.items())) for name, values in sorted(route_quality.items())
        },
        "reason_quality": {
            name: dict(sorted(values.items())) for name, values in sorted(reason_quality.items())
        },
        "raw_content_emitted": False,
    }


def render_replay(report: dict[str, Any]) -> str:
    payload = report["payload"]
    lines = [
        "# Lazy Commander admission replay",
        "",
        "Raw commands, arguments, tool results, reasoning, and conversation content are omitted.",
        "",
        f"- Actions considered: {report['actions_considered']}",
        f"- Payload characters counted: {payload['total_chars']}",
        f"- Payload characters routed through a projection: {payload['chars_requiring_projection']}",
        f"- Oversized results: {payload['large_items']}",
        f"- Oversized results caught: {payload['large_items_caught']}",
        f"- Oversized results missed: {payload['large_items_missed']}",
        f"- Oversized catch rate: {payload['large_catch_rate']:.2%}",
        f"- Manual waits routed to exact wake: {report['attention']['exact_wait_routes']}",
        f"- Small actions conservatively intercepted: {report['attention']['small_interventions']}",
        f"- Small actions automatically projected: {report['attention']['automatic_small_projections']}",
        f"- Small actions routed to a deterministic alternate: {report['attention']['deterministic_alternate_small']}",
        f"- Small actions requiring a transition: {report['attention']['blocked_small_transitions']}",
        "",
        "## Routes",
        "",
    ]
    for route, count in report["route_counts"].items():
        quality = report["route_quality"][route]
        lines.append(
            f"- `{route}`: {count}; {quality.get('large_actions', 0)} oversized, "
            f"{quality.get('small_interventions', 0)} small interventions"
        )
    lines.append("")
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    subparsers = root.add_subparsers(dest="command", required=True)
    decide = subparsers.add_parser("decide")
    decide.add_argument("--request", type=Path, required=True)
    decide.add_argument("--output", type=Path, required=True)
    run = subparsers.add_parser("run-command")
    run.add_argument("--request", type=Path, required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    replay = subparsers.add_parser("replay")
    replay.add_argument("--db", type=Path, default=Path.home() / ".codex" / "thread_history_1.sqlite")
    replay.add_argument("--since-days", type=int, default=14)
    replay.add_argument("--large-output-chars", type=int, default=32_768)
    replay.add_argument("--output-dir", type=Path, required=True)
    return root


def decide_main(args: argparse.Namespace) -> int:
    request = load_request(args.request)
    decision = decide_request(request)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(decision, indent=2, sort_keys=True) + "\n"
    write_private(args.output, text)
    print(json.dumps({
        "decision": str(args.output),
        "request_sha256": decision["request_sha256"],
        "route": decision["route"],
        "original_action_executable": decision["original_action_executable"],
    }, sort_keys=True))
    return 0


def run_main(args: argparse.Namespace) -> int:
    request = load_request(args.request)
    if request["kind"] != "command":
        raise SystemExit("run-command only accepts command requests")
    if not request.get("cwd"):
        raise SystemExit("run-command requires an explicit absolute cwd")
    run_cwd = Path(request["cwd"])
    if not run_cwd.is_dir():
        raise SystemExit("run-command cwd does not exist or is not a directory")
    decision = decide_request(request)
    admitted_at = datetime.now(timezone.utc)
    if decision["route"] == "exact-wake" and "wake_at" not in decision["next"]:
        delay = float(decision["next"].get("delay_seconds", 0))
        decision["next"]["wake_at"] = (admitted_at + timedelta(seconds=delay)).isoformat()
    decision["admitted_at"] = admitted_at.isoformat()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    decision_path = args.output_dir / "decision.json"
    receipt_path = args.output_dir / "admission-receipt.json"
    if any(path.exists() for path in (decision_path, receipt_path, args.output_dir / "run")):
        raise SystemExit("refusing to overwrite admitted-command artifacts")
    write_private(decision_path, json.dumps(decision, indent=2, sort_keys=True) + "\n")
    executed = decision["route"] in {"direct", "bounded-command", "bounded-read"}
    exit_code: int | None = None
    bounded_stdout_sha256: str | None = None
    if executed:
        bounded = Path(__file__).with_name("bounded_command.py")
        bounded_argv = [
            sys.executable,
            str(bounded),
            "--output-dir",
            str(args.output_dir / "run"),
            "--cwd",
            str(run_cwd),
        ]
        if decision["route"] == "bounded-read":
            bounded_argv.extend(
                [
                    "--view-mode",
                    "prefix",
                    "--max-view-lines",
                    "40",
                    "--max-view-chars",
                    "8000",
                    "--max-output-bytes",
                    "8000000",
                ]
            )
        bounded_argv.extend(["--", *request["argv"]])
        process = subprocess.run(
            bounded_argv,
            check=False,
            capture_output=True,
            text=True,
        )
        exit_code = process.returncode
        bounded_stdout_sha256 = hashlib.sha256(process.stdout.encode("utf-8")).hexdigest()
        if process.stderr:
            raise SystemExit("bounded command runner failed before producing a stable receipt")
    receipt = {
        "schema": "lazy-admitted-command-receipt/v1",
        "request_sha256": decision["request_sha256"],
        "decision_sha256": hashlib.sha256(decision_path.read_bytes()).hexdigest(),
        "route": decision["route"],
        "executed": executed,
        "exit_code": exit_code,
        "bounded_runner_stdout_sha256": bounded_stdout_sha256,
        "raw_content_emitted": False,
    }
    write_private(receipt_path, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"receipt": str(receipt_path), **receipt}, sort_keys=True))
    return exit_code if exit_code is not None else 0


def replay_main(args: argparse.Namespace) -> int:
    if args.since_days < 1 or args.since_days > 3650:
        raise SystemExit("since-days must be between 1 and 3650")
    if args.large_output_chars < 1024 or args.large_output_chars > 10_000_000:
        raise SystemExit("large-output-chars must be between 1024 and 10000000")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / "replay.json"
    view_path = args.output_dir / "view.md"
    receipt_path = args.output_dir / "receipt.json"
    if any(path.exists() for path in (report_path, view_path, receipt_path)):
        raise SystemExit("refusing to overwrite admission-replay artifacts")
    report = replay_history(
        args.db, since_days=args.since_days, large_chars=args.large_output_chars
    )
    report_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    view_text = render_replay(report)
    receipt = {
        "schema": "lazy-admission-replay-receipt/v1",
        "report_sha256": hashlib.sha256(report_text.encode("utf-8")).hexdigest(),
        "view_sha256": hashlib.sha256(view_text.encode("utf-8")).hexdigest(),
        "actions_considered": report["actions_considered"],
        "large_catch_rate": report["payload"]["large_catch_rate"],
        "raw_content_emitted": False,
    }
    write_private(report_path, report_text)
    write_private(view_path, view_text)
    write_private(receipt_path, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"receipt": str(receipt_path), **receipt}, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "decide":
        return decide_main(args)
    if args.command == "run-command":
        return run_main(args)
    return replay_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
