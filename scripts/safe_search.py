#!/usr/bin/env python3
"""Run ripgrep behind explicit consumer budgets and emit a bounded projection."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


SCHEMA = "lazy-safe-search-view/v3"
ROW_COLUMNS = [
    "line",
    "textSuffix",
    "charactersOmittedBefore",
    "charactersOmittedAfter",
]


def bounded(value: str, *, minimum: int, maximum: int, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{label} must be an integer") from error
    if parsed < minimum or parsed > maximum:
        raise argparse.ArgumentTypeError(f"{label} must be from {minimum} to {maximum}")
    return parsed


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("pattern")
    result.add_argument("paths", nargs="+")
    result.add_argument("--glob", action="append", default=[])
    result.add_argument("--max-matches", required=True, type=lambda v: bounded(v, minimum=1, maximum=500, label="max-matches"))
    result.add_argument("--max-line-chars", required=True, type=lambda v: bounded(v, minimum=32, maximum=2000, label="max-line-chars"))
    result.add_argument("--max-view-chars", required=True, type=lambda v: bounded(v, minimum=1024, maximum=50000, label="max-view-chars"))
    return result


def render_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) + 1


def shared_prefix(values: list[str]) -> str:
    if len(values) < 2:
        return ""
    prefix = values[0]
    for value in values[1:]:
        while prefix and not value.startswith(prefix):
            prefix = prefix[:-1]
        if not prefix:
            break
    return prefix


def worth_factoring(prefix: str, values: list[str], *, field_cost: int) -> bool:
    """Factor only when the repeated characters outweigh schema overhead."""
    return len(prefix) * (len(values) - 1) > field_cost


def match_neighborhood(
    text: str,
    submatches: list[dict[str, Any]],
    max_chars: int,
) -> tuple[str, int, int]:
    """Keep a bounded character window around the first selected byte match."""
    if len(text) <= max_chars:
        return text, 0, 0
    encoded = text.encode("utf-8")
    first = submatches[0] if submatches else {}
    byte_start = first.get("start", 0)
    byte_end = first.get("end", byte_start)
    if not isinstance(byte_start, int) or not isinstance(byte_end, int):
        byte_start = byte_end = 0
    byte_start = min(max(byte_start, 0), len(encoded))
    byte_end = min(max(byte_end, byte_start), len(encoded))
    start = len(encoded[:byte_start].decode("utf-8", errors="ignore"))
    end = len(encoded[:byte_end].decode("utf-8", errors="ignore"))
    match_chars = max(1, end - start)
    if match_chars >= max_chars:
        low = start
    else:
        low = max(0, start - (max_chars - match_chars) // 2)
    high = min(len(text), low + max_chars)
    low = max(0, high - max_chars)
    return text[low:high], low, len(text) - high


def encode_matches(matches: list[dict[str, Any]]) -> dict[str, Any]:
    """Losslessly front-code selected projected matches in stable search order."""
    groups: list[dict[str, Any]] = []
    for match in matches:
        path = str(match["path"])
        if not groups or groups[-1]["_path"] != path:
            groups.append({"_path": path, "_matches": []})
        groups[-1]["_matches"].append(match)

    paths = [str(group["_path"]) for group in groups]
    path_prefix = shared_prefix(paths)
    if not worth_factoring(path_prefix, paths, field_cost=18):
        path_prefix = ""

    encoded_groups: list[dict[str, Any]] = []
    for group in groups:
        group_matches = list(group["_matches"])
        texts = [str(match["text"]) for match in group_matches]
        text_prefix = shared_prefix(texts)
        if not worth_factoring(text_prefix, texts, field_cost=18):
            text_prefix = ""
        encoded_groups.append(
            {
                "pathSuffix": str(group["_path"])[len(path_prefix) :],
                "textPrefix": text_prefix,
                "rows": [
                    [
                        match["line"],
                        str(match["text"])[len(text_prefix) :],
                        match["charactersOmittedBefore"],
                        match["charactersOmittedAfter"],
                    ]
                    for match in group_matches
                ],
            }
        )
    return {
        "kind": "shared-prefix-groups",
        "pathPrefix": path_prefix,
        "rowColumns": ROW_COLUMNS,
        "groups": encoded_groups,
    }


def make_projection(
    args: argparse.Namespace,
    matches: list[dict[str, Any]],
    *,
    line_characters_omitted: int,
    path_characters_omitted: int,
    match_limit_omitted: int,
    view_budget_omitted: int,
    malformed_records: int,
    stopped_early: bool,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "querySha256": hashlib.sha256(args.pattern.encode("utf-8")).hexdigest(),
        "selectedMatches": len(matches),
        "matchEncoding": encode_matches(matches),
        "budgetsApplied": {
            "maxMatches": args.max_matches,
            "maxLineCharacters": args.max_line_chars,
            "maxViewCharacters": args.max_view_chars,
        },
        "omissions": {
            "lineCharacters": line_characters_omitted,
            "pathCharacters": path_characters_omitted,
            "matchLimit": match_limit_omitted,
            "viewBudget": view_budget_omitted,
            "malformedRecords": malformed_records,
        },
        "searchStoppedEarly": stopped_early,
        "rawDocumentEmitted": False,
        "selectedMatchesLosslesslyEncoded": True,
        "authorizesWork": False,
        "authorizesEffects": False,
        "authorizesDispatch": False,
    }


def project(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    command = ["rg", "--json", "--no-messages", "--sort", "path", "--"]
    for pattern in args.glob:
        command[1:1] = ["--glob", pattern]
    command.extend([args.pattern, *args.paths])
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    matches: list[dict[str, Any]] = []
    line_characters_omitted = 0
    path_characters_omitted = 0
    match_limit_omitted = 0
    view_budget_omitted = 0
    malformed_records = 0
    stopped_early = False
    assert process.stdout is not None
    for raw in process.stdout:
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            malformed_records += 1
            continue
        if record.get("type") != "match":
            continue
        data = record.get("data") or {}
        text = str((data.get("lines") or {}).get("text") or "").rstrip("\r\n")
        path = str((data.get("path") or {}).get("text") or "")
        neighborhood, omitted_before, omitted_after = match_neighborhood(
            text,
            list(data.get("submatches") or []),
            args.max_line_chars,
        )
        omitted = omitted_before + omitted_after
        path_omitted = max(0, len(path) - 500)
        candidate = {
            "path": path[:500],
            "line": data.get("line_number"),
            "text": neighborhood,
            "charactersOmittedBefore": omitted_before,
            "charactersOmittedAfter": omitted_after,
        }
        if len(matches) >= args.max_matches:
            match_limit_omitted += 1
            stopped_early = True
            break
        draft = make_projection(
            args,
            [*matches, candidate],
            line_characters_omitted=line_characters_omitted + omitted,
            path_characters_omitted=path_characters_omitted + path_omitted,
            match_limit_omitted=match_limit_omitted,
            view_budget_omitted=view_budget_omitted,
            malformed_records=malformed_records,
            stopped_early=False,
        )
        if render_size(draft) > args.max_view_chars:
            view_budget_omitted += 1
            stopped_early = True
            break
        matches.append(candidate)
        line_characters_omitted += omitted
        path_characters_omitted += path_omitted
    if stopped_early:
        process.terminate()
    process.stdout.close()
    status = process.wait()
    if stopped_early and status not in {0, 1, -15}:
        status = 0
    projection = make_projection(
        args,
        matches,
        line_characters_omitted=line_characters_omitted,
        path_characters_omitted=path_characters_omitted,
        match_limit_omitted=match_limit_omitted,
        view_budget_omitted=view_budget_omitted,
        malformed_records=malformed_records,
        stopped_early=stopped_early,
    )
    return projection, status


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if len(args.paths) > 64 or any(len(value) > 4096 for value in [args.pattern, *args.paths, *args.glob]):
        raise SystemExit("search inputs exceed structural bounds")
    view, status = project(args)
    encoded = json.dumps(view, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) + 1 > args.max_view_chars:
        raise SystemExit("mandatory search metadata exceeds the consumer view budget")
    print(encoded)
    return 0 if status in {0, 1, -15} else status


if __name__ == "__main__":
    raise SystemExit(main())
