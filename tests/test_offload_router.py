from __future__ import annotations

import unittest

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from offload_router import (
    plan_control_services,
    plan_lane_budget,
    route_task,
    settle_carrier_effect,
    settle_worker_execution,
)


def task(**overrides):
    value = {
        "schema": "lazy-offload-request/v1",
        "taskRef": "task:one",
        "kind": "research",
        "urgency": "background",
        "deterministicObservation": False,
        "ownerQueryAvailable": False,
        "requiresNovelJudgment": False,
        "requiresRepositoryMutation": False,
        "requiresLiveBrowser": False,
        "consequentialEffect": False,
        "carrierContextAdvantage": True,
        "retainedCarrierContinuation": False,
        "parallelizable": True,
        "containsSensitiveMaterial": False,
        "expectedOutputChars": 30000,
    }
    value.update(overrides)
    return value


def lane(index, **overrides):
    value = {
        "laneRef": f"lane:{index}",
        "laneGeneration": 1,
        "state": "responsive",
        "activity": "idle",
        "protected": False,
        "blockers": [],
        "lastUsedAt": f"2026-08-{index + 1:02d}T00:00:00Z",
    }
    value.update(overrides)
    return value


def inventory(lanes):
    return {
        "schema": "lazy-elatura-lane-inventory/v1",
        "observedAt": "2026-08-31T00:00:00Z",
        "lanes": lanes,
    }


def control_service(**overrides):
    value = {
        "serviceRef": "edge-control:primary",
        "serviceGeneration": 3,
        "role": "primary",
        "state": "connected",
        "heartbeatAt": "2026-08-31T00:09:30Z",
        "inventoryVerifiedAt": "2026-08-31T00:09:35Z",
        "surfaceCount": 2,
        "protected": False,
        "blockers": [],
    }
    value.update(overrides)
    return value


def control_inventory(services):
    return {
        "schema": "lazy-control-service-inventory/v1",
        "observedAt": "2026-08-31T00:10:00Z",
        "browserFamily": "edge",
        "services": services,
    }


