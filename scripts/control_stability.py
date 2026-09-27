#!/usr/bin/env python3
"""Record content-free browser-control capability events and project stability."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


OBSERVATION_SCHEMA = "lazy-control-stability-observation/v2"
RECEIPT_SCHEMA = "lazy-control-stability-receipt/v2"
OBSERVATION_KEYS = {
    "schema", "eventId", "controlRef", "controlGeneration", "observedAt",
    "scope", "capabilities",
}
CAPABILITY_KEYS = {
    "inventoryReadable", "projectSurfaceIdentified", "composerWritable",
    "sendControlIdentified",
}
SCOPE_KEYS = {
    "browserFamily", "surfaceProfile", "projectRef", "laneRef",
    "laneGeneration", "controlInventorySha256", "probeVersion",
}
APPROVED_POLICY = {
    "requiredSuccesses": 3,
    "minSpanSeconds": 60,
    "maxAgeSeconds": 300,
}
MAX_FUTURE_SKEW_SECONDS = 5
OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@-]{0,239}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class StabilityError(RuntimeError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def timestamp(value: Any, label: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or not value:
        raise StabilityError(f"{label} must be an ISO-8601 timestamp")
    rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as error:
        raise StabilityError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise StabilityError(f"{label} must include a timezone")
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(), parsed


def opaque(value: Any, label: str) -> str:
    if not isinstance(value, str) or not OPAQUE.fullmatch(value):
        raise StabilityError(f"{label} must be a bounded opaque identifier")
    return value


def positive(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StabilityError(f"{label} must be a positive integer")
    return value


def validate_scope(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != SCOPE_KEYS:
        raise StabilityError("control observation scope fields are not exact")
    for key in ("browserFamily", "surfaceProfile", "projectRef", "laneRef", "probeVersion"):
        opaque(raw.get(key), key)
    positive(raw.get("laneGeneration"), "laneGeneration")
    inventory_sha = raw.get("controlInventorySha256")
    if not isinstance(inventory_sha, str) or not SHA256_RE.fullmatch(inventory_sha):
        raise StabilityError("controlInventorySha256 must be lowercase SHA-256")
    return {key: raw[key] for key in sorted(SCOPE_KEYS)}


def validate_observation(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != OBSERVATION_KEYS:
        raise StabilityError("control observation fields are not exact")
    if raw.get("schema") != OBSERVATION_SCHEMA:
        raise StabilityError(f"schema must be {OBSERVATION_SCHEMA}")
    capabilities = raw.get("capabilities")
    if not isinstance(capabilities, dict) or set(capabilities) != CAPABILITY_KEYS:
        raise StabilityError("capability fields are not exact")
    if any(not isinstance(capabilities[key], bool) for key in CAPABILITY_KEYS):
        raise StabilityError("capabilities must be booleans")
    observed_at, _ = timestamp(raw.get("observedAt"), "observedAt")
    return {
        "schema": OBSERVATION_SCHEMA,
        "eventId": opaque(raw.get("eventId"), "eventId"),
        "controlRef": opaque(raw.get("controlRef"), "controlRef"),
        "controlGeneration": positive(raw.get("controlGeneration"), "controlGeneration"),
        "observedAt": observed_at,
        "scope": validate_scope(raw.get("scope")),
        "capabilities": {key: capabilities[key] for key in sorted(CAPABILITY_KEYS)},
    }


def private_root(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise StabilityError("state root mode drifted")


def connect(root: Path) -> sqlite3.Connection:
    private_root(root)
    database = root / "control-stability.sqlite3"
    connection = sqlite3.connect(database, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS observations (
          event_id TEXT PRIMARY KEY,
          binding_sha256 TEXT NOT NULL,
          binding_json TEXT NOT NULL,
          control_ref TEXT NOT NULL,
          control_generation INTEGER NOT NULL,
          observed_at TEXT NOT NULL,
          success INTEGER NOT NULL CHECK (success IN (0,1)),
          receipt_json TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(control_ref, control_generation, observed_at)
        );
        CREATE TABLE IF NOT EXISTS status_receipts (
          receipt_sha256 TEXT PRIMARY KEY,
          receipt_json TEXT NOT NULL,
          control_ref TEXT NOT NULL,
          control_generation INTEGER NOT NULL,
          scope_sha256 TEXT NOT NULL,
          evaluated_at TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS observations_v2 (
          event_id TEXT PRIMARY KEY,
          binding_sha256 TEXT NOT NULL,
          binding_json TEXT NOT NULL,
          control_ref TEXT NOT NULL,
          control_generation INTEGER NOT NULL,
          scope_sha256 TEXT NOT NULL,
          observed_at TEXT NOT NULL,
          success INTEGER NOT NULL CHECK (success IN (0,1)),
          receipt_json TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(control_ref, control_generation, scope_sha256, observed_at)
        );
        CREATE INDEX IF NOT EXISTS observations_v2_scoped_stream
          ON observations_v2(control_ref,control_generation,scope_sha256,observed_at);
        CREATE INDEX IF NOT EXISTS observations_stream
          ON observations(control_ref, control_generation, observed_at);
        """
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(observations)")}
    if "scope_sha256" not in columns:
        connection.execute("ALTER TABLE observations ADD COLUMN scope_sha256 TEXT")
    for candidate in (
        database,
        database.with_name(database.name + "-wal"),
        database.with_name(database.name + "-shm"),
    ):
        if candidate.exists():
            os.chmod(candidate, 0o600)
    return connection


