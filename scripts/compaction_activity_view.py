#!/usr/bin/env python3
"""Project one Codex rollout into a content-free compaction audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any


def bounded_int(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("max-compactions must be between 1 and 100")
    return parsed


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("rollout", type=Path)
    root.add_argument("--output-dir", type=Path, required=True)
    root.add_argument("--max-compactions", type=bounded_int, default=40)
    return root


def private_file(path: Path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def iso_minutes(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    first = datetime.fromisoformat(start.replace("Z", "+00:00"))
    second = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return round((second - first).total_seconds() / 60, 1)


def percentile(values: list[float], name: str) -> float | None:
    if not values:
        return None
    if name == "median":
        return round(statistics.median(values), 1)
    return round(min(values) if name == "min" else max(values), 1)


def token_sample(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, dict) or value.get("type") != "token_count":
        return None
    info = value.get("info")
    if not isinstance(info, dict):
        return None
    usage = info.get("last_token_usage")
    if not isinstance(usage, dict):
        return None
    used = usage.get("input_tokens")
    window = info.get("model_context_window")
    if (
        isinstance(used, bool)
        or not isinstance(used, int)
        or isinstance(window, bool)
        or not isinstance(window, int)
        or used <= 0
        or window <= 0
    ):
        return None
    return used, window


def read_rollout(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not path.is_file():
        raise SystemExit(f"rollout not found: {path}")
    compactions: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    last_usage: tuple[int, int] | None = None
    last_compaction_at: str | None = None
    line_count = 0
    malformed = 0
    with path.open(encoding="utf-8") as source:
        for line_count, line in enumerate(source, 1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(item, dict):
                continue
            payload = item.get("payload")
            sample = token_sample(payload)
            if sample:
                if waiting:
                    for record in waiting:
                        record["afterInputTokens"] = sample[0]
                        before = record.get("beforeInputTokens")
                        if isinstance(before, int) and before > 0:
                            reclaimed = max(0, before - sample[0])
                            record["reclaimedInputTokens"] = reclaimed
                            record["inputReductionPct"] = round(
                                100 * reclaimed / before, 1
                            )
                    waiting.clear()
                last_usage = sample
            if item.get("type") != "compacted" or not isinstance(payload, dict):
                continue
            timestamp = item.get("timestamp")
            before, window = last_usage or (None, None)
            record = {
                "windowNumber": payload.get("window_number"),
                "timestamp": timestamp,
                "minutesSincePrevious": iso_minutes(last_compaction_at, timestamp),
                "beforeInputTokens": before,
                "modelContextWindow": window,
                "beforeWindowPct": (
                    round(100 * before / window, 1) if before and window else None
                ),
                "replacementItems": (
                    len(payload["replacement_history"])
                    if isinstance(payload.get("replacement_history"), list)
                    else None
                ),
                "afterInputTokens": None,
                "reclaimedInputTokens": None,
                "inputReductionPct": None,
            }
            compactions.append(record)
            waiting.append(record)
            last_compaction_at = timestamp if isinstance(timestamp, str) else None
    return compactions, {
        "lines": line_count,
        "malformedLines": malformed,
        "bytes": path.stat().st_size,
        "lastInputTokens": last_usage[0] if last_usage else None,
        "lastContextWindow": last_usage[1] if last_usage else None,
    }


def build_report(path: Path, max_compactions: int) -> dict[str, Any]:
    compactions, source = read_rollout(path)
    ratios = [row["beforeWindowPct"] for row in compactions if row["beforeWindowPct"] is not None]
    intervals = [row["minutesSincePrevious"] for row in compactions if row["minutesSincePrevious"] is not None]
    reductions = [row["inputReductionPct"] for row in compactions if row["inputReductionPct"] is not None]
    selected = compactions[-max_compactions:]
    return {
        "schema": "lazy-compaction-activity/v1",
        "source": {
            "pathSha256": hashlib.sha256(str(path.resolve()).encode()).hexdigest(),
            **source,
        },
        "compactions": {
            "count": len(compactions),
            "rows": selected,
            "omittedRows": len(compactions) - len(selected),
            "beforeWindowPct": {
                "min": percentile(ratios, "min"),
                "median": percentile(ratios, "median"),
                "max": percentile(ratios, "max"),
            },
            "minutesBetween": {
                "min": percentile(intervals, "min"),
                "median": percentile(intervals, "median"),
                "max": percentile(intervals, "max"),
            },
            "inputReductionPct": {
                "min": percentile(reductions, "min"),
                "median": percentile(reductions, "median"),
                "max": percentile(reductions, "max"),
            },
        },
        "limits": {"maxCompactions": max_compactions, "rawContentEmitted": False},
        "caveat": "beforeWindowPct is total model input, not body-after-prefix charge",
    }


def render_view(report: dict[str, Any]) -> str:
    compact = report["compactions"]
    source = report["source"]
    current_pct = (
        round(100 * source["lastInputTokens"] / source["lastContextWindow"], 1)
        if source["lastInputTokens"] and source["lastContextWindow"]
        else None
    )
    return "\n".join(
        [
            "# Compaction activity",
            "",
            f"Compactions: {compact['count']}. Omitted rows: {compact['omittedRows']}.",
            f"Before window % min/median/max: {compact['beforeWindowPct']['min']} / {compact['beforeWindowPct']['median']} / {compact['beforeWindowPct']['max']}.",
            f"Minutes between min/median/max: {compact['minutesBetween']['min']} / {compact['minutesBetween']['median']} / {compact['minutesBetween']['max']}.",
            f"Input reduction % min/median/max: {compact['inputReductionPct']['min']} / {compact['inputReductionPct']['median']} / {compact['inputReductionPct']['max']}.",
            f"Current total-input window %: {current_pct}.",
            "",
            "Caveat: total input is not the configured body-after-prefix charge.",
            "Raw conversation content emitted: no.",
            "",
        ]
    )


def main() -> int:
    args = parser().parse_args()
    report = build_report(args.rollout, args.max_compactions)
    args.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output_dir.chmod(0o700)
    report_path = args.output_dir / "activity.json"
    view_path = args.output_dir / "view.md"
    receipt_path = args.output_dir / "receipt.json"
    if any(path.exists() for path in (report_path, view_path, receipt_path)):
        raise SystemExit("refusing to overwrite compaction receipts")
    report_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    view_text = render_view(report)
    with private_file(report_path) as target:
        target.write(report_text)
    with private_file(view_path) as target:
        target.write(view_text)
    receipt = {
        "schema": "lazy-compaction-activity-receipt/v1",
        "activitySha256": hashlib.sha256(report_text.encode()).hexdigest(),
        "viewSha256": hashlib.sha256(view_text.encode()).hexdigest(),
        "rawContentEmitted": False,
    }
    with private_file(receipt_path) as target:
        json.dump(receipt, target, indent=2, sort_keys=True)
        target.write("\n")
    print(view_text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
