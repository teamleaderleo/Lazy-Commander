#!/usr/bin/env python3
"""Choose the next compaction transition from bounded checkpoint state."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


SCHEMA = "lazy-compaction-checkpoint/v1"
BOUNDARIES = {"yes", "no", "uncertain"}
EFFECT_STATES = {"none", "settled", "issued", "ambiguous"}
CLASSIFIER_DECISIONS = {"now", "soon", "keep"}
CLASSIFIER_REASONS = {
    "now": ("stable-boundary",),
    "soon": ("checkpoint-first",),
    "keep": ("active-phase",),
}
KEYS = {
    "schema",
    "threadRef",
    "usedTokens",
    "contextWindow",
    "checkpointGeneration",
    "lastCompactedGeneration",
    "durableCheckpointReady",
    "semanticBoundary",
    "effectState",
}
OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@-]{0,239}$")


def validate(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != KEYS:
        raise ValueError("compaction checkpoint keys do not match the contract")
    if raw["schema"] != SCHEMA:
        raise ValueError("compaction checkpoint schema is unsupported")
    if not isinstance(raw["threadRef"], str) or not OPAQUE.fullmatch(raw["threadRef"]):
        raise ValueError("threadRef must be a bounded opaque identifier")
    for key in ("usedTokens", "contextWindow", "checkpointGeneration", "lastCompactedGeneration"):
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
    if raw["contextWindow"] < 1 or raw["usedTokens"] > raw["contextWindow"]:
        raise ValueError("token counts are inconsistent")
    if raw["lastCompactedGeneration"] > raw["checkpointGeneration"]:
        raise ValueError("checkpoint generation moved backwards")
    if not isinstance(raw["durableCheckpointReady"], bool):
        raise ValueError("durableCheckpointReady must be a boolean")
    if raw["semanticBoundary"] not in BOUNDARIES:
        raise ValueError("semanticBoundary is unsupported")
    if raw["effectState"] not in EFFECT_STATES:
        raise ValueError("effectState is unsupported")
    return dict(raw)


def advise(raw: Any) -> dict[str, Any]:
    state = validate(raw)
    ratio = state["usedTokens"] / state["contextWindow"]
    fresh = state["checkpointGeneration"] > state["lastCompactedGeneration"]
    reasons: list[str] = []
    if state["effectState"] in {"issued", "ambiguous"}:
        action = "reconcile-effect"
        reasons.append("effect-state-must-settle-before-context-rewrite")
    elif state["semanticBoundary"] == "yes":
        action = "compact" if state["durableCheckpointReady"] and fresh else "checkpoint-now"
        reasons.append(
            "fresh-semantic-boundary"
            if action == "compact"
            else "semantic-boundary-needs-fresh-checkpoint"
        )
    elif state["semanticBoundary"] == "uncertain":
        action = "classify-boundary"
        reasons.append("cheap-classifier-before-expensive-replay")
    elif not state["durableCheckpointReady"] or not fresh:
        action = "checkpoint-now" if ratio >= 0.70 else "continue"
        reasons.append(
            "pressure-needs-fresh-durable-checkpoint"
            if action == "checkpoint-now"
            else "no-fresh-checkpoint-yet"
        )
    elif ratio >= 0.70:
        action = "compact"
        reasons.append("pressure-backstop-and-fresh-checkpoint")
    else:
        action = "continue"
        reasons.append("active-phase-has-no-boundary")
    return {
        "schema": "lazy-compaction-decision/v1",
        "requestSha256": hashlib.sha256(
            json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "threadRef": state["threadRef"],
        "usedWindowPct": round(100 * ratio, 1),
        "action": action,
        "reasonCodes": reasons,
        "classifierContract": (
            {
                "question": "compact before the next expensive turn?",
                "answers": ["now", "soon", "keep"],
                "reasonEnums": CLASSIFIER_REASONS,
                "input": "bounded durable boundary card only",
                "forbidden": ["transcript", "raw logs", "repository content"],
            }
            if action == "classify-boundary"
            else None
        ),
        "authorizesCompaction": action == "compact",
        "rawConversationRequired": False,
    }


def validate_classification(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"decision", "reason"}:
        raise ValueError("classifier result keys do not match the contract")
    decision = raw["decision"]
    reason = raw["reason"]
    if decision not in CLASSIFIER_DECISIONS:
        raise ValueError("classifier decision is unsupported")
    if reason not in CLASSIFIER_REASONS[decision]:
        raise ValueError("classifier reason does not match its decision")
    return {"decision": decision, "reason": reason}


def _pending_checkpoint(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "lazy-compaction-pending/v1",
        "threadRef": state["threadRef"],
        "prerequisite": "checkpoint-first",
        "afterCheckpointGeneration": state["checkpointGeneration"],
    }


def resolve_classification(raw_state: Any, raw_classification: Any) -> dict[str, Any]:
    state = validate(raw_state)
    classification = validate_classification(raw_classification)
    if state["semanticBoundary"] != "uncertain":
        raise ValueError("classifier may resolve only an uncertain boundary")
    base = advise(state)
    if base["action"] == "reconcile-effect":
        action = "reconcile-effect"
        pending = None
    elif classification["decision"] == "keep":
        action = "continue"
        pending = None
    else:
        fresh = state["checkpointGeneration"] > state["lastCompactedGeneration"]
        if state["durableCheckpointReady"] and fresh:
            action = "compact"
            pending = None
        else:
            action = "checkpoint-now"
            pending = _pending_checkpoint(state)
    return {
        "schema": "lazy-compaction-classifier-resolution/v1",
        "threadRef": state["threadRef"],
        "classifierDecision": classification["decision"],
        "classifierReason": classification["reason"],
        "action": action,
        "pending": pending,
        "authorizesCompaction": action == "compact",
        "rawConversationRequired": False,
    }


def advance_pending(raw_state: Any, raw_pending: Any) -> dict[str, Any]:
    state = validate(raw_state)
    required = {
        "schema",
        "threadRef",
        "prerequisite",
        "afterCheckpointGeneration",
    }
    if not isinstance(raw_pending, dict) or set(raw_pending) != required:
        raise ValueError("pending compaction keys do not match the contract")
    if raw_pending["schema"] != "lazy-compaction-pending/v1":
        raise ValueError("pending compaction schema is unsupported")
    if raw_pending["threadRef"] != state["threadRef"]:
        raise ValueError("pending compaction belongs to another thread")
    if raw_pending["prerequisite"] != "checkpoint-first":
        raise ValueError("pending prerequisite is unsupported")
    generation = raw_pending["afterCheckpointGeneration"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ValueError("pending checkpoint generation is invalid")
    fresh = state["checkpointGeneration"] > generation
    if state["effectState"] in {"issued", "ambiguous"}:
        action = "reconcile-effect"
    elif fresh and state["durableCheckpointReady"]:
        action = "compact"
    else:
        action = "checkpoint-now"
    return {
        "schema": "lazy-compaction-pending-resolution/v1",
        "threadRef": state["threadRef"],
        "action": action,
        "pending": None if action == "compact" else dict(raw_pending),
        "authorizesCompaction": action == "compact",
        "rawConversationRequired": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--classification", type=Path)
    mode.add_argument("--pending", type=Path)
    args = parser.parse_args()
    with args.request.open(encoding="utf-8") as source:
        state = json.load(source)
    if args.classification:
        with args.classification.open(encoding="utf-8") as source:
            result = resolve_classification(state, json.load(source))
    elif args.pending:
        with args.pending.open(encoding="utf-8") as source:
            result = advance_pending(state, json.load(source))
    else:
        result = advise(state)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