def policy(required_successes: int, min_span_seconds: int, max_age_seconds: int) -> dict[str, int]:
    required_successes = positive(required_successes, "requiredSuccesses")
    min_span_seconds = positive(min_span_seconds, "minSpanSeconds")
    max_age_seconds = positive(max_age_seconds, "maxAgeSeconds")
    if min_span_seconds > max_age_seconds:
        raise StabilityError("minSpanSeconds cannot exceed maxAgeSeconds")
    return {
        "requiredSuccesses": required_successes,
        "minSpanSeconds": min_span_seconds,
        "maxAgeSeconds": max_age_seconds,
    }


def require_approved_policy(settings: dict[str, int]) -> None:
    if settings != APPROVED_POLICY:
        raise StabilityError("control stability policy is not the approved policy")


def project(
    connection: sqlite3.Connection,
    *,
    control_ref: str,
    control_generation: int,
    scope: dict[str, Any],
    evaluated_at: str,
    settings: dict[str, int],
) -> dict[str, Any]:
    require_approved_policy(settings)
    evaluated_text, evaluated = timestamp(evaluated_at, "evaluatedAt")
    scope = validate_scope(scope)
    scope_sha = digest(scope)
    rows = list(
        connection.execute(
            """SELECT event_id,binding_sha256,observed_at,success
               FROM observations_v2
               WHERE control_ref=? AND control_generation=? AND scope_sha256=?
                 AND observed_at<=?
               ORDER BY observed_at,event_id""",
            (control_ref, control_generation, scope_sha, evaluated_text),
        )
    )
    consecutive: list[sqlite3.Row] = []
    for row in reversed(rows):
        if not bool(row["success"]):
            break
        consecutive.append(row)
    consecutive.reverse()
    latest = rows[-1] if rows else None
    last_at = None
    first_success_at = None
    expires_at = None
    age_seconds = None
    span_seconds = 0
    if latest is not None:
        last_at, last_dt = timestamp(latest["observed_at"], "stored observedAt")
        age_seconds = int((evaluated - last_dt).total_seconds())
        expires_at = (last_dt + timedelta(seconds=settings["maxAgeSeconds"])).isoformat()
    if consecutive:
        first_success_at, first_dt = timestamp(consecutive[0]["observed_at"], "stored observedAt")
        _, last_success_dt = timestamp(consecutive[-1]["observed_at"], "stored observedAt")
        span_seconds = int((last_success_dt - first_dt).total_seconds())

    if latest is None:
        status = "wait"
        reason = "no-capability-observation"
    elif not bool(latest["success"]):
        status = "rotate"
        reason = "latest-capability-observation-failed"
    elif age_seconds is not None and age_seconds > settings["maxAgeSeconds"]:
        status = "rotate"
        reason = "capability-evidence-expired"
    elif len(consecutive) < settings["requiredSuccesses"]:
        status = "wait"
        reason = "insufficient-consecutive-successes"
    elif span_seconds < settings["minSpanSeconds"]:
        status = "wait"
        reason = "success-window-too-short"
    else:
        status = "stable"
        reason = "capability-window-satisfied"

    if status == "stable":
        next_step = "use-stable-control-before-expiry"
        wake_at = expires_at
    elif status == "rotate":
        next_step = "settle-control-rotation"
        wake_at = None
    else:
        next_step = "observe-control-on-material-event"
        wake_at = None

    event_fingerprint = digest(
        [
            {"eventId": row["event_id"], "bindingSha256": row["binding_sha256"]}
            for row in rows
        ]
    )
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "controlRef": control_ref,
        "controlGeneration": control_generation,
        "scope": scope,
        "scopeSha256": scope_sha,
        "evaluatedAt": evaluated_text,
        "status": status,
        "reasonCode": reason,
        "observations": len(rows),
        "consecutiveSuccesses": len(consecutive),
        "firstSuccessAt": first_success_at,
        "lastObservationAt": last_at,
        "successSpanSeconds": span_seconds,
        "evidenceAgeSeconds": age_seconds,
        "expiresAt": expires_at,
        "wakeAt": wake_at,
        "next": next_step,
        "policy": settings,
        "policySha256": digest(settings),
        "eventSetSha256": event_fingerprint,
        "retryAuthorized": False,
        "rawContentEmitted": False,
        "authorizesWork": False,
        "authorizesEffects": False,
        "authorizesDispatch": False,
    }
    receipt["receiptSha256"] = digest(receipt)
    return receipt


