#!/usr/bin/env python3
"""Retain a content-free compaction wake from one Codex Stop hook event."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from compaction_owner_checkpoint import load_checkpoint, record, thread_key


KEYS = {
    "session_id",
    "turn_id",
    "transcript_path",
    "cwd",
    "hook_event_name",
    "model",
    "permission_mode",
    "stop_hook_active",
    "last_assistant_message",
}
OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@-]{0,239}$")


def _bounded_string(raw: dict[str, Any], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ValueError(f"{key} must be a bounded non-empty string")
    return value


def project(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != KEYS:
        raise ValueError("Stop hook keys do not match the contract")
    if raw["hook_event_name"] != "Stop":
        raise ValueError("only root Stop events are supported")
    thread_ref = _bounded_string(raw, "session_id")
    turn_ref = _bounded_string(raw, "turn_id")
    if not OPAQUE.fullmatch(thread_ref) or not OPAQUE.fullmatch(turn_ref):
        raise ValueError("thread and turn references must be opaque identifiers")
    cwd = _bounded_string(raw, "cwd")
    if not Path(cwd).is_absolute():
        raise ValueError("cwd must be absolute")
    model = _bounded_string(raw, "model")
    permission_mode = _bounded_string(raw, "permission_mode")
    if not isinstance(raw["stop_hook_active"], bool):
        raise ValueError("stop_hook_active must be boolean")
    transcript_path = raw["transcript_path"]
    if transcript_path is not None and not isinstance(transcript_path, str):
        raise ValueError("transcript_path must be string or null")
    message = raw["last_assistant_message"]
    if message is not None and not isinstance(message, str):
        raise ValueError("last_assistant_message must be string or null")
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    message_bytes = (message or "").encode()
    return {
        "schema": "lazy-compaction-stop-event/v1",
        "threadRef": thread_ref,
        "turnRef": turn_ref,
        "cwd": cwd,
        "model": model,
        "permissionMode": permission_mode,
        "stopHookActive": raw["stop_hook_active"],
        "transcriptPresent": transcript_path is not None,
        "assistantMessagePresent": message is not None,
        "assistantMessageChars": len(message or ""),
        "assistantMessageSha256": hashlib.sha256(message_bytes).hexdigest(),
        "sourceSha256": hashlib.sha256(canonical).hexdigest(),
        "stableEvent": "stop-hook",
        "idleProven": False,
        "authorizesCompaction": False,
        "rawConversationRetained": False,
    }


def persist(receipt: dict[str, Any], state_root: Path) -> tuple[Path, bool]:
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_root, 0o700)
    # Stop hooks may run more than once inside one turn when another hook asks
    # the model to continue. Content hash distinguishes those stable wakes.
    identity = (
        f'{receipt["threadRef"]}\0{receipt["turnRef"]}\0{receipt["sourceSha256"]}'
    ).encode()
    path = state_root / f"{hashlib.sha256(identity).hexdigest()}.json"
    body = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != body:
            raise ValueError("same Stop event identity changed projection")
        return path, True
    with os.fdopen(fd, "wb") as target:
        target.write(body)
        target.flush()
        os.fsync(target.fileno())
    return path, False


def checkpoint_from_stop(receipt: dict[str, Any], state_root: Path) -> tuple[int, bool]:
    """Reserve one monotonic checkpoint generation for an exact Stop event."""
    key = thread_key(receipt["threadRef"])
    locks = state_root / "stop-checkpoint-locks"
    intents = state_root / "stop-checkpoint-intents"
    locks.mkdir(mode=0o700, parents=True, exist_ok=True)
    intents.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(locks, 0o700)
    os.chmod(intents, 0o700)
    lock_fd = os.open(locks / f"{key}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        intent_path = intents / f'{key}-{receipt["sourceSha256"]}.json'
        if intent_path.exists():
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            generation = intent["checkpointGeneration"]
            replay = True
        else:
            current = load_checkpoint(state_root, receipt["threadRef"])
            generation = (current["checkpointGeneration"] if current else 0) + 1
            intent = {
                "schema": "lazy-compaction-stop-checkpoint-intent/v1",
                "threadRef": receipt["threadRef"],
                "sourceSha256": receipt["sourceSha256"],
                "checkpointGeneration": generation,
            }
            body = (json.dumps(intent, indent=2, sort_keys=True) + "\n").encode()
            fd = os.open(intent_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as target:
                target.write(body)
                target.flush()
                os.fsync(target.fileno())
            replay = False
        current = load_checkpoint(state_root, receipt["threadRef"])
        if current is None or current["checkpointGeneration"] <= generation:
            record(
                {
                    "schema": "lazy-compaction-owner-checkpoint/v1",
                    "threadRef": receipt["threadRef"],
                    "checkpointGeneration": generation,
                    "durableCheckpointReady": receipt["transcriptPresent"],
                    "nextActionPresent": receipt["assistantMessagePresent"],
                    "semanticBoundary": "no",
                    "effectState": "none",
                    "boundaryEvidence": ["new-user-request", "unknown"],
                },
                state_root,
            )
        return generation, replay
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path.home() / ".codex" / "state" / "lazy-commander" / "compaction-events",
    )
    parser.add_argument(
        "--checkpoint-state-root",
        type=Path,
        default=Path.home() / ".codex" / "state" / "lazy-commander" / "compaction-proxy",
    )
    args = parser.parse_args()
    receipt = project(json.load(sys.stdin))
    persist(receipt, args.state_root)
    checkpoint_from_stop(receipt, args.checkpoint_state_root)
    # Empty valid Stop-hook output. Receipts stay private; nothing enters the turn.
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
