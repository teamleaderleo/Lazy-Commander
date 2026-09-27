"""Pure, content-free controller for the cost-aware Lazy campaign.

This module deliberately does not call a worker, browser, carrier, or model.
It validates one owner-provided semantic snapshot and returns one transition
advice object.  All references and fingerprints are opaque; application
content is not part of this contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


INPUT_SCHEMA = "lazy-campaign-controller-snapshot/v2"
LEGACY_INPUT_SCHEMA = "lazy-campaign-controller-snapshot/v1"
VIEW_SCHEMA = "lazy-campaign-controller-view/v1"
TRANSITION_KINDS = frozenset({"wake", "observe", "effect", "reconcile", "complete", "blocked"})
ROUTES = frozenset({"codex-local", "workstation-worker", "chatgpt-carrier"})
EFFECT_STATES = frozenset({"none", "issued", "ambiguous", "committed", "verified-no-effect"})
OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@-]{0,239}$")
FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
MAX_CHARS = 100_000_000
MAX_INPUT_BYTES = 1_000_000
RECEIPT_SCHEMA = "lazy-campaign-controller-receipt/v1"


class ControllerError(ValueError):
    """Raised when a snapshot is not an admissible controller input."""


def digest(value: Any) -> str:
    """Return the canonical SHA-256 fingerprint used by exact replay."""

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _keys(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ControllerError(f"{label} must be an object")
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unknown " + ", ".join(extra))
        raise ControllerError(f"{label} keys do not match contract ({'; '.join(details)})")


def _opaque(value: Any, label: str, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not OPAQUE.fullmatch(value):
        raise ControllerError(f"{label} must be a bounded opaque identifier")
    return value


def _fingerprint(value: Any, label: str, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not FINGERPRINT.fullmatch(value):
        raise ControllerError(f"{label} must be a SHA-256 fingerprint")
    return value


def _positive_int(value: Any, label: str, *, zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if zero else 1):
        raise ControllerError(f"{label} must be a {'non-negative' if zero else 'positive'} integer")
    return value


def _chars(value: Any, label: str) -> int:
    result = _positive_int(value, label, zero=True)
    if result > MAX_CHARS:
        raise ControllerError(f"{label} exceeds the character bound")
    return result


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ControllerError(f"{label} must be a boolean")
    return value


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ControllerError(f"{label} must be an ISO-8601 timestamp")
    rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as exc:
        raise ControllerError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ControllerError(f"{label} must include a timezone")
    return value


def _generation_record(raw: Any, label: str) -> dict[str, Any]:
    _keys(raw, {"ref", "generation", "status"}, label)
    status = raw["status"]
    if not isinstance(status, str) or status not in {"active", "complete", "blocked", "paused"}:
        raise ControllerError(f"{label}.status is unsupported")
    return {
        "ref": _opaque(raw["ref"], f"{label}.ref"),
        "generation": _positive_int(raw["generation"], f"{label}.generation"),
        "status": status,
    }


def _authority_summary(raw: Any, label: str, expected: set[str]) -> dict[str, Any]:
    _keys(
        raw,
        expected | {"ref", "generation", "fingerprint", "authorizesWork", "authorizesEffects", "authorizesDispatch"},
        label,
    )
    route = raw.get("route")
    if route is not None and route not in ROUTES:
        raise ControllerError(f"{label}.route is unsupported")
    for key in ("authorizesWork", "authorizesEffects", "authorizesDispatch"):
        if _bool(raw[key], f"{label}.{key}"):
            raise ControllerError(f"{label}.{key} must be false")
    result = dict(raw)
    result["ref"] = _opaque(raw["ref"], f"{label}.ref")
    result["generation"] = _positive_int(raw["generation"], f"{label}.generation")
    result["fingerprint"] = _fingerprint(raw["fingerprint"], f"{label}.fingerprint")
    return result


def validate_snapshot(raw: Any) -> dict[str, Any]:
    """Validate and normalize the strict content-free snapshot contract."""

    top = {
        "schema", "observedAt", "campaign", "step", "previousSnapshotSha256",
        "expectedInputChars", "expectedOutputChars", "codexBudget", "routeDecision",
        "lanePlan", "controlPlan", "event", "deadlineAt", "effectSettlement", "materialDelta",
        "actionTimeConfirmation",
    }
    schema = raw.get("schema") if isinstance(raw, dict) else None
    if schema == INPUT_SCHEMA:
        top.add("ownerCursors")
    elif schema != LEGACY_INPUT_SCHEMA:
        raise ControllerError("unsupported controller snapshot schema")
    _keys(raw, top, "controller snapshot")

    owner_cursors: list[dict[str, Any]] = []
    if schema == INPUT_SCHEMA:
        if not isinstance(raw["ownerCursors"], list) or len(raw["ownerCursors"]) > 32:
            raise ControllerError("ownerCursors must contain at most 32 cursor records")
        seen_owners: set[str] = set()
        for index, cursor in enumerate(raw["ownerCursors"]):
            label = f"ownerCursors[{index}]"
            _keys(cursor, {"ownerRef", "generation", "fingerprint"}, label)
            owner_ref = _opaque(cursor["ownerRef"], f"{label}.ownerRef")
            if owner_ref in seen_owners:
                raise ControllerError("ownerCursors ownerRef values must be unique")
            seen_owners.add(owner_ref)
            owner_cursors.append({
                "ownerRef": owner_ref,
                "generation": _positive_int(cursor["generation"], f"{label}.generation"),
                "fingerprint": _fingerprint(cursor["fingerprint"], f"{label}.fingerprint"),
            })
        owner_cursors.sort(key=lambda value: value["ownerRef"])

    previous = _fingerprint(raw["previousSnapshotSha256"], "previousSnapshotSha256", nullable=True)
    budget = raw["codexBudget"]
    _keys(budget, {"limit", "reserved", "consumed"}, "codexBudget")
    normalized_budget = {
        "limit": _positive_int(budget["limit"], "codexBudget.limit", zero=True),
        "reserved": _positive_int(budget["reserved"], "codexBudget.reserved", zero=True),
        "consumed": _positive_int(budget["consumed"], "codexBudget.consumed", zero=True),
    }
    if normalized_budget["reserved"] + normalized_budget["consumed"] > normalized_budget["limit"]:
        raise ControllerError("codexBudget reserved plus consumed exceeds limit")

    route = _authority_summary(raw["routeDecision"], "routeDecision", {"route"})
    if route["route"] not in ROUTES:
        raise ControllerError("routeDecision.route is required and unsupported")
    lane = _authority_summary(raw["lanePlan"], "lanePlan", {"route", "status"})
    control = _authority_summary(raw["controlPlan"], "controlPlan", {"route", "status"})
    for label, summary in (("lanePlan", lane), ("controlPlan", control)):
        if summary["route"] is not None:
            raise ControllerError(f"{label}.route must be null")
        if summary["status"] not in {"ready", "blocked", "stale", "unavailable"}:
            raise ControllerError(f"{label}.status is unsupported")

    event = raw["event"]
    _keys(event, {"ref", "generation", "kind"}, "event")
    if event["kind"] not in {"none", "deadline", "owner-event", "goal-continuation", "operator-steer", "recovery"}:
        raise ControllerError("event.kind is unsupported")
    normalized_event = {
        "ref": _opaque(event["ref"], "event.ref"),
        "generation": _positive_int(event["generation"], "event.generation"),
        "kind": event["kind"],
    }
    deadline_at = _timestamp(raw["deadlineAt"], "deadlineAt") if raw["deadlineAt"] is not None else None
    if (normalized_event["kind"] == "deadline") != (deadline_at is not None):
        raise ControllerError("deadline event and deadlineAt must be supplied together")

    settlement = raw["effectSettlement"]
    _keys(settlement, {"ref", "generation", "state", "semanticFingerprint"}, "effectSettlement")
    if settlement["state"] not in EFFECT_STATES:
        raise ControllerError("effectSettlement.state is unsupported")
    normalized_settlement = {
        "ref": _opaque(settlement["ref"], "effectSettlement.ref", nullable=True),
        "generation": (
            None if settlement["generation"] is None
            else _positive_int(settlement["generation"], "effectSettlement.generation")
        ),
        "state": settlement["state"],
        "semanticFingerprint": _fingerprint(
            settlement["semanticFingerprint"], "effectSettlement.semanticFingerprint", nullable=True
        ),
    }
    if settlement["state"] == "none" and any(
        normalized_settlement[key] is not None for key in ("ref", "generation", "semanticFingerprint")
    ):
        raise ControllerError("empty effect settlement cannot carry an effect identity")
    if settlement["state"] != "none" and (
        normalized_settlement["ref"] is None or normalized_settlement["generation"] is None
    ):
        raise ControllerError("settled effect requires ref and generation")

    delta = raw["materialDelta"]
    _keys(delta, {"present", "generation", "fingerprint"}, "materialDelta")
    normalized_delta = {
        "present": _bool(delta["present"], "materialDelta.present"),
        "generation": None if delta["generation"] is None else _positive_int(delta["generation"], "materialDelta.generation"),
        "fingerprint": _fingerprint(delta["fingerprint"], "materialDelta.fingerprint", nullable=True),
    }
    if normalized_delta["present"] != (normalized_delta["generation"] is not None and normalized_delta["fingerprint"] is not None):
        raise ControllerError("materialDelta presence must match its identity")

    confirmation = raw["actionTimeConfirmation"]
    _keys(
        confirmation,
        {"confirmed", "ref", "generation", "requestFingerprint", "afterEffectRef", "afterEffectGeneration"},
        "actionTimeConfirmation",
    )
    normalized_confirmation = {
        "confirmed": _bool(confirmation["confirmed"], "actionTimeConfirmation.confirmed"),
        "ref": _opaque(confirmation["ref"], "actionTimeConfirmation.ref", nullable=True),
        "generation": None if confirmation["generation"] is None else _positive_int(confirmation["generation"], "actionTimeConfirmation.generation"),
        "requestFingerprint": _fingerprint(
            confirmation["requestFingerprint"], "actionTimeConfirmation.requestFingerprint", nullable=True
        ),
        "afterEffectRef": _opaque(
            confirmation["afterEffectRef"], "actionTimeConfirmation.afterEffectRef", nullable=True
        ),
        "afterEffectGeneration": (
            None
            if confirmation["afterEffectGeneration"] is None
            else _positive_int(confirmation["afterEffectGeneration"], "actionTimeConfirmation.afterEffectGeneration")
        ),
    }
    core_confirmation_present = all(
        normalized_confirmation[key] is not None
        for key in ("ref", "generation", "requestFingerprint")
    )
    if normalized_confirmation["confirmed"] != core_confirmation_present:
        raise ControllerError("confirmed action-time confirmation requires ref, generation, and request fingerprint")
    if (normalized_confirmation["afterEffectRef"] is None) != (
        normalized_confirmation["afterEffectGeneration"] is None
    ):
        raise ControllerError("confirmation prior-effect binding must be all or nothing")
    if not normalized_confirmation["confirmed"] and any(
        normalized_confirmation[key] is not None
        for key in ("afterEffectRef", "afterEffectGeneration")
    ):
        raise ControllerError("unconfirmed action cannot bind a prior effect")
    if normalized_confirmation["confirmed"] and normalized_confirmation["requestFingerprint"] != route["fingerprint"]:
        raise ControllerError("action-time confirmation does not bind the route request")

    return {
        "schema": schema,
        "observedAt": _timestamp(raw["observedAt"], "observedAt"),
        "campaign": _generation_record(raw["campaign"], "campaign"),
        "step": _generation_record(raw["step"], "step"),
        "previousSnapshotSha256": previous,
        "expectedInputChars": _chars(raw["expectedInputChars"], "expectedInputChars"),
        "expectedOutputChars": _chars(raw["expectedOutputChars"], "expectedOutputChars"),
        "codexBudget": normalized_budget,
        "routeDecision": route,
        "lanePlan": lane,
        "controlPlan": control,
        "event": normalized_event,
        "deadlineAt": deadline_at,
        "effectSettlement": normalized_settlement,
        "materialDelta": normalized_delta,
        "actionTimeConfirmation": normalized_confirmation,
        "ownerCursors": owner_cursors,
    }


def snapshot_payload(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return semantic state; wall-clock observation time is not material."""

    return {
        key: snapshot[key]
        for key in snapshot
        if key not in {"previousSnapshotSha256", "observedAt"}
    }


