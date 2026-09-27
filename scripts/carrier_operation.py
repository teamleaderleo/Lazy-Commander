#!/usr/bin/env python3
"""Durable one-shot ledger for retained-carrier sends and response recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control_stability import (
    APPROVED_POLICY,
    StabilityError,
    connect as connect_stability,
    load_stored_status,
    project as project_stability,
    validate_scope,
    verify_stored_status,
)
from offload_router import plan_control_services, validate_inventory


REQUEST_SCHEMA = "lazy-carrier-operation-request/v1"
RECEIPT_SCHEMA = "lazy-carrier-operation-receipt/v1"
TOP_KEYS = {
    "schema", "operationId", "routeRunId", "routeGeneration", "promptSha256",
    "promptCharacters", "projectRef", "laneRef", "laneGeneration",
    "surfaceProfile", "actionConfirmationRef",
}
EFFECT_STATES = {"prepared", "issued", "committed", "verified-no-effect", "ambiguous"}
RESPONSE_STATES = {"not-observed", "pending", "unknown", "complete"}
SETTLEMENTS = {"committed", "verified-no-effect", "ambiguous"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STABILITY_KEYS = {
    "schema", "controlRef", "controlGeneration", "evaluatedAt", "status",
    "scope", "scopeSha256",
    "reasonCode", "observations", "consecutiveSuccesses", "firstSuccessAt",
    "lastObservationAt", "successSpanSeconds", "evidenceAgeSeconds",
    "expiresAt", "wakeAt", "next", "policy", "policySha256",
    "eventSetSha256", "retryAuthorized", "rawContentEmitted",
    "authorizesWork", "authorizesEffects", "authorizesDispatch",
    "receiptSha256",
}


class LedgerError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def bounded_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise LedgerError(f"{name} must be a non-empty string of at most 256 characters")
    return value


def nonnegative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LedgerError(f"{name} must be a non-negative integer")
    return value


def validate_request(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != TOP_KEYS:
        raise LedgerError("operation request fields are not exact")
    if raw.get("schema") != REQUEST_SCHEMA:
        raise LedgerError(f"schema must be {REQUEST_SCHEMA}")
    for name in (
        "operationId", "routeRunId", "projectRef", "laneRef", "surfaceProfile",
        "actionConfirmationRef",
    ):
        bounded_string(raw.get(name), name)
    nonnegative_int(raw.get("routeGeneration"), "routeGeneration")
    nonnegative_int(raw.get("laneGeneration"), "laneGeneration")
    nonnegative_int(raw.get("promptCharacters"), "promptCharacters")
    if not isinstance(raw.get("promptSha256"), str) or not SHA256_RE.fullmatch(raw["promptSha256"]):
        raise LedgerError("promptSha256 must be lowercase SHA-256")
    return dict(raw)


def parsed_timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise LedgerError(f"{name} must be an ISO-8601 timestamp")
    rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as error:
        raise LedgerError(f"{name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise LedgerError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_stability_receipt(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != STABILITY_KEYS:
        raise LedgerError("control stability receipt fields are not exact")
    if raw.get("schema") != "lazy-control-stability-receipt/v2":
        raise LedgerError("control stability receipt schema is unsupported")
    bounded_string(raw.get("controlRef"), "controlRef")
    generation = nonnegative_int(raw.get("controlGeneration"), "controlGeneration")
    if generation < 1:
        raise LedgerError("controlGeneration must be positive")
    evaluated_at = parsed_timestamp(raw.get("evaluatedAt"), "evaluatedAt")
    if raw.get("status") not in {"wait", "stable", "rotate"}:
        raise LedgerError("control stability status is unsupported")
    bounded_string(raw.get("reasonCode"), "reasonCode")
    bounded_string(raw.get("next"), "next")
    for key in ("observations", "consecutiveSuccesses", "successSpanSeconds"):
        nonnegative_int(raw.get(key), key)
    if raw.get("evidenceAgeSeconds") is not None:
        nonnegative_int(raw["evidenceAgeSeconds"], "evidenceAgeSeconds")
    for key in ("firstSuccessAt", "lastObservationAt", "expiresAt", "wakeAt"):
        if raw.get(key) is not None:
            parsed_timestamp(raw[key], key)
    for key in ("retryAuthorized", "rawContentEmitted", "authorizesWork", "authorizesEffects", "authorizesDispatch"):
        if raw.get(key) is not False:
            raise LedgerError("control stability receipt must carry zero authority and no raw content")
    for key in ("policySha256", "eventSetSha256", "receiptSha256"):
        if not isinstance(raw.get(key), str) or not SHA256_RE.fullmatch(raw[key]):
            raise LedgerError(f"{key} must be lowercase SHA-256")
    policy = raw.get("policy")
    if not isinstance(policy, dict) or set(policy) != {"requiredSuccesses", "minSpanSeconds", "maxAgeSeconds"}:
        raise LedgerError("control stability policy fields are not exact")
    for key in ("requiredSuccesses", "minSpanSeconds", "maxAgeSeconds"):
        if nonnegative_int(policy.get(key), key) < 1:
            raise LedgerError(f"{key} must be positive")
    if policy["minSpanSeconds"] > policy["maxAgeSeconds"]:
        raise LedgerError("control stability policy span exceeds maximum age")
    if policy != APPROVED_POLICY:
        raise LedgerError("control stability policy is not the approved policy")
    if raw["policySha256"] != sha256_text(canonical(policy)):
        raise LedgerError("control stability policy digest does not match")
    if raw["status"] == "stable":
        if raw.get("expiresAt") is None or parsed_timestamp(raw["expiresAt"], "expiresAt") < evaluated_at:
            raise LedgerError("stable control evidence must have a future-or-current expiry")
        if raw.get("consecutiveSuccesses", 0) < policy["requiredSuccesses"]:
            raise LedgerError("stable control evidence lacks required successes")
        if raw.get("successSpanSeconds", 0) < policy["minSpanSeconds"]:
            raise LedgerError("stable control evidence lacks the required time span")
    try:
        scope = validate_scope(raw.get("scope"))
    except StabilityError as error:
        raise LedgerError(str(error)) from error
    if raw.get("scopeSha256") != sha256_text(canonical(scope)):
        raise LedgerError("control stability scope digest does not match")
    claimed = raw["receiptSha256"]
    calculated = sha256_text(canonical({key: value for key, value in raw.items() if key != "receiptSha256"}))
    if claimed != calculated:
        raise LedgerError("control stability receipt digest does not match")
    return dict(raw)


def private_root(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise LedgerError("state root mode drifted")


def connect(root: Path) -> sqlite3.Connection:
    private_root(root)
    database = root / "carrier-operations.sqlite3"
    connection = sqlite3.connect(database, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS operations (
          operation_id TEXT PRIMARY KEY,
          binding_sha256 TEXT NOT NULL,
          binding_json TEXT NOT NULL,
          effect_state TEXT NOT NULL,
          attempt_ref TEXT,
          conversation_ref TEXT,
          response_state TEXT NOT NULL,
          artifact_ref TEXT,
          artifact_sha256 TEXT,
          artifact_bytes INTEGER,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          CHECK (effect_state IN ('prepared','issued','committed','verified-no-effect','ambiguous')),
          CHECK (response_state IN ('not-observed','pending','unknown','complete'))
        );
        CREATE TABLE IF NOT EXISTS operation_events (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT,
          operation_id TEXT NOT NULL REFERENCES operations(operation_id),
          event_kind TEXT NOT NULL,
          detail_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(operations)")}
    if "cancel_reason" not in columns:
        connection.execute("ALTER TABLE operations ADD COLUMN cancel_reason TEXT")
    if "cancelled_at" not in columns:
        connection.execute("ALTER TABLE operations ADD COLUMN cancelled_at TEXT")
    for name in (
        "gate_receipt_sha256", "gate_json", "gate_expires_at",
        "gate_event_set_sha256", "gated_at",
    ):
        if name not in columns:
            connection.execute(f"ALTER TABLE operations ADD COLUMN {name} TEXT")
    for candidate in (database, database.with_name(database.name + "-wal"), database.with_name(database.name + "-shm")):
        if candidate.exists():
            os.chmod(candidate, 0o600)
    return connection


def event(connection: sqlite3.Connection, operation_id: str, kind: str, detail: dict[str, Any]) -> None:
    connection.execute(
        "INSERT INTO operation_events(operation_id,event_kind,detail_json,created_at) VALUES(?,?,?,?)",
        (operation_id, kind, canonical(detail), now_iso()),
    )


def load(connection: sqlite3.Connection, operation_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if row is None:
        raise LedgerError("operation does not exist")
    return row


def base_receipt(row: sqlite3.Row, outcome: str) -> dict[str, Any]:
    binding = json.loads(row["binding_json"])
    return {
        "schema": RECEIPT_SCHEMA,
        "outcome": outcome,
        "operationId": row["operation_id"],
        "bindingSha256": row["binding_sha256"],
        "routeRunId": binding["routeRunId"],
        "routeGeneration": binding["routeGeneration"],
        "promptSha256": binding["promptSha256"],
        "promptCharacters": binding["promptCharacters"],
        "projectRef": binding["projectRef"],
        "laneRef": binding["laneRef"],
        "laneGeneration": binding["laneGeneration"],
        "surfaceProfile": binding["surfaceProfile"],
        "actionConfirmationRef": binding["actionConfirmationRef"],
        "effectState": "cancelled" if row["cancel_reason"] is not None else row["effect_state"],
        "cancelReason": row["cancel_reason"],
        "attemptRef": row["attempt_ref"],
        "conversationRef": row["conversation_ref"],
        "responseState": row["response_state"],
        "artifactRef": row["artifact_ref"],
        "artifactSha256": row["artifact_sha256"],
        "artifactBytes": row["artifact_bytes"],
        "retryAuthorized": False,
        "authorizesWork": False,
        "authorizesEffects": False,
        "authorizesDispatch": False,
        "rawContentEmitted": False,
        "gateReceiptSha256": row["gate_receipt_sha256"],
    }


def prepare(connection: sqlite3.Connection, request: dict[str, Any]) -> dict[str, Any]:
    binding_json = canonical(request)
    binding_sha = sha256_text(binding_json)
    operation_id = request["operationId"]
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            timestamp = now_iso()
            connection.execute(
                """INSERT INTO operations(
                     operation_id,binding_sha256,binding_json,effect_state,
                     attempt_ref,conversation_ref,response_state,artifact_ref,
                     artifact_sha256,artifact_bytes,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    operation_id, binding_sha, binding_json, "prepared", None, None,
                    "not-observed", None, None, None, timestamp, timestamp,
                ),
            )
            event(connection, operation_id, "prepared", {"bindingSha256": binding_sha})
            outcome = "prepared"
        elif row["binding_sha256"] != binding_sha:
            raise LedgerError("operation replay changed stable binding")
        else:
            outcome = "replay"
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return base_receipt(load(connection, operation_id), outcome)


