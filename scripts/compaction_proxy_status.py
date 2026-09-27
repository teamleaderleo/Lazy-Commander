#!/usr/bin/env python3
"""Emit one bounded status row for the owning compaction proxy runtime."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


def process_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def observation_present(state_root: Path, runtime_id: str, event: str) -> bool:
    path = state_root / "runtimes" / f"{runtime_id}-{event}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    return (
        value.get("schema") == "lazy-compaction-proxy-observation/v1"
        and value.get("runtimeId") == runtime_id
        and value.get("event") == event
        and value.get("rawConversationRetained") is False
    )


def runtime_is_active(state_root: Path, runtime: dict[str, Any]) -> bool:
    runtime_id = runtime.get("runtimeId")
    if not isinstance(runtime_id, str):
        return False
    if (state_root / "runtimes" / f"{runtime_id}-stopped.json").exists():
        return False
    return process_alive(runtime.get("proxyPid")) and process_alive(runtime.get("childPid"))


def root_thread_observed(state_root: Path, runtime_id: str) -> bool:
    return observation_present(state_root, runtime_id, "root-thread")


def latest_status(state_root: Path) -> dict[str, Any]:
    candidates: list[tuple[int, Path, dict[str, Any]]] = []
    for path in (state_root / "runtimes").glob("*-started.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        started = value.get("startedAtUnixNs")
        if value.get("schema") == "lazy-compaction-proxy-runtime/v1" and isinstance(started, int):
            candidates.append((started, path, value))
    if not candidates:
        return {"schema": "lazy-compaction-proxy-status/v1", "state": "not-started"}

    active = [item for item in candidates if runtime_is_active(state_root, item[2])]
    if active:
        rooted = [
            item
            for item in active
            if isinstance(item[2].get("runtimeId"), str)
            and root_thread_observed(state_root, item[2]["runtimeId"])
        ]
        _, _, runtime = max(rooted or active, key=lambda item: item[0])
    else:
        _, _, runtime = max(candidates, key=lambda item: item[0])

    runtime_id = runtime["runtimeId"]
    stop_path = state_root / "runtimes" / f"{runtime_id}-stopped.json"
    if stop_path.exists():
        state = "stopped"
    elif runtime_is_active(state_root, runtime):
        state = "active"
    else:
        state = "stale"
    return {
        "schema": "lazy-compaction-proxy-status/v1",
        "state": state,
        "runtimeId": runtime_id,
        "apply": runtime.get("apply") is True,
        "realCodexVersion": runtime.get("realCodexVersion", "unavailable"),
        "startedAtUnixNs": runtime["startedAtUnixNs"],
        "rootThreadObserved": observation_present(state_root, runtime_id, "root-thread"),
        "rootIdleObserved": observation_present(state_root, runtime_id, "root-idle"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path.home() / ".codex" / "state" / "lazy-commander" / "compaction-proxy",
    )
    parser.add_argument("--require-active", action="store_true")
    parser.add_argument("--require-apply", action="store_true")
    parser.add_argument("--require-root-idle", action="store_true")
    parser.add_argument("--require-version")
    args = parser.parse_args()
    status = latest_status(args.state_root)
    print(json.dumps(status, separators=(",", ":"), sort_keys=True))
    ok = not args.require_active or status["state"] == "active"
    ok = ok and (not args.require_apply or status.get("apply") is True)
    ok = ok and (not args.require_root_idle or status.get("rootIdleObserved") is True)
    ok = ok and (
        args.require_version is None
        or args.require_version in status.get("realCodexVersion", "")
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
