#!/usr/bin/env python3
"""Record one private monotonic owner checkpoint for a root Codex thread."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


BOUNDARIES = {"yes", "no", "uncertain"}
EFFECT_STATES = {"none", "settled", "issued", "ambiguous"}
BOUNDARY_EVIDENCE = {
    "active-phase",
    "new-task-family",
    "new-user-request",
    "phase-changed",
    "phase-complete",
    "same-task-family",
    "settled-effect",
    "unknown",
}


def thread_key(thread_ref: str) -> str:
    return hashlib.sha256(thread_ref.encode()).hexdigest()


def checkpoint_path(state_root: Path, thread_ref: str) -> Path:
    return state_root / "checkpoints" / f"{thread_key(thread_ref)}.json"


def validate(raw: Any) -> dict[str, Any]:
    keys = {
        "schema",
        "threadRef",
        "checkpointGeneration",
        "durableCheckpointReady",
        "nextActionPresent",
        "semanticBoundary",
        "effectState",
    }
    if not isinstance(raw, dict) or frozenset(raw) not in {
        frozenset(keys),
        frozenset(keys | {"boundaryEvidence"}),
    }:
        raise ValueError("owner checkpoint keys do not match the contract")
    if raw["schema"] != "lazy-compaction-owner-checkpoint/v1":
        raise ValueError("owner checkpoint schema is unsupported")
    if not isinstance(raw["threadRef"], str) or not raw["threadRef"] or len(raw["threadRef"]) > 240:
        raise ValueError("threadRef must be a bounded identifier")
    generation = raw["checkpointGeneration"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("checkpointGeneration must be a positive integer")
    for key in ("durableCheckpointReady", "nextActionPresent"):
        if not isinstance(raw[key], bool):
            raise ValueError(f"{key} must be boolean")
    if raw["semanticBoundary"] not in BOUNDARIES:
        raise ValueError("semanticBoundary is unsupported")
    if raw["effectState"] not in EFFECT_STATES:
        raise ValueError("effectState is unsupported")
    evidence = raw.get("boundaryEvidence", [])
    if (
        not isinstance(evidence, list)
        or len(evidence) > 8
        or any(not isinstance(item, str) for item in evidence)
        or len(evidence) != len(set(evidence))
        or any(item not in BOUNDARY_EVIDENCE for item in evidence)
    ):
        raise ValueError("boundaryEvidence must be unique supported enums")
    value = dict(raw)
    value["boundaryEvidence"] = evidence
    return value


def load_checkpoint(state_root: Path, thread_ref: str) -> dict[str, Any] | None:
    path = checkpoint_path(state_root, thread_ref)
    if not path.exists():
        return None
    checkpoint = validate(json.loads(path.read_text(encoding="utf-8")))
    if checkpoint["threadRef"] != thread_ref:
        raise ValueError("checkpoint belongs to another thread")
    return checkpoint


def record(checkpoint: Any, state_root: Path) -> tuple[Path, bool]:
    value = validate(checkpoint)
    path = checkpoint_path(state_root, value["threadRef"])
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    previous = load_checkpoint(state_root, value["threadRef"])
    if previous is not None:
        old_generation = previous["checkpointGeneration"]
        new_generation = value["checkpointGeneration"]
        if new_generation < old_generation:
            raise ValueError("checkpoint generation moved backwards")
        if new_generation == old_generation:
            if previous != value:
                raise ValueError("same checkpoint generation changed content")
            return path, True
    body = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as target:
            target.write(body)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path, False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--thread-ref", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--boundary", choices=sorted(BOUNDARIES), required=True)
    parser.add_argument("--effect-state", choices=sorted(EFFECT_STATES), required=True)
    parser.add_argument("--evidence", choices=sorted(BOUNDARY_EVIDENCE), action="append", default=[])
    parser.add_argument("--not-ready", action="store_true")
    parser.add_argument("--no-next-action", action="store_true")
    args = parser.parse_args()
    _, replay = record(
        {
            "schema": "lazy-compaction-owner-checkpoint/v1",
            "threadRef": args.thread_ref,
            "checkpointGeneration": args.generation,
            "durableCheckpointReady": not args.not_ready,
            "nextActionPresent": not args.no_next_action,
            "semanticBoundary": args.boundary,
            "effectState": args.effect_state,
            "boundaryEvidence": args.evidence,
        },
        args.state_root,
    )
    print(
        json.dumps(
            {
                "schema": "lazy-compaction-owner-checkpoint-receipt/v1",
                "threadKey": thread_key(args.thread_ref),
                "checkpointGeneration": args.generation,
                "replay": replay,
                "rawConversationRetained": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