def issue(
    connection: sqlite3.Connection,
    stability_connection: sqlite3.Connection,
    operation_id: str,
    binding_sha: str,
    gate_receipt_sha: str,
) -> dict[str, Any]:
    if not isinstance(gate_receipt_sha, str) or not SHA256_RE.fullmatch(gate_receipt_sha):
        raise LedgerError("gate receipt SHA-256 is invalid")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = load(connection, operation_id)
        if row["binding_sha256"] != binding_sha:
            raise LedgerError("issue binding does not match prepared operation")
        if row["cancel_reason"] is not None:
            raise LedgerError("cancelled operation cannot issue")
        if row["gate_receipt_sha256"] != gate_receipt_sha or row["gate_json"] is None:
            raise LedgerError("issue lacks the exact durable gate acceptance")
        gate = json.loads(row["gate_json"])
        if gate.get("gateReceiptSha256") != gate_receipt_sha:
            raise LedgerError("durable gate acceptance is inconsistent")
        if gate.get("outcome") != "ready-to-confirm-and-issue":
            raise LedgerError("durable gate acceptance does not admit issue")
        admitted = False
        if row["effect_state"] == "prepared":
            current_time = datetime.now(timezone.utc)
            if parsed_timestamp(row["gate_expires_at"], "gate expiresAt") < current_time:
                raise LedgerError("durable gate acceptance expired before issue")
            stored_stability = load_stored_status(
                stability_connection, gate["controlStabilityReceiptSha256"]
            )
            current_stability = project_stability(
                stability_connection,
                control_ref=stored_stability["controlRef"],
                control_generation=stored_stability["controlGeneration"],
                scope=stored_stability["scope"],
                evaluated_at=current_time.isoformat(),
                settings=APPROVED_POLICY,
            )
            if current_stability["status"] != "stable":
                raise LedgerError("control stability is no longer valid at issue time")
            if current_stability["eventSetSha256"] != row["gate_event_set_sha256"]:
                raise LedgerError("control stability event set changed before issue")
            attempt_ref = f"attempt:{uuid.uuid4()}"
            timestamp = now_iso()
            connection.execute(
                "UPDATE operations SET effect_state='issued',attempt_ref=?,updated_at=? WHERE operation_id=?",
                (attempt_ref, timestamp, operation_id),
            )
            event(connection, operation_id, "issued", {"attemptRef": attempt_ref})
            outcome = "issued"
            admitted = True
        else:
            outcome = "replay"
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    receipt = base_receipt(load(connection, operation_id), outcome)
    receipt["effectAttemptAdmitted"] = admitted
    receipt["next"] = "attempt-send-once" if admitted else "reconcile-or-observe-only"
    return receipt