class OffloadRouterTests(unittest.TestCase):
    def test_routes_long_independent_research_to_retained_carrier(self):
        decision = route_task(task())
        self.assertEqual(decision["route"], "chatgpt-carrier")
        self.assertEqual(decision["wakeOn"], "material-elatura-delta")
        self.assertTrue(decision["browserMessageRequiresActionTimeConfirmation"])
        self.assertFalse(decision["authorizesDispatch"])

    def test_routes_small_retained_context_continuation_to_same_carrier(self):
        decision = route_task(
            task(
                kind="coordination",
                retainedCarrierContinuation=True,
                parallelizable=False,
                expectedOutputChars=1500,
            )
        )
        self.assertEqual(decision["route"], "chatgpt-carrier")
        self.assertIn(
            "retained-context-continuation-avoids-rehydration",
            decision["reasonCodes"],
        )

    def test_small_continuation_without_retained_context_stays_local(self):
        decision = route_task(
            task(
                retainedCarrierContinuation=True,
                carrierContextAdvantage=False,
                parallelizable=False,
                expectedOutputChars=1500,
            )
        )
        self.assertEqual(decision["route"], "codex-local")

    def test_routes_mechanical_observation_to_worker(self):
        decision = route_task(
            task(
                kind="verification",
                deterministicObservation=True,
                carrierContextAdvantage=False,
                expectedOutputChars=1000,
            )
        )
        self.assertEqual(decision["route"], "workstation-worker")
        self.assertFalse(decision["browserMessageRequiresActionTimeConfirmation"])

    def test_effect_mutation_browser_and_sensitive_work_stay_local(self):
        cases = (
            {"consequentialEffect": True},
            {"requiresRepositoryMutation": True},
            {"requiresLiveBrowser": True},
            {"containsSensitiveMaterial": True},
        )
        for values in cases:
            with self.subTest(values=values):
                self.assertEqual(route_task(task(**values))["route"], "codex-local")

    def test_soft_cap_requests_lru_parking_without_authorizing_it(self):
        decision = plan_lane_budget(inventory([lane(i) for i in range(12)]), 1)
        self.assertEqual(decision["outcome"], "park-before-admit")
        self.assertFalse(decision["admitNow"])
        self.assertEqual(decision["requestedLifecycleActions"][0]["laneRef"], "lane:0")
        self.assertTrue(decision["effectsRequireElaturaSettlement"])
        self.assertFalse(decision["authorizesDispatch"])

    def test_hard_cap_prefers_old_reclaimable_lane(self):
        lanes = [lane(i, state="suspended") for i in range(20)]
        lanes[7]["state"] = "reclaimable"
        decision = plan_lane_budget(inventory(lanes), 1)
        self.assertEqual(decision["outcome"], "cleanup-required")
        self.assertEqual(decision["requestedLifecycleActions"], [
            {"action": "request-close", "laneRef": "lane:7", "laneGeneration": 1}
        ])
        self.assertFalse(decision["admitNow"])

    def test_blockers_prevent_automatic_parking(self):
        lanes = [lane(i, blockers=["unsaved"]) for i in range(12)]
        decision = plan_lane_budget(inventory(lanes), 1)
        self.assertEqual(decision["outcome"], "soft-cap-decision")
        self.assertEqual(decision["requestedLifecycleActions"], [])

    def test_ambiguous_carrier_effect_never_authorizes_retry(self):
        settlement = settle_carrier_effect(route_task(task()), "ambiguous")
        self.assertFalse(settlement["effectConfirmed"])
        self.assertFalse(settlement["retryAuthorized"])
        self.assertTrue(settlement["requiresReconciliation"])
        self.assertIn("reconcile", settlement["next"])

    def test_committed_carrier_effect_requires_opaque_conversation_identity(self):
        decision = route_task(task())
        with self.assertRaises(ValueError):
            settle_carrier_effect(decision, "committed")
        settlement = settle_carrier_effect(
            decision, "committed", "conversation:opaque-1"
        )
        self.assertTrue(settlement["effectConfirmed"])
        self.assertFalse(settlement["retryAuthorized"])

    def test_committed_worker_execution_binds_content_free_artifact(self):
        decision = route_task(task(
            deterministicObservation=True,
            ownerQueryAvailable=True,
            carrierContextAdvantage=False,
            expectedOutputChars=6000,
        ))
        self.assertEqual(decision["route"], "workstation-worker")
        settlement = settle_worker_execution(
            decision, "committed", "execution:unit-1", "a" * 64, 4159
        )
        self.assertEqual(settlement["artifactBytes"], 4159)
        self.assertFalse(settlement["retryAuthorized"])
        self.assertFalse(settlement["authorizesWork"])

    def test_worker_execution_refuses_wrong_route_and_false_artifact_claim(self):
        with self.assertRaises(ValueError):
            settle_worker_execution(route_task(task()), "committed", "execution:x", "a" * 64, 1)
        decision = route_task(task(
            deterministicObservation=True,
            ownerQueryAvailable=True,
            carrierContextAdvantage=False,
        ))
        with self.assertRaises(ValueError):
            settle_worker_execution(decision, "failed", "execution:x", "a" * 64, 1)

    def test_fresh_verified_control_service_keeps_current_window(self):
        decision = plan_control_services(control_inventory([control_service()]))
        self.assertEqual(decision["outcome"], "keep-primary")
        self.assertFalse(decision["newWindowRecommended"])
        self.assertEqual(decision["requestedLifecycleActions"], [])
        self.assertFalse(decision["controlSurfacesCountTowardCarrierBudget"])

    def test_stale_primary_starts_successor_without_retiring_predecessor(self):
        stale = control_service(
            heartbeatAt="2026-08-30T23:00:00Z",
            inventoryVerifiedAt="2026-08-30T23:00:00Z",
        )
        decision = plan_control_services(control_inventory([stale]))
        self.assertEqual(decision["outcome"], "rotation-required")
        self.assertEqual(decision["requestedLifecycleActions"], [
            {"action": "request-start-successor", "browserFamily": "edge"}
        ])
        self.assertTrue(decision["predecessorRetirementRequiresVerifiedSuccessor"])

    def test_starting_successor_suppresses_duplicate_window(self):
        starting = control_service(
            role="successor",
            state="starting",
            inventoryVerifiedAt=None,
        )
        decision = plan_control_services(control_inventory([starting]))
        self.assertEqual(decision["outcome"], "wait-successor-verification")
        self.assertFalse(decision["newWindowRecommended"])
        self.assertEqual(decision["requestedLifecycleActions"], [])

    def test_verified_successor_is_promoted_before_old_service_is_retired(self):
        old = control_service(
            state="degraded",
            heartbeatAt="2026-08-30T23:00:00Z",
            inventoryVerifiedAt="2026-08-30T23:00:00Z",
        )
        successor = control_service(
            serviceRef="edge-control:successor",
            serviceGeneration=4,
            role="successor",
        )
        decision = plan_control_services(control_inventory([old, successor]))
        self.assertEqual(decision["outcome"], "successor-ready")
        self.assertEqual(
            [action["action"] for action in decision["requestedLifecycleActions"]],
            ["request-promote-successor", "request-retire-after-promotion"],
        )

    def test_protected_predecessor_is_not_requested_for_retirement(self):
        old = control_service(
            state="degraded",
            protected=True,
            heartbeatAt="2026-08-30T23:00:00Z",
            inventoryVerifiedAt="2026-08-30T23:00:00Z",
        )
        successor = control_service(
            serviceRef="edge-control:successor",
            serviceGeneration=4,
            role="successor",
        )
        decision = plan_control_services(control_inventory([old, successor]))
        self.assertEqual(len(decision["requestedLifecycleActions"]), 1)
        self.assertEqual(
            decision["requestedLifecycleActions"][0]["action"],
            "request-promote-successor",
        )

    def test_verified_primary_keeps_only_one_warm_successor(self):
        primary = control_service()
        successor_four = control_service(
            serviceRef="edge-control:successor-four",
            serviceGeneration=4,
            role="successor",
        )
        successor_five = control_service(
            serviceRef="edge-control:successor-five",
            serviceGeneration=5,
            role="successor",
        )
        decision = plan_control_services(
            control_inventory([primary, successor_four, successor_five])
        )
        self.assertEqual(decision["outcome"], "cleanup-required")
        self.assertEqual(decision["requestedLifecycleActions"], [
            {
                "action": "request-retire",
                "serviceRef": "edge-control:successor-four",
                "serviceGeneration": 4,
            }
        ])


if __name__ == "__main__":
    unittest.main()