def temporal_class(snapshot: Mapping[str, Any]) -> str:
    """Collapse wall time to the only material boundary: deadline eligibility."""

    if snapshot["event"]["kind"] != "deadline":
        return "timeless"
    now = datetime.fromisoformat(snapshot["observedAt"].replace("Z", "+00:00"))
    deadline = datetime.fromisoformat(snapshot["deadlineAt"].replace("Z", "+00:00"))
    return "deadline-due" if now >= deadline else "deadline-future"


def estimate_cost(expected_input_chars: int, expected_output_chars: int) -> int:
    """Estimate portable model tokens using the campaign's 4 chars/token rule."""

    return math.ceil((_chars(expected_input_chars, "expectedInputChars") + _chars(expected_output_chars, "expectedOutputChars")) / 4)


def estimate_cost_breakdown(expected_input_chars: int, expected_output_chars: int) -> dict[str, int]:
    """Return the content-free cost estimate with input/output components."""

    input_chars = _chars(expected_input_chars, "expectedInputChars")
    output_chars = _chars(expected_output_chars, "expectedOutputChars")
    return {
        "inputChars": input_chars,
        "outputChars": output_chars,
        "estimatedTokens": math.ceil((input_chars + output_chars) / 4),
    }


def _transition(kind: str, ref: str, reason: str, *, wake_at: str | None = None, route: str | None = None) -> dict[str, Any]:
    if kind not in TRANSITION_KINDS:
        raise AssertionError("transition kind outside contract")
    result: dict[str, Any] = {"kind": kind, "ref": ref, "reason": reason, "wakeAt": wake_at}
    if route is not None:
        result["route"] = route
    return result