def record(
    connection: sqlite3.Connection,
    observation: dict[str, Any],
    settings: dict[str, int],
) -> str:
    require_approved_policy(settings)
    binding_json = canonical(observation)
    binding_sha = hashlib.sha256(binding_json.encode("utf-8")).hexdigest()
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute(
            "SELECT binding_sha256,receipt_json FROM observations_v2 WHERE event_id=?",
            (observation["eventId"],),
        ).fetchone()
        if existing is not None:
            if existing["binding_sha256"] != binding_sha:
                raise StabilityError("event replay changed stable binding")
            if existing["receipt_json"] is None:
                raise StabilityError("event replay found an incomplete durable receipt")
            connection.commit()
            return existing["receipt_json"]

        _, observed_dt = timestamp(observation["observedAt"], "observedAt")
        if observed_dt > datetime.now(timezone.utc) + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
            raise StabilityError("control observation is too far in the future")

        scope_sha = digest(observation["scope"])

        latest = connection.execute(
            """SELECT observed_at FROM observations_v2
               WHERE control_ref=? AND control_generation=? AND scope_sha256=?
               ORDER BY observed_at DESC LIMIT 1""",
            (observation["controlRef"], observation["controlGeneration"], scope_sha),
        ).fetchone()
        if latest is not None and observation["observedAt"] <= latest["observed_at"]:
            raise StabilityError("control observations must arrive in increasing time order")
        created_at = datetime.now(timezone.utc).isoformat()
        success = int(all(observation["capabilities"].values()))
        connection.execute(
            """INSERT INTO observations_v2(
                 event_id,binding_sha256,binding_json,control_ref,
                 control_generation,observed_at,success,receipt_json,created_at,scope_sha256
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                observation["eventId"], binding_sha, binding_json,
                observation["controlRef"], observation["controlGeneration"],
                observation["observedAt"], success, None, created_at, scope_sha,
            ),
        )
        receipt = project(
            connection,
            control_ref=observation["controlRef"],
            control_generation=observation["controlGeneration"],
            scope=observation["scope"],
            evaluated_at=observation["observedAt"],
            settings=settings,
        )
        receipt["recordedEventId"] = observation["eventId"]
        receipt["recordedEventSha256"] = binding_sha
        receipt["recordedEventSucceeded"] = bool(success)
        receipt["receiptSha256"] = digest({key: value for key, value in receipt.items() if key != "receiptSha256"})
        receipt_json = canonical(receipt)
        connection.execute(
            "UPDATE observations_v2 SET receipt_json=? WHERE event_id=?",
            (receipt_json, observation["eventId"]),
        )
        connection.commit()
        return receipt_json
    except Exception:
        connection.rollback()
        raise


def store_status(connection: sqlite3.Connection, receipt: dict[str, Any]) -> str:
    receipt_json = canonical(receipt)
    receipt_sha = receipt["receiptSha256"]
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute(
            "SELECT receipt_json FROM status_receipts WHERE receipt_sha256=?",
            (receipt_sha,),
        ).fetchone()
        if existing is not None and existing["receipt_json"] != receipt_json:
            raise StabilityError("status receipt digest collision")
        if existing is None:
            connection.execute(
                """INSERT INTO status_receipts(
                     receipt_sha256,receipt_json,control_ref,control_generation,
                     scope_sha256,evaluated_at,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    receipt_sha, receipt_json, receipt["controlRef"],
                    receipt["controlGeneration"], receipt["scopeSha256"],
                    receipt["evaluatedAt"], datetime.now(timezone.utc).isoformat(),
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return receipt_json


def verify_stored_status(connection: sqlite3.Connection, receipt: dict[str, Any]) -> None:
    receipt_json = canonical(receipt)
    stored = connection.execute(
        "SELECT receipt_json FROM status_receipts WHERE receipt_sha256=?",
        (receipt.get("receiptSha256"),),
    ).fetchone()
    if stored is None or stored["receipt_json"] != receipt_json:
        raise StabilityError("control stability receipt lacks durable ledger provenance")
    projected = project(
        connection,
        control_ref=receipt["controlRef"],
        control_generation=receipt["controlGeneration"],
        scope=receipt["scope"],
        evaluated_at=receipt["evaluatedAt"],
        settings=APPROVED_POLICY,
    )
    if canonical(projected) != receipt_json:
        raise StabilityError("control stability receipt does not match durable observations")


def load_stored_status(connection: sqlite3.Connection, receipt_sha256: str) -> dict[str, Any]:
    if not isinstance(receipt_sha256, str) or not SHA256_RE.fullmatch(receipt_sha256):
        raise StabilityError("status receipt SHA-256 is invalid")
    stored = connection.execute(
        "SELECT receipt_json FROM status_receipts WHERE receipt_sha256=?",
        (receipt_sha256,),
    ).fetchone()
    if stored is None:
        raise StabilityError("control stability receipt lacks durable ledger provenance")
    return json.loads(stored["receipt_json"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    record_parser = sub.add_parser("record")
    record_parser.add_argument("--observation", type=Path, required=True)
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--control-ref", required=True)
    status_parser.add_argument("--control-generation", type=int, required=True)
    status_parser.add_argument("--evaluated-at", required=True)
    status_parser.add_argument("--scope", type=Path, required=True)
    status_parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    settings = dict(APPROVED_POLICY)
    connection = connect(args.state_root)
    try:
        if args.command == "record":
            observation = validate_observation(
                json.loads(args.observation.read_text(encoding="utf-8"))
            )
            output = record(connection, observation, settings)
        else:
            receipt = project(
                connection,
                control_ref=opaque(args.control_ref, "controlRef"),
                control_generation=positive(args.control_generation, "controlGeneration"),
                scope=validate_scope(json.loads(args.scope.read_text(encoding="utf-8"))),
                evaluated_at=args.evaluated_at,
                settings=settings,
            )
            output = store_status(connection, receipt)
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as sink:
                    sink.write(output + "\n")
                    sink.flush()
                    os.fsync(sink.fileno())
    except (OSError, json.JSONDecodeError, sqlite3.Error, StabilityError) as error:
        raise SystemExit(str(error)) from error
    finally:
        connection.close()
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