def cancel(connection: sqlite3.Connection, operation_id: str, reason_code: str) -> dict[str, Any]:
    bounded_string(reason_code, "reasonCode")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = load(connection, operation_id)
        if row["cancel_reason"] is None:
            if row["effect_state"] != "prepared" or row["attempt_ref"] is not None:
                raise LedgerError("only an unissued prepared operation can be cancelled")
            timestamp = now_iso()
            connection.execute(
                "UPDATE operations SET cancel_reason=?,cancelled_at=?,updated_at=? WHERE operation_id=?",
                (reason_code, timestamp, timestamp, operation_id),
            )
            event(connection, operation_id, "cancelled", {"reasonCode": reason_code})
            outcome = "cancelled"
        elif row["cancel_reason"] == reason_code:
            outcome = "replay"
        else:
            raise LedgerError("operation is already cancelled for a different reason")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return base_receipt(load(connection, operation_id), outcome)


def preparation_gate(
    connection: sqlite3.Connection,
    stability_connection: sqlite3.Connection,
    request: dict[str, Any],
    route_receipt: Any,
    lane_inventory: Any,
    control_inventory: Any,
    control_stability: Any,
    freshness_seconds: int,
) -> dict[str, Any]:
    if not isinstance(route_receipt, dict) or route_receipt.get("schema") != "lazy-routing-receipt/v1":
        raise LedgerError("route receipt is invalid")
    if route_receipt.get("run_id") != request["routeRunId"]:
        raise LedgerError("route receipt does not match operation request")
    if route_receipt.get("route") != "chatgpt-carrier":
        raise LedgerError("route receipt does not admit a carrier candidate")
    lanes = validate_inventory(lane_inventory)
    control = plan_control_services(control_inventory, freshness_seconds)
    stability = validate_stability_receipt(control_stability)
    try:
        verify_stored_status(stability_connection, stability)
    except StabilityError as error:
        raise LedgerError(str(error)) from error
    expected_identity = (request["laneRef"], request["laneGeneration"])
    matches = [
        lane for lane in lanes["lanes"]
        if (lane["laneRef"], lane["laneGeneration"]) == expected_identity
    ]
    lane = matches[0] if len(matches) == 1 else None
    checked_at = datetime.now(timezone.utc)
    lane_observed = parsed_timestamp(lanes["observedAt"], "lane observedAt")
    control_observed = parsed_timestamp(control["observedAt"], "control observedAt")
    future_limit = checked_at.timestamp() + 5
    if lane_observed.timestamp() > future_limit or control_observed.timestamp() > future_limit:
        raise LedgerError("lane or control inventory is too far in the future")
    lane_inventory_fresh = (
        0 <= (checked_at - lane_observed).total_seconds() <= freshness_seconds
    )
    lane_ready = bool(
        lane
        and lane_inventory_fresh
        and lane["state"] == "responsive"
        and lane["activity"] == "idle"
        and lane["protected"] is True
        and not lane["blockers"]
    )
    control_lease_ready = control["outcome"] == "keep-primary"
    active_service = control.get("activeService")
    stability_identity_ready = bool(
        active_service
        and stability["controlRef"] == active_service["serviceRef"]
        and stability["controlGeneration"] == active_service["serviceGeneration"]
    )
    stability_time_ready = (
        parsed_timestamp(stability["evaluatedAt"], "evaluatedAt")
        == parsed_timestamp(control["observedAt"], "control observedAt")
    )
    control_stability_ready = bool(
        stability_identity_ready
        and stability_time_ready
        and stability["status"] == "stable"
    )
    expected_scope = {
        "browserFamily": control["browserFamily"],
        "controlInventorySha256": control["inventorySha256"],
        "laneGeneration": request["laneGeneration"],
        "laneRef": request["laneRef"],
        "probeVersion": stability["scope"]["probeVersion"],
        "projectRef": request["projectRef"],
        "surfaceProfile": request["surfaceProfile"],
    }
    scope_ready = stability["scope"] == expected_scope
    control_stability_ready = control_stability_ready and scope_ready
    control_ready = control_lease_ready and control_stability_ready
    binding_sha = sha256_text(canonical(request))
    row = connection.execute(
        "SELECT * FROM operations WHERE operation_id=?", (request["operationId"],)
    ).fetchone()
    if row is not None and row["binding_sha256"] != binding_sha:
        raise LedgerError("gate request changed the durable operation binding")

    operation_state = "unprepared" if row is None else (
        "cancelled" if row["cancel_reason"] is not None else row["effect_state"]
    )
    cancel_reason = None
    if operation_state in {"issued", "ambiguous"}:
        outcome = "reconcile-only"
        next_step = "reconcile-effect-only"
    elif operation_state == "committed" and row["response_state"] != "complete":
        outcome = "observe-only"
        next_step = "observe-response-only"
    elif operation_state in {"committed", "verified-no-effect", "cancelled"}:
        outcome = "terminal"
        next_step = "no-action"
    elif not control_lease_ready:
        cancel_reason = "control-not-ready-before-issue" if operation_state == "prepared" else None
        outcome = "cancel-prepared-operation" if cancel_reason else "rotate-control-before-prepare"
        next_step = "cancel-prepared" if cancel_reason else "settle-control-rotation"
    elif not control_stability_ready:
        cancel_reason = "control-stability-lost-before-issue" if operation_state == "prepared" else None
        if cancel_reason:
            outcome = "cancel-prepared-operation"
            next_step = "cancel-prepared"
        elif stability["status"] == "rotate":
            outcome = "rotate-control-before-prepare"
            next_step = "settle-control-rotation"
        else:
            outcome = "wait-for-control-stability-before-prepare"
            next_step = "record-control-capability-event"
    elif not lane_ready:
        cancel_reason = "lane-not-ready-before-issue" if operation_state == "prepared" else None
        outcome = "cancel-prepared-operation" if cancel_reason else "replace-lane-before-prepare"
        next_step = "cancel-prepared" if cancel_reason else "settle-lane-lifecycle"
    elif operation_state == "prepared":
        outcome = "ready-to-confirm-and-issue"
        next_step = "fresh-action-time-confirmation"
    else:
        outcome = "ready-to-prepare"
        next_step = "prepare-exact-operation"

    gate_receipt = {
        "schema": "lazy-carrier-preparation-gate/v1",
        "operationId": request["operationId"],
        "bindingSha256": binding_sha,
        "operationState": operation_state,
        "outcome": outcome,
        "next": next_step,
        "cancelReason": cancel_reason,
        "controlOutcome": control["outcome"],
        "controlReady": control_ready,
        "controlLeaseReady": control_lease_ready,
        "controlStabilityReady": control_stability_ready,
        "controlStabilityStatus": stability["status"],
        "controlStabilityReceiptSha256": stability["receiptSha256"],
        "controlStabilityScopeSha256": stability["scopeSha256"],
        "controlService": active_service,
        "controlInventorySha256": control["inventorySha256"],
        "laneReady": lane_ready,
        "laneInventoryFresh": lane_inventory_fresh,
        "laneInventorySha256": sha256_text(canonical(lanes)),
        "laneRef": request["laneRef"],
        "laneGeneration": request["laneGeneration"],
        "retryAuthorized": False,
        "rawContentEmitted": False,
        "authorizesWork": False,
        "authorizesEffects": False,
        "authorizesDispatch": False,
    }
    gate_receipt["gateReceiptSha256"] = sha256_text(canonical(gate_receipt))
    if operation_state == "prepared" and outcome == "ready-to-confirm-and-issue":
        expires_at = stability.get("expiresAt")
        if expires_at is None:
            raise LedgerError("stable gate acceptance lacks expiry")
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = load(connection, request["operationId"])
            if (
                current["binding_sha256"] != binding_sha
                or current["effect_state"] != "prepared"
                or current["cancel_reason"] is not None
            ):
                raise LedgerError("operation state changed while persisting gate acceptance")
            encoded = canonical(gate_receipt)
            previous = current["gate_receipt_sha256"]
            connection.execute(
                """UPDATE operations SET gate_receipt_sha256=?,gate_json=?,
                     gate_expires_at=?,gate_event_set_sha256=?,gated_at=?,updated_at=?
                   WHERE operation_id=?""",
                (
                    gate_receipt["gateReceiptSha256"], encoded, expires_at,
                    stability["eventSetSha256"], now_iso(), now_iso(),
                    request["operationId"],
                ),
            )
            if previous != gate_receipt["gateReceiptSha256"]:
                event(
                    connection, request["operationId"], "gate-accepted",
                    {
                        "gateReceiptSha256": gate_receipt["gateReceiptSha256"],
                        "controlStabilityReceiptSha256": stability["receiptSha256"],
                        "expiresAt": expires_at,
                    },
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return gate_receipt


def settle(
    connection: sqlite3.Connection,
    operation_id: str,
    attempt_ref: str,
    outcome: str,
    conversation_ref: str | None,
) -> dict[str, Any]:
    if outcome not in SETTLEMENTS:
        raise LedgerError("settlement outcome is not supported")
    if outcome == "committed" and not conversation_ref:
        raise LedgerError("committed settlement requires conversationRef")
    if conversation_ref is not None:
        bounded_string(conversation_ref, "conversationRef")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = load(connection, operation_id)
        if row["attempt_ref"] != attempt_ref:
            raise LedgerError("attemptRef does not match issued operation")
        if row["effect_state"] == "issued":
            connection.execute(
                "UPDATE operations SET effect_state=?,conversation_ref=?,updated_at=? WHERE operation_id=?",
                (outcome, conversation_ref, now_iso(), operation_id),
            )
            event(connection, operation_id, "settled", {"outcome": outcome, "conversationRef": conversation_ref})
            receipt_outcome = "settled"
        elif row["effect_state"] == outcome and row["conversation_ref"] == conversation_ref:
            receipt_outcome = "replay"
        else:
            raise LedgerError("operation is already settled differently")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return base_receipt(load(connection, operation_id), receipt_outcome)


def record_response(
    connection: sqlite3.Connection,
    operation_id: str,
    state: str,
    artifact_ref: str | None,
    artifact_sha: str | None,
    artifact_bytes: int | None,
) -> dict[str, Any]:
    if state not in {"pending", "unknown", "complete"}:
        raise LedgerError("response state is not supported")
    if state == "complete":
        bounded_string(artifact_ref, "artifactRef")
        if not isinstance(artifact_sha, str) or not SHA256_RE.fullmatch(artifact_sha):
            raise LedgerError("complete response requires artifactSha256")
        nonnegative_int(artifact_bytes, "artifactBytes")
    elif any(value is not None for value in (artifact_ref, artifact_sha, artifact_bytes)):
        raise LedgerError("only a complete response accepts artifact metadata")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = load(connection, operation_id)
        if row["effect_state"] != "committed":
            raise LedgerError("response observation requires committed send settlement")
        existing = row["response_state"]
        exact_replay = (
            existing == state
            and row["artifact_ref"] == artifact_ref
            and row["artifact_sha256"] == artifact_sha
            and row["artifact_bytes"] == artifact_bytes
        )
        if exact_replay:
            outcome = "replay"
        elif existing == "complete":
            raise LedgerError("complete response cannot change")
        elif state in {"pending", "unknown"} and existing in {"pending", "unknown"}:
            connection.execute(
                "UPDATE operations SET response_state=?,updated_at=? WHERE operation_id=?",
                (state, now_iso(), operation_id),
            )
            event(connection, operation_id, "response-observed", {"state": state})
            outcome = "updated"
        else:
            connection.execute(
                "UPDATE operations SET response_state=?,artifact_ref=?,artifact_sha256=?,artifact_bytes=?,updated_at=? WHERE operation_id=?",
                (state, artifact_ref, artifact_sha, artifact_bytes, now_iso(), operation_id),
            )
            event(
                connection,
                operation_id,
                "response-observed",
                {"state": state, "artifactRef": artifact_ref, "artifactSha256": artifact_sha, "artifactBytes": artifact_bytes},
            )
            outcome = "updated"
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return base_receipt(load(connection, operation_id), outcome)


def resume(connection: sqlite3.Connection, operation_id: str) -> dict[str, Any]:
    row = load(connection, operation_id)
    receipt = base_receipt(row, "resume")
    if row["cancel_reason"] is not None:
        next_step = "terminal"
    elif row["effect_state"] == "prepared":
        next_step = "await-new-action-time-confirmation-or-issue"
    elif row["effect_state"] in {"issued", "ambiguous"}:
        next_step = "reconcile-effect-only"
    elif row["effect_state"] == "committed" and row["response_state"] != "complete":
        next_step = "observe-response-only"
    else:
        next_step = "terminal"
    receipt.update({"next": next_step, "sendAuthorized": False, "observationOnly": True})
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--request", type=Path, required=True)
    issue_parser = sub.add_parser("issue")
    issue_parser.add_argument("--operation-id", required=True)
    issue_parser.add_argument("--binding-sha256", required=True)
    issue_parser.add_argument("--gate-receipt-sha256", required=True)
    issue_parser.add_argument("--control-stability-state-root", type=Path, required=True)
    cancel_parser = sub.add_parser("cancel")
    cancel_parser.add_argument("--operation-id", required=True)
    cancel_parser.add_argument("--reason-code", required=True)
    gate_parser = sub.add_parser("gate")
    gate_parser.add_argument("--request", type=Path, required=True)
    gate_parser.add_argument("--route-receipt", type=Path, required=True)
    gate_parser.add_argument("--lane-inventory", type=Path, required=True)
    gate_parser.add_argument("--control-inventory", type=Path, required=True)
    gate_parser.add_argument("--control-stability", type=Path, required=True)
    gate_parser.add_argument("--control-stability-state-root", type=Path, required=True)
    gate_parser.add_argument("--freshness-seconds", type=int, default=300)
    settle_parser = sub.add_parser("settle")
    settle_parser.add_argument("--operation-id", required=True)
    settle_parser.add_argument("--attempt-ref", required=True)
    settle_parser.add_argument("--outcome", choices=sorted(SETTLEMENTS), required=True)
    settle_parser.add_argument("--conversation-ref")
    response_parser = sub.add_parser("record-response")
    response_parser.add_argument("--operation-id", required=True)
    response_parser.add_argument("--state", choices=("pending", "unknown", "complete"), required=True)
    response_parser.add_argument("--artifact-ref")
    response_parser.add_argument("--artifact-sha256")
    response_parser.add_argument("--artifact-bytes", type=int)
    for name in ("resume", "show"):
        command_parser = sub.add_parser(name)
        command_parser.add_argument("--operation-id", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    connection = connect(args.state_root)
    try:
        if args.command == "prepare":
            receipt = prepare(connection, validate_request(json.loads(args.request.read_text(encoding="utf-8"))))
        elif args.command == "issue":
            stability_connection = connect_stability(args.control_stability_state_root)
            try:
                receipt = issue(
                    connection, stability_connection, args.operation_id,
                    args.binding_sha256, args.gate_receipt_sha256,
                )
            finally:
                stability_connection.close()
        elif args.command == "cancel":
            receipt = cancel(connection, args.operation_id, args.reason_code)
        elif args.command == "gate":
            stability_connection = connect_stability(args.control_stability_state_root)
            try:
                receipt = preparation_gate(
                    connection,
                    stability_connection,
                    validate_request(json.loads(args.request.read_text(encoding="utf-8"))),
                    json.loads(args.route_receipt.read_text(encoding="utf-8")),
                    json.loads(args.lane_inventory.read_text(encoding="utf-8")),
                    json.loads(args.control_inventory.read_text(encoding="utf-8")),
                    json.loads(args.control_stability.read_text(encoding="utf-8")),
                    args.freshness_seconds,
                )
            finally:
                stability_connection.close()
        elif args.command == "settle":
            receipt = settle(connection, args.operation_id, args.attempt_ref, args.outcome, args.conversation_ref)
        elif args.command == "record-response":
            receipt = record_response(
                connection, args.operation_id, args.state, args.artifact_ref,
                args.artifact_sha256, args.artifact_bytes,
            )
        elif args.command == "resume":
            receipt = resume(connection, args.operation_id)
        else:
            receipt = base_receipt(load(connection, args.operation_id), "show")
    except (OSError, json.JSONDecodeError, LedgerError, StabilityError, sqlite3.Error) as error:
        raise SystemExit(str(error)) from error
    finally:
        connection.close()
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