def choose_transition(snapshot: Mapping[str, Any], snapshot_sha256: str, admitted: bool) -> dict[str, Any]:
    """Choose one transition in safety order; no branch performs an effect."""

    campaign = snapshot["campaign"]
    step = snapshot["step"]
    if campaign["status"] == "complete" or step["status"] == "complete":
        return _transition("complete", step["ref"], "campaign-step-complete")
    if campaign["status"] != "active" or step["status"] != "active":
        return _transition("blocked", step["ref"], "campaign-or-step-not-active")

    effect = snapshot["effectSettlement"]
    if effect["state"] in {"issued", "ambiguous"}:
        return _transition("reconcile", effect["ref"], "effect-must-reconcile-before-retry")
    if effect["state"] == "verified-no-effect":
        if snapshot["routeDecision"]["route"] != "chatgpt-carrier":
            return _transition("blocked", effect["ref"], "new-step-generation-required")
        confirmation = snapshot["actionTimeConfirmation"]
        if (
            not confirmation["confirmed"]
            or confirmation["generation"] <= effect["generation"]
            or confirmation["afterEffectRef"] != effect["ref"]
            or confirmation["afterEffectGeneration"] != effect["generation"]
        ):
            return _transition("blocked", effect["ref"], "new-action-time-confirmation-required")
    if effect["state"] == "committed":
        if (
            snapshot["materialDelta"]["present"]
            and snapshot["materialDelta"]["fingerprint"] != effect["semanticFingerprint"]
        ):
            return _transition("observe", "delta:" + snapshot["materialDelta"]["fingerprint"][:16], "committed-effect-material-delta")
        return _transition("wake", effect["ref"], "wait-material-semantic-delta")

    if not admitted:
        return _transition("blocked", "budget:codex", "codex-token-budget-exceeded")

    if snapshot["event"]["kind"] == "deadline":
        now = datetime.fromisoformat(snapshot["observedAt"].replace("Z", "+00:00"))
        deadline = datetime.fromisoformat(snapshot["deadlineAt"].replace("Z", "+00:00"))
        if now < deadline:
            return _transition("wake", snapshot["event"]["ref"], "exact-deadline-not-due", wake_at=snapshot["deadlineAt"])
        return _transition("observe", snapshot["event"]["ref"], "deadline-due-owner-observation")
    if snapshot["event"]["kind"] != "none":
        return _transition("observe", snapshot["event"]["ref"], "owner-event-bounded-observation")
    if snapshot["materialDelta"]["present"]:
        return _transition("observe", "delta:" + snapshot["materialDelta"]["fingerprint"][:16], "material-semantic-delta")

    route = snapshot["routeDecision"]["route"]
    if route == "chatgpt-carrier":
        for label in ("lanePlan", "controlPlan"):
            if snapshot[label]["status"] != "ready":
                return _transition("blocked", snapshot[label]["ref"], f"{label}-not-ready")
        if not snapshot["actionTimeConfirmation"]["confirmed"]:
            return _transition("blocked", snapshot["routeDecision"]["ref"], "action-time-confirmation-required")
        return _transition("effect", snapshot["routeDecision"]["ref"], "confirmed-carrier-effect-advice", route=route)
    if route == "workstation-worker":
        return _transition("effect", snapshot["routeDecision"]["ref"], "bounded-worker-effect-advice", route=route)
    return _transition("observe", snapshot["routeDecision"]["ref"], "codex-local-observation", route=route)


