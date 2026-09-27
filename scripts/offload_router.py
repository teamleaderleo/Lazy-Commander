#!/usr/bin/env python3
"""Pure routing and retained-lane budget contracts for Lazy Commander."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any


TASK_SCHEMA = "lazy-offload-request/v1"
TASK_DECISION_SCHEMA = "lazy-offload-decision/v1"
INVENTORY_SCHEMA = "lazy-elatura-lane-inventory/v1"
BUDGET_SCHEMA = "lazy-lane-budget-decision/v1"
CARRIER_SETTLEMENT_SCHEMA = "lazy-carrier-effect-settlement/v1"
WORKER_SETTLEMENT_SCHEMA = "lazy-worker-execution-settlement/v1"
CONTROL_INVENTORY_SCHEMA = "lazy-control-service-inventory/v1"
CONTROL_DECISION_SCHEMA = "lazy-control-service-decision/v1"
TASK_KINDS = {"coding", "research", "browser", "coordination", "verification"}
URGENCIES = {"interactive", "background"}
LANE_STATES = {"responsive", "suspended", "reclaimable", "closed"}
LANE_ACTIVITIES = {
    "changed",
    "generating",
    "idle",
    "possible_completion",
    "error",
    "unknown",
}
BLOCKERS = {"unsaved", "composition", "modal", "media", "download", "unknown"}
CONTROL_STATES = {"connected", "degraded", "disconnected", "starting", "retired"}
CONTROL_ROLES = {"primary", "successor", "orphan"}
CONTROL_BLOCKERS = {"active-effect", "operator-handoff", "unknown"}
OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@-]{0,239}$")
TASK_KEYS = {
    "schema",
    "taskRef",
    "kind",
    "urgency",
    "deterministicObservation",
    "ownerQueryAvailable",
    "requiresNovelJudgment",
    "requiresRepositoryMutation",
    "requiresLiveBrowser",
    "consequentialEffect",
    "carrierContextAdvantage",
    "retainedCarrierContinuation",
    "parallelizable",
    "containsSensitiveMaterial",
    "expectedOutputChars",
}
LANE_KEYS = {
    "laneRef",
    "laneGeneration",
    "state",
    "activity",
    "protected",
    "blockers",
    "lastUsedAt",
}
CONTROL_KEYS = {
    "serviceRef",
    "serviceGeneration",
    "role",
    "state",
    "heartbeatAt",
    "inventoryVerifiedAt",
    "surfaceCount",
    "protected",
    "blockers",
}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        raise ValueError(f"{label} keys do not match the contract")


def require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def require_opaque(value: Any, label: str) -> str:
    if not isinstance(value, str) or not OPAQUE.fullmatch(value):
        raise ValueError(f"{label} must be a bounded opaque identifier")
    return value


def require_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError(f"{label} must be a bounded timestamp")
    rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as error:
        raise ValueError(f"{label} must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return value


def timestamp_value(value: str) -> datetime:
    rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(rendered)


def validate_task(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("offload request must be an object")
    require_exact_keys(raw, TASK_KEYS, "offload request")
    if raw["schema"] != TASK_SCHEMA:
        raise ValueError("offload request schema is unsupported")
    kind = raw["kind"]
    urgency = raw["urgency"]
    if kind not in TASK_KINDS or urgency not in URGENCIES:
        raise ValueError("offload kind or urgency is unsupported")
    expected = raw["expectedOutputChars"]
    if isinstance(expected, bool) or not isinstance(expected, int) or not 0 <= expected <= 100_000_000:
        raise ValueError("expectedOutputChars is outside its bound")
    result = {
        "schema": TASK_SCHEMA,
        "taskRef": require_opaque(raw["taskRef"], "taskRef"),
        "kind": kind,
        "urgency": urgency,
        "expectedOutputChars": expected,
    }
    for key in TASK_KEYS - {"schema", "taskRef", "kind", "urgency", "expectedOutputChars"}:
        result[key] = require_bool(raw[key], key)
    return result


def route_task(raw: Any) -> dict[str, Any]:
    request = validate_task(raw)
    reasons: list[str] = []
    route = "codex-local"
    wake = "immediate"
    if request["containsSensitiveMaterial"]:
        reasons.append("sensitive-material-stays-local")
    elif request["consequentialEffect"]:
        reasons.append("consequential-effect-needs-current-agent-authority")
    elif request["requiresRepositoryMutation"]:
        reasons.append("repository-mutation-needs-integrated-codex-loop")
    elif request["requiresLiveBrowser"]:
        reasons.append("live-browser-state-needs-current-bounded-observation")
    elif request["deterministicObservation"]:
        route = "workstation-worker"
        wake = "execution-receipt"
        reasons.append("deterministic-observation-is-mechanical")
    elif request["ownerQueryAvailable"] and not request["requiresNovelJudgment"]:
        route = "workstation-worker"
        wake = "owner-query-receipt"
        reasons.append("owner-query-precedes-model-work")
    elif (
        request["urgency"] == "background"
        and request["carrierContextAdvantage"]
        and (
            request["retainedCarrierContinuation"]
            or request["parallelizable"]
            or request["expectedOutputChars"] >= 20_000
        )
        and request["kind"] in {"coding", "research", "coordination"}
    ):
        route = "chatgpt-carrier"
        wake = "material-elatura-delta"
        if request["retainedCarrierContinuation"]:
            reasons.append("retained-context-continuation-avoids-rehydration")
        else:
            reasons.append("retained-carrier-can-amortize-long-context")
        if request["parallelizable"]:
            reasons.append("independent-background-cycle")
        if request["expectedOutputChars"] >= 20_000:
            reasons.append("large-reasoning-output-kept-out-of-codex-loop")
    elif request["requiresNovelJudgment"]:
        reasons.append("novel-judgment-remains-in-current-codex-loop")
    elif request["urgency"] == "interactive":
        reasons.append("interactive-answer-costs-less-than-handoff")
    else:
        reasons.append("offload-benefit-not-proven")
    return {
        "schema": TASK_DECISION_SCHEMA,
        "requestSha256": digest(request),
        "taskRef": request["taskRef"],
        "route": route,
        "reasonCodes": reasons,
        "wakeOn": wake,
        "routineRead": "semantic-delta" if route == "chatgpt-carrier" else "owner-bounded",
        "browserMessageRequiresActionTimeConfirmation": route == "chatgpt-carrier",
        "grantsWorkAuthority": False,
        "authorizesDispatch": False,
        "rawContentEmitted": False,
    }


def validate_inventory(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"schema", "observedAt", "lanes"}:
        raise ValueError("lane inventory keys do not match the contract")
    if raw["schema"] != INVENTORY_SCHEMA:
        raise ValueError("lane inventory schema is unsupported")
    observed_at = require_timestamp(raw["observedAt"], "observedAt")
    if not isinstance(raw["lanes"], list) or len(raw["lanes"]) > 100:
        raise ValueError("lane inventory must contain at most 100 lanes")
    lanes = []
    identities: set[tuple[str, int]] = set()
    for value in raw["lanes"]:
        if not isinstance(value, dict):
            raise ValueError("lane must be an object")
        require_exact_keys(value, LANE_KEYS, "lane")
        generation = value["laneGeneration"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ValueError("laneGeneration must be positive")
        lane_ref = require_opaque(value["laneRef"], "laneRef")
        identity = (lane_ref, generation)
        if identity in identities:
            raise ValueError("lane identities must be unique")
        identities.add(identity)
        if value["state"] not in LANE_STATES or value["activity"] not in LANE_ACTIVITIES:
            raise ValueError("lane state or activity is unsupported")
        blockers = value["blockers"]
        if not isinstance(blockers, list) or len(set(blockers)) != len(blockers) or any(item not in BLOCKERS for item in blockers):
            raise ValueError("lane blockers are invalid")
        lanes.append(
            {
                "laneRef": lane_ref,
                "laneGeneration": generation,
                "state": value["state"],
                "activity": value["activity"],
                "protected": require_bool(value["protected"], "protected"),
                "blockers": sorted(blockers),
                "lastUsedAt": require_timestamp(value["lastUsedAt"], "lastUsedAt"),
            }
        )
    return {"schema": INVENTORY_SCHEMA, "observedAt": observed_at, "lanes": lanes}


def lane_identity(lane: dict[str, Any]) -> dict[str, Any]:
    return {"laneRef": lane["laneRef"], "laneGeneration": lane["laneGeneration"]}


def plan_lane_budget(raw: Any, desired_new: int = 1) -> dict[str, Any]:
    inventory = validate_inventory(raw)
    if isinstance(desired_new, bool) or not isinstance(desired_new, int) or not 0 <= desired_new <= 20:
        raise ValueError("desired new lanes must be between 0 and 20")
    lanes = inventory["lanes"]
    retained = [lane for lane in lanes if lane["state"] != "closed"]
    responsive = [lane for lane in retained if lane["state"] == "responsive"]
    safe = [
        lane
        for lane in retained
        if not lane["protected"] and not lane["blockers"] and lane["activity"] == "idle"
    ]
    safe.sort(key=lambda lane: (lane["lastUsedAt"], lane["laneRef"], lane["laneGeneration"]))
    close_candidates = sorted(
        [lane for lane in safe if lane["state"] in {"reclaimable", "suspended"}],
        key=lambda lane: (
            0 if lane["state"] == "reclaimable" else 1,
            lane["lastUsedAt"],
            lane["laneRef"],
        ),
    )
    if len(retained) + desired_new > 20:
        needed = len(retained) + desired_new - 20
        selected = close_candidates[:needed]
        outcome = "cleanup-required" if len(selected) == needed else "hard-cap-blocked"
        admit_now = False
        reason = "hard-retained-lane-cap"
        actions = [{"action": "request-close", **lane_identity(lane)} for lane in selected]
    elif len(responsive) + desired_new > 12:
        needed = len(responsive) + desired_new - 12
        candidates = [lane for lane in safe if lane["state"] == "responsive"][:needed]
        outcome = "park-before-admit" if len(candidates) == needed else "soft-cap-decision"
        admit_now = False
        reason = "soft-responsive-lane-cap"
        actions = [{"action": "request-suspended", **lane_identity(lane)} for lane in candidates]
    else:
        outcome = "admit"
        admit_now = True
        reason = "within-soft-and-hard-caps"
        actions = []
    return {
        "schema": BUDGET_SCHEMA,
        "inventorySha256": digest(inventory),
        "observedAt": inventory["observedAt"],
        "softResponsiveCap": 12,
        "hardRetainedCap": 20,
        "responsive": len(responsive),
        "retained": len(retained),
        "desiredNew": desired_new,
        "outcome": outcome,
        "admitNow": admit_now,
        "reasonCode": reason,
        "requestedLifecycleActions": actions,
        "effectsRequireElaturaSettlement": bool(actions),
        "grantsWorkAuthority": False,
        "authorizesDispatch": False,
        "rawContentEmitted": False,
    }


def settle_carrier_effect(
    decision: Any,
    outcome: str,
    conversation_ref: str | None = None,
) -> dict[str, Any]:
    """Project one observed carrier-send outcome without authorizing a retry."""
    if not isinstance(decision, dict) or decision.get("schema") != TASK_DECISION_SCHEMA:
        raise ValueError("carrier settlement requires an offload decision")
    if decision.get("route") != "chatgpt-carrier":
        raise ValueError("only a chatgpt-carrier decision can be effect-settled")
    if outcome not in {"committed", "verified-no-effect", "ambiguous"}:
        raise ValueError("carrier effect outcome is unsupported")
    if conversation_ref is not None:
        conversation_ref = require_opaque(conversation_ref, "conversationRef")
    if outcome == "committed" and conversation_ref is None:
        raise ValueError("committed carrier effect requires a conversationRef")
    if outcome == "committed":
        next_action = "wait-for-material-elatura-delta"
    elif outcome == "verified-no-effect":
        next_action = "new-action-time-confirmation-before-any-retry"
    else:
        next_action = "reconcile-current-conversation-before-any-retry"
    return {
        "schema": CARRIER_SETTLEMENT_SCHEMA,
        "decisionSha256": digest(decision),
        "taskRef": require_opaque(decision.get("taskRef"), "taskRef"),
        "outcome": outcome,
        "conversationRef": conversation_ref,
        "effectConfirmed": outcome == "committed",
        "retryAuthorized": False,
        "requiresReconciliation": outcome == "ambiguous",
        "next": next_action,
        "rawContentEmitted": False,
    }


def settle_worker_execution(
    decision: Any,
    outcome: str,
    execution_ref: str,
    artifact_sha256: str | None = None,
    artifact_bytes: int | None = None,
) -> dict[str, Any]:
    """Bind one content-free local-worker outcome to an exact route decision."""
    if not isinstance(decision, dict) or decision.get("schema") != TASK_DECISION_SCHEMA:
        raise ValueError("worker settlement requires an offload decision")
    if decision.get("route") != "workstation-worker":
        raise ValueError("only a workstation-worker decision can be execution-settled")
    if outcome not in {"committed", "failed", "ambiguous"}:
        raise ValueError("worker execution outcome is unsupported")
    execution_ref = require_opaque(execution_ref, "executionRef")
    if outcome == "committed":
        if not isinstance(artifact_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256):
            raise ValueError("committed worker execution requires a lowercase artifact SHA-256")
        if not isinstance(artifact_bytes, int) or isinstance(artifact_bytes, bool) or artifact_bytes < 0:
            raise ValueError("committed worker execution requires non-negative artifact bytes")
    elif artifact_sha256 is not None or artifact_bytes is not None:
        raise ValueError("non-committed worker execution cannot claim an artifact")
    return {
        "schema": WORKER_SETTLEMENT_SCHEMA,
        "decisionSha256": digest(decision),
        "taskRef": require_opaque(decision.get("taskRef"), "taskRef"),
        "executionRef": execution_ref,
        "outcome": outcome,
        "artifactSha256": artifact_sha256,
        "artifactBytes": artifact_bytes,
        "requiresReconciliation": outcome == "ambiguous",
        "retryAuthorized": False,
        "next": "owner-acceptance-check" if outcome == "committed" else (
            "reconcile-worker-execution-before-any-retry" if outcome == "ambiguous" else "owner-decides-retry-or-fallback"
        ),
        "rawContentEmitted": False,
        "authorizesWork": False,
        "authorizesDispatch": False,
    }


def validate_control_inventory(raw: Any) -> dict[str, Any]:
    expected = {"schema", "observedAt", "browserFamily", "services"}
    if not isinstance(raw, dict):
        raise ValueError("control-service inventory must be an object")
    require_exact_keys(raw, expected, "control-service inventory")
    if raw["schema"] != CONTROL_INVENTORY_SCHEMA:
        raise ValueError("control-service inventory schema is unsupported")
    observed_at = require_timestamp(raw["observedAt"], "observedAt")
    browser_family = require_opaque(raw["browserFamily"], "browserFamily")
    if not isinstance(raw["services"], list) or len(raw["services"]) > 20:
        raise ValueError("control-service inventory must contain at most 20 services")
    services = []
    identities: set[tuple[str, int]] = set()
    for value in raw["services"]:
        if not isinstance(value, dict):
            raise ValueError("control service must be an object")
        require_exact_keys(value, CONTROL_KEYS, "control service")
        generation = value["serviceGeneration"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ValueError("serviceGeneration must be positive")
        service_ref = require_opaque(value["serviceRef"], "serviceRef")
        identity = (service_ref, generation)
        if identity in identities:
            raise ValueError("control-service identities must be unique")
        identities.add(identity)
        if value["role"] not in CONTROL_ROLES or value["state"] not in CONTROL_STATES:
            raise ValueError("control-service role or state is unsupported")
        heartbeat_at = require_timestamp(value["heartbeatAt"], "heartbeatAt")
        verified_at = value["inventoryVerifiedAt"]
        if verified_at is not None:
            verified_at = require_timestamp(verified_at, "inventoryVerifiedAt")
        surface_count = value["surfaceCount"]
        if isinstance(surface_count, bool) or not isinstance(surface_count, int) or not 0 <= surface_count <= 100:
            raise ValueError("surfaceCount is outside its bound")
        blockers = value["blockers"]
        if not isinstance(blockers, list) or len(set(blockers)) != len(blockers) or any(item not in CONTROL_BLOCKERS for item in blockers):
            raise ValueError("control-service blockers are invalid")
        services.append(
            {
                "serviceRef": service_ref,
                "serviceGeneration": generation,
                "role": value["role"],
                "state": value["state"],
                "heartbeatAt": heartbeat_at,
                "inventoryVerifiedAt": verified_at,
                "surfaceCount": surface_count,
                "protected": require_bool(value["protected"], "protected"),
                "blockers": sorted(blockers),
            }
        )
    return {
        "schema": CONTROL_INVENTORY_SCHEMA,
        "observedAt": observed_at,
        "browserFamily": browser_family,
        "services": services,
    }


def control_identity(service: dict[str, Any]) -> dict[str, Any]:
    return {
        "serviceRef": service["serviceRef"],
        "serviceGeneration": service["serviceGeneration"],
    }


def plan_control_services(raw: Any, freshness_seconds: int = 300) -> dict[str, Any]:
    inventory = validate_control_inventory(raw)
    if isinstance(freshness_seconds, bool) or not isinstance(freshness_seconds, int) or not 30 <= freshness_seconds <= 3600:
        raise ValueError("freshness seconds must be between 30 and 3600")
    observed = timestamp_value(inventory["observedAt"])

    def fresh(value: str | None) -> bool:
        if value is None:
            return False
        age = (observed - timestamp_value(value)).total_seconds()
        return 0 <= age <= freshness_seconds

    services = [service for service in inventory["services"] if service["state"] != "retired"]
    healthy = [
        service
        for service in services
        if service["state"] == "connected"
        and fresh(service["heartbeatAt"])
        and fresh(service["inventoryVerifiedAt"])
    ]
    healthy_primaries = [service for service in healthy if service["role"] == "primary"]
    healthy_successors = [service for service in healthy if service["role"] == "successor"]
    actions: list[dict[str, Any]] = []
    if healthy_primaries:
        selected = sorted(
            healthy_primaries,
            key=lambda service: (
                -service["serviceGeneration"],
                service["serviceRef"],
            ),
        )[0]
        extras = [service for service in services if service is not selected]
        warm_successors = sorted(
            [service for service in healthy_successors if service is not selected],
            key=lambda service: (-service["serviceGeneration"], service["serviceRef"]),
        )
        kept_warm_successor = warm_successors[0] if warm_successors else None
        safe_stale_extras = [
            service
            for service in extras
            if service not in healthy
            and service["state"] != "starting"
            and not service["protected"]
            and not service["blockers"]
        ]
        safe_redundant_healthy = [
            service
            for service in extras
            if service in healthy
            and service is not kept_warm_successor
            and not service["protected"]
            and not service["blockers"]
        ]
        retire_candidates = safe_stale_extras + [
            service
            for service in safe_redundant_healthy
            if service not in safe_stale_extras
        ]
        actions = [
            {"action": "request-retire", **control_identity(service)}
            for service in retire_candidates
        ]
        outcome = "cleanup-required" if actions else "keep-primary"
        reason = "verified-primary-available"
        active_ref = control_identity(selected)
    elif healthy_successors:
        selected = sorted(
            healthy_successors,
            key=lambda service: (-service["serviceGeneration"], service["serviceRef"]),
        )[0]
        actions.append({"action": "request-promote-successor", **control_identity(selected)})
        safe_predecessors = [
            service
            for service in services
            if service is not selected
            and service["state"] != "starting"
            and not service["protected"]
            and not service["blockers"]
        ]
        actions.extend(
            {"action": "request-retire-after-promotion", **control_identity(service)}
            for service in safe_predecessors
        )
        outcome = "successor-ready"
        reason = "verified-successor-precedes-retirement"
        active_ref = control_identity(selected)
    elif any(service["state"] == "starting" for service in services):
        outcome = "wait-successor-verification"
        reason = "successor-started-but-not-yet-verified"
        active_ref = None
    else:
        actions.append(
            {
                "action": "request-start-successor",
                "browserFamily": inventory["browserFamily"],
            }
        )
        outcome = "rotation-required"
        reason = "no-fresh-inventory-verified-primary"
        active_ref = None
    return {
        "schema": CONTROL_DECISION_SCHEMA,
        "inventorySha256": digest(inventory),
        "observedAt": inventory["observedAt"],
        "browserFamily": inventory["browserFamily"],
        "freshnessSeconds": freshness_seconds,
        "activeServices": len(services),
        "healthyServices": len(healthy),
        "controlSurfaceCount": sum(service["surfaceCount"] for service in services),
        "controlSurfacesCountTowardCarrierBudget": False,
        "activeService": active_ref,
        "outcome": outcome,
        "reasonCode": reason,
        "requestedLifecycleActions": actions,
        "newWindowRecommended": any(action["action"] == "request-start-successor" for action in actions),
        "predecessorRetirementRequiresVerifiedSuccessor": True,
        "effectsRequireSettlement": bool(actions),
        "grantsWorkAuthority": False,
        "authorizesDispatch": False,
        "rawContentEmitted": False,
    }