def compile_controller(raw: Any) -> dict[str, Any]:
    """Compile a snapshot to a deterministic, deeply content-free view."""

    snapshot = validate_snapshot(raw)
    snapshot_sha = digest(snapshot_payload(snapshot))
    estimate = estimate_cost_breakdown(snapshot["expectedInputChars"], snapshot["expectedOutputChars"])
    route = snapshot["routeDecision"]["route"]
    reservation = estimate["estimatedTokens"] if route == "codex-local" else 0
    budget = snapshot["codexBudget"]
    admitted = budget["consumed"] + budget["reserved"] + reservation <= budget["limit"]
    transition = choose_transition(snapshot, snapshot_sha, admitted)
    unchanged = snapshot["previousSnapshotSha256"] == snapshot_sha
    if (
        unchanged
        and transition["kind"] in {"observe", "effect"}
        and transition["reason"] != "deadline-due-owner-observation"
    ):
        transition = _transition("wake", "snapshot:" + snapshot_sha[:16], "unchanged-snapshot-await-material-delta")
    decision_fingerprint = digest(
        {
            "snapshotSha256": snapshot_sha,
            "temporalClass": temporal_class(snapshot),
            "replayClass": "unchanged" if unchanged else "new",
            "transition": transition,
        }
    )
    transition["decisionFingerprint"] = decision_fingerprint
    acceptance_proposal = {
        "transitionId": decision_fingerprint,
        "ownerRef": snapshot["campaign"]["ref"],
        "evaluatedOwnerGeneration": snapshot["campaign"]["generation"],
        "evaluatedOwnerFingerprint": snapshot_sha,
        "evaluatedCursorVectorFingerprint": digest(snapshot["ownerCursors"]),
        "requestedCodexReservationTokens": reservation,
        "authorizesWork": False,
        "authorizesEffects": False,
        "authorizesDispatch": False,
    }
    return {
        "schema": VIEW_SCHEMA,
        "temporalClass": temporal_class(snapshot),
        "campaignRef": snapshot["campaign"]["ref"],
        "campaignGeneration": snapshot["campaign"]["generation"],
        "stepRef": snapshot["step"]["ref"],
        "stepGeneration": snapshot["step"]["generation"],
        "snapshotSha256": snapshot_sha,
        "decisionFingerprint": decision_fingerprint,
        "unchanged": unchanged,
        "cost": {
            **estimate,
            "route": route,
            "codexReservationTokens": reservation,
            "admitted": admitted,
        },
        "budget": {
            "limit": budget["limit"],
            "consumed": budget["consumed"],
            "reserved": budget["reserved"],
            "requestedReservation": reservation,
            "remainingAfterReservation": budget["limit"] - budget["consumed"] - budget["reserved"] - reservation,
        },
        "inputs": {
            "routeRef": snapshot["routeDecision"]["ref"],
            "routeGeneration": snapshot["routeDecision"]["generation"],
            "lanePlanRef": snapshot["lanePlan"]["ref"],
            "lanePlanGeneration": snapshot["lanePlan"]["generation"],
            "controlPlanRef": snapshot["controlPlan"]["ref"],
            "controlPlanGeneration": snapshot["controlPlan"]["generation"],
            "ownerCursorCount": len(snapshot["ownerCursors"]),
            "ownerCursorVectorFingerprint": digest(snapshot["ownerCursors"]),
        },
        "transition": transition,
        "acceptanceProposal": acceptance_proposal,
        "authority": {
            "authorizesWork": False,
            "authorizesEffects": False,
            "authorizesDispatch": False,
            "rawContentEmitted": False,
        },
        "rawContentEmitted": False,
        "authorizesWork": False,
        "authorizesEffects": False,
        "authorizesDispatch": False,
    }


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ControllerError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def load_snapshot(path: Path) -> dict[str, Any]:
    """Load one bounded JSON snapshot while rejecting duplicate keys."""

    size = path.stat().st_size
    if size > MAX_INPUT_BYTES:
        raise ControllerError(f"controller snapshot exceeds {MAX_INPUT_BYTES} bytes")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except UnicodeDecodeError as exc:
        raise ControllerError("controller snapshot must be UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ControllerError("controller snapshot must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ControllerError("controller snapshot must be an object")
    return value


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _markdown_view(view: Mapping[str, Any]) -> bytes:
    transition = view["transition"]
    lines = [
        "# Lazy campaign transition",
        "",
        f"- campaign: `{view['campaignRef']}` generation {view['campaignGeneration']}",
        f"- step: `{view['stepRef']}` generation {view['stepGeneration']}",
        f"- route: `{view['cost']['route']}`",
        f"- transition: `{transition['kind']}` / `{transition['reason']}`",
        f"- transition ref: `{transition['ref']}`",
        f"- estimated tokens: {view['cost']['estimatedTokens']}",
        f"- Codex reservation: {view['cost']['codexReservationTokens']}",
        f"- admitted: `{str(view['cost']['admitted']).lower()}`",
        f"- snapshot: `{view['snapshotSha256']}`",
        "- authority: none (advice only)",
        "- raw content emitted: `false`",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _receipt(view: Mapping[str, Any], view_json: bytes, view_md: bytes) -> dict[str, Any]:
    transition = view["transition"]
    return {
        "schema": RECEIPT_SCHEMA,
        "temporalClass": view["temporalClass"],
        "campaignRef": view["campaignRef"],
        "campaignGeneration": view["campaignGeneration"],
        "stepRef": view["stepRef"],
        "stepGeneration": view["stepGeneration"],
        "snapshotSha256": view["snapshotSha256"],
        "decisionFingerprint": view["decisionFingerprint"],
        "transition": {
            "kind": transition["kind"],
            "ref": transition["ref"],
            "reason": transition["reason"],
            "decisionFingerprint": transition["decisionFingerprint"],
        },
        "acceptanceProposal": view["acceptanceProposal"],
        "cost": {
            "estimatedTokens": view["cost"]["estimatedTokens"],
            "codexReservationTokens": view["cost"]["codexReservationTokens"],
            "admitted": view["cost"]["admitted"],
        },
        "artifacts": {
            "viewJsonSha256": hashlib.sha256(view_json).hexdigest(),
            "viewMarkdownSha256": hashlib.sha256(view_md).hexdigest(),
        },
        "rawContentEmitted": False,
        "authorizesWork": False,
        "authorizesEffects": False,
        "authorizesDispatch": False,
    }


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def run(input_path: Path, output_dir: Path) -> dict[str, Any]:
    """Compile one snapshot into a private, replay-checked artifact directory."""

    view = compile_controller(load_snapshot(input_path))
    view_json = _canonical_json(view)
    view_md = _markdown_view(view)
    receipt = _receipt(view, view_json, view_md)
    payloads = {
        "view.json": view_json,
        "view.md": view_md,
        "receipt.json": _canonical_json(receipt),
    }
    if output_dir.exists():
        if not output_dir.is_dir():
            raise ControllerError("controller output exists and is not a directory")
        for name, expected in payloads.items():
            path = output_dir / name
            if not path.is_file() or path.read_bytes() != expected:
                raise ControllerError("controller output replay conflicts with existing artifacts")
        return receipt

    parent = output_dir.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = parent / ("." + output_dir.name + ".partial-" + digest(receipt)[:12])
    if temporary.exists():
        raise ControllerError("controller partial output requires reconciliation")
    temporary.mkdir(mode=0o700)
    try:
        for name, payload in payloads.items():
            _write_private(temporary / name, payload)
        os.rename(temporary, output_dir)
    except BaseException:
        for name in payloads:
            (temporary / name).unlink(missing_ok=True)
        temporary.rmdir()
        raise
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    receipt = run(args.input, args.output_dir)
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


# Short aliases make the pure surface convenient for callers and tests.
compile_snapshot = compile_controller
validate_input = validate_snapshot


def make_snapshot(**overrides: Any) -> dict[str, Any]:
    """Build a valid minimal snapshot, useful for deterministic callers/tests."""

    value: dict[str, Any] = {
        "schema": INPUT_SCHEMA,
        "observedAt": "2026-08-31T00:00:00Z",
        "campaign": {"ref": "campaign:example", "generation": 1, "status": "active"},
        "step": {"ref": "step:example", "generation": 1, "status": "active"},
        "previousSnapshotSha256": None,
        "expectedInputChars": 0,
        "expectedOutputChars": 0,
        "codexBudget": {"limit": 1000, "reserved": 0, "consumed": 0},
        "routeDecision": {"ref": "route:example", "generation": 1, "route": "workstation-worker", "fingerprint": "0" * 64, "authorizesWork": False, "authorizesEffects": False, "authorizesDispatch": False},
        "lanePlan": {"ref": "lane-plan:example", "generation": 1, "route": None, "status": "ready", "fingerprint": "1" * 64, "authorizesWork": False, "authorizesEffects": False, "authorizesDispatch": False},
        "controlPlan": {"ref": "control-plan:example", "generation": 1, "route": None, "status": "ready", "fingerprint": "2" * 64, "authorizesWork": False, "authorizesEffects": False, "authorizesDispatch": False},
        "event": {"ref": "event:none", "generation": 1, "kind": "none"},
        "deadlineAt": None,
        "effectSettlement": {"ref": None, "generation": None, "state": "none", "semanticFingerprint": None},
        "materialDelta": {"present": False, "generation": None, "fingerprint": None},
        "actionTimeConfirmation": {
            "confirmed": False,
            "ref": None,
            "generation": None,
            "requestFingerprint": None,
            "afterEffectRef": None,
            "afterEffectGeneration": None,
        },
        "ownerCursors": [
            {"ownerRef": "owner:stensibly", "generation": 1, "fingerprint": "3" * 64},
            {"ownerRef": "owner:elatura", "generation": 1, "fingerprint": "4" * 64},
        ],
    }
    value.update(overrides)
    return value


__all__ = [
    "ControllerError", "INPUT_SCHEMA", "LEGACY_INPUT_SCHEMA", "VIEW_SCHEMA", "TRANSITION_KINDS", "digest",
    "validate_snapshot", "validate_input", "snapshot_payload", "estimate_cost",
    "estimate_cost_breakdown", "choose_transition", "compile_controller", "compile_snapshot",
    "make_snapshot",
    "load_snapshot", "run", "main", "RECEIPT_SCHEMA",
]


if __name__ == "__main__":
    raise SystemExit(main())
