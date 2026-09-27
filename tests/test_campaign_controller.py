import copy
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import campaign_controller as controller

LAZY_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "lazy.py"


def compiled(**overrides):
    return controller.compile_controller(controller.make_snapshot(**overrides))


class CampaignControllerTests(unittest.TestCase):
    def assert_zero_authority(self, view):
        self.assertFalse(view["rawContentEmitted"])
        self.assertFalse(view["authorizesWork"])
        self.assertFalse(view["authorizesEffects"])
        self.assertFalse(view["authorizesDispatch"])
        self.assertEqual(
            view["authority"],
            {
                "authorizesWork": False,
                "authorizesEffects": False,
                "authorizesDispatch": False,
                "rawContentEmitted": False,
            },
        )

    def test_minimal_worker_snapshot_emits_one_effect_advice(self):
        view = compiled(expectedInputChars=5, expectedOutputChars=4)
        self.assertEqual(view["transition"]["kind"], "effect")
        self.assertEqual(view["transition"]["route"], "workstation-worker")
        self.assertEqual(view["cost"]["estimatedTokens"], 3)
        self.assertEqual(view["cost"]["codexReservationTokens"], 0)
        self.assertEqual(view["acceptanceProposal"]["transitionId"], view["decisionFingerprint"])
        self.assertEqual(view["acceptanceProposal"]["evaluatedOwnerFingerprint"], view["snapshotSha256"])
        self.assert_zero_authority(view)

    def test_cost_estimate_rounds_up(self):
        self.assertEqual(controller.estimate_cost(0, 0), 0)
        self.assertEqual(controller.estimate_cost(1, 0), 1)
        self.assertEqual(controller.estimate_cost(4, 0), 1)
        self.assertEqual(controller.estimate_cost(5, 0), 2)

    def test_unknown_snapshot_key_fails_closed(self):
        snapshot = controller.make_snapshot()
        snapshot["rawPrompt"] = "must never enter the controller"
        with self.assertRaisesRegex(controller.ControllerError, "unknown rawPrompt"):
            controller.compile_controller(snapshot)

    def test_authority_grant_in_owner_summary_fails_closed(self):
        snapshot = controller.make_snapshot()
        snapshot["routeDecision"]["authorizesDispatch"] = True
        with self.assertRaisesRegex(controller.ControllerError, "must be false"):
            controller.compile_controller(snapshot)

    def test_invalid_budget_fails_closed(self):
        snapshot = controller.make_snapshot(codexBudget={"limit": 5, "reserved": 3, "consumed": 3})
        with self.assertRaisesRegex(controller.ControllerError, "exceeds limit"):
            controller.compile_controller(snapshot)

    def test_exact_worker_replay_does_not_redispatch(self):
        snapshot = controller.make_snapshot()
        first = controller.compile_controller(snapshot)
        snapshot["previousSnapshotSha256"] = first["snapshotSha256"]
        replay = controller.compile_controller(snapshot)
        self.assertTrue(replay["unchanged"])
        self.assertEqual(replay["snapshotSha256"], first["snapshotSha256"])
        self.assertEqual(replay["transition"]["kind"], "wake")
        self.assertEqual(replay["transition"]["reason"], "unchanged-snapshot-await-material-delta")

    def test_generation_drift_rotates_decision(self):
        snapshot = controller.make_snapshot()
        first = controller.compile_controller(snapshot)
        snapshot["previousSnapshotSha256"] = first["snapshotSha256"]
        snapshot["routeDecision"]["generation"] += 1
        changed = controller.compile_controller(snapshot)
        self.assertFalse(changed["unchanged"])
        self.assertNotEqual(changed["snapshotSha256"], first["snapshotSha256"])
        self.assertEqual(changed["transition"]["kind"], "effect")

    def test_owner_cursor_drift_rotates_snapshot_and_acceptance_binding(self):
        snapshot = controller.make_snapshot()
        first = controller.compile_controller(snapshot)
        snapshot["previousSnapshotSha256"] = first["snapshotSha256"]
        snapshot["ownerCursors"][0]["generation"] += 1
        changed = controller.compile_controller(snapshot)
        self.assertNotEqual(changed["snapshotSha256"], first["snapshotSha256"])
        self.assertNotEqual(
            changed["acceptanceProposal"]["evaluatedCursorVectorFingerprint"],
            first["acceptanceProposal"]["evaluatedCursorVectorFingerprint"],
        )

    def test_owner_cursor_order_is_normalized_and_duplicates_fail_closed(self):
        snapshot = controller.make_snapshot()
        first = controller.compile_controller(snapshot)
        snapshot["ownerCursors"].reverse()
        reordered = controller.compile_controller(snapshot)
        self.assertEqual(reordered["snapshotSha256"], first["snapshotSha256"])
        snapshot["ownerCursors"][1]["ownerRef"] = snapshot["ownerCursors"][0]["ownerRef"]
        with self.assertRaisesRegex(controller.ControllerError, "must be unique"):
            controller.compile_controller(snapshot)

    def test_legacy_v1_snapshot_remains_admissible_with_empty_cursor_vector(self):
        snapshot = controller.make_snapshot()
        snapshot["schema"] = controller.LEGACY_INPUT_SCHEMA
        snapshot.pop("ownerCursors")
        view = controller.compile_controller(snapshot)
        self.assertEqual(view["inputs"]["ownerCursorCount"], 0)
        self.assertEqual(
            view["acceptanceProposal"]["evaluatedCursorVectorFingerprint"],
            controller.digest([]),
        )

    def test_observation_time_drift_is_not_a_material_change(self):
        snapshot = controller.make_snapshot()
        first = controller.compile_controller(snapshot)
        snapshot["previousSnapshotSha256"] = first["snapshotSha256"]
        snapshot["observedAt"] = "2026-08-31T00:10:00Z"
        later = controller.compile_controller(snapshot)
        self.assertEqual(later["snapshotSha256"], first["snapshotSha256"])
        self.assertTrue(later["unchanged"])
        self.assertEqual(later["transition"]["kind"], "wake")
        self.assertNotIn("observedAt", later)
        self.assertEqual(later["temporalClass"], "timeless")

    def test_worker_does_not_depend_on_browser_lane_or_control(self):
        snapshot = controller.make_snapshot()
        snapshot["lanePlan"]["status"] = "blocked"
        snapshot["controlPlan"]["status"] = "stale"
        self.assertEqual(controller.compile_controller(snapshot)["transition"]["kind"], "effect")

    def test_carrier_requires_ready_lane(self):
        snapshot = controller.make_snapshot()
        snapshot["routeDecision"]["route"] = "chatgpt-carrier"
        snapshot["lanePlan"]["status"] = "blocked"
        view = controller.compile_controller(snapshot)
        self.assertEqual(view["transition"]["kind"], "blocked")
        self.assertEqual(view["transition"]["reason"], "lanePlan-not-ready")

    def test_carrier_requires_ready_control(self):
        snapshot = controller.make_snapshot()
        snapshot["routeDecision"]["route"] = "chatgpt-carrier"
        snapshot["controlPlan"]["status"] = "stale"
        view = controller.compile_controller(snapshot)
        self.assertEqual(view["transition"]["kind"], "blocked")
        self.assertEqual(view["transition"]["reason"], "controlPlan-not-ready")

    def test_carrier_requires_action_time_confirmation(self):
        snapshot = controller.make_snapshot()
        snapshot["routeDecision"]["route"] = "chatgpt-carrier"
        view = controller.compile_controller(snapshot)
        self.assertEqual(view["transition"]["kind"], "blocked")
        self.assertEqual(view["transition"]["reason"], "action-time-confirmation-required")

    def test_confirmed_carrier_is_advice_not_authority(self):
        snapshot = controller.make_snapshot()
        snapshot["routeDecision"]["route"] = "chatgpt-carrier"
        snapshot["actionTimeConfirmation"] = {
            "confirmed": True,
            "ref": "confirmation:17",
            "generation": 1,
            "requestFingerprint": "0" * 64,
            "afterEffectRef": None,
            "afterEffectGeneration": None,
        }
        view = controller.compile_controller(snapshot)
        self.assertEqual(view["transition"]["kind"], "effect")
        self.assertEqual(view["transition"]["route"], "chatgpt-carrier")
        self.assert_zero_authority(view)

    def test_verified_no_effect_requires_fresh_confirmation(self):
        snapshot = controller.make_snapshot()
        snapshot["routeDecision"]["route"] = "chatgpt-carrier"
        snapshot["actionTimeConfirmation"] = {
            "confirmed": True,
            "ref": "confirmation:old",
            "generation": 1,
            "requestFingerprint": "0" * 64,
            "afterEffectRef": "effect:send-1",
            "afterEffectGeneration": 1,
        }
        snapshot["effectSettlement"] = {
            "ref": "effect:send-1",
            "generation": 1,
            "state": "verified-no-effect",
            "semanticFingerprint": None,
        }
        view = controller.compile_controller(snapshot)
        self.assertEqual(view["transition"]["kind"], "blocked")
        self.assertEqual(view["transition"]["reason"], "new-action-time-confirmation-required")

    def test_verified_no_effect_accepts_only_a_newer_confirmation(self):
        snapshot = controller.make_snapshot()
        snapshot["routeDecision"]["route"] = "chatgpt-carrier"
        snapshot["actionTimeConfirmation"] = {
            "confirmed": True,
            "ref": "confirmation:new",
            "generation": 2,
            "requestFingerprint": "0" * 64,
            "afterEffectRef": "effect:send-1",
            "afterEffectGeneration": 1,
        }
        snapshot["effectSettlement"] = {
            "ref": "effect:send-1",
            "generation": 1,
            "state": "verified-no-effect",
            "semanticFingerprint": None,
        }
        view = controller.compile_controller(snapshot)
        self.assertEqual(view["transition"]["kind"], "effect")
        self.assertEqual(view["transition"]["reason"], "confirmed-carrier-effect-advice")

    def test_issued_and_ambiguous_effects_reconcile(self):
        for state in ("issued", "ambiguous"):
            with self.subTest(state=state):
                snapshot = controller.make_snapshot()
                snapshot["effectSettlement"] = {
                    "ref": "effect:one",
                    "generation": 1,
                    "state": state,
                    "semanticFingerprint": None,
                }
                view = controller.compile_controller(snapshot)
                self.assertEqual(view["transition"]["kind"], "reconcile")

    def test_ambiguous_effect_reconciliation_outranks_budget_block(self):
        snapshot = controller.make_snapshot()
        snapshot["routeDecision"]["route"] = "codex-local"
        snapshot["expectedInputChars"] = 400
        snapshot["codexBudget"] = {"limit": 0, "reserved": 0, "consumed": 0}
        snapshot["effectSettlement"] = {
            "ref": "effect:ambiguous",
            "generation": 1,
            "state": "ambiguous",
            "semanticFingerprint": None,
        }
        self.assertEqual(controller.compile_controller(snapshot)["transition"]["kind"], "reconcile")

    def test_confirmation_must_bind_exact_route_fingerprint(self):
        snapshot = controller.make_snapshot()
        snapshot["routeDecision"]["route"] = "chatgpt-carrier"
        snapshot["actionTimeConfirmation"] = {
            "confirmed": True,
            "ref": "confirmation:wrong",
            "generation": 1,
            "requestFingerprint": "f" * 64,
            "afterEffectRef": None,
            "afterEffectGeneration": None,
        }
        with self.assertRaisesRegex(controller.ControllerError, "does not bind"):
            controller.compile_controller(snapshot)

    def test_committed_effect_waits_for_semantic_delta(self):
        fingerprint = "a" * 64
        snapshot = controller.make_snapshot()
        snapshot["effectSettlement"] = {
            "ref": "effect:one",
            "generation": 1,
            "state": "committed",
            "semanticFingerprint": fingerprint,
        }
        view = controller.compile_controller(snapshot)
        self.assertEqual(view["transition"]["kind"], "wake")
        self.assertEqual(view["transition"]["reason"], "wait-material-semantic-delta")
        self.assertEqual(view["transition"]["ref"], "effect:one")

    def test_committed_effect_observes_only_changed_semantic_delta(self):
        snapshot = controller.make_snapshot()
        snapshot["effectSettlement"] = {
            "ref": "effect:one",
            "generation": 1,
            "state": "committed",
            "semanticFingerprint": "a" * 64,
        }
        snapshot["materialDelta"] = {"present": True, "generation": 2, "fingerprint": "b" * 64}
        view = controller.compile_controller(snapshot)
        self.assertEqual(view["transition"]["kind"], "observe")
        self.assertEqual(view["transition"]["reason"], "committed-effect-material-delta")

    def test_same_semantic_fingerprint_is_not_reobserved(self):
        snapshot = controller.make_snapshot()
        snapshot["effectSettlement"] = {
            "ref": "effect:one",
            "generation": 1,
            "state": "committed",
            "semanticFingerprint": "a" * 64,
        }
        snapshot["materialDelta"] = {"present": True, "generation": 2, "fingerprint": "a" * 64}
        view = controller.compile_controller(snapshot)
        self.assertEqual(view["transition"]["kind"], "wake")

    def test_complete_precedes_budget_admission(self):
        snapshot = controller.make_snapshot(
            step={"ref": "step:done", "generation": 1, "status": "complete"},
            routeDecision={
                "ref": "route:codex",
                "generation": 1,
                "route": "codex-local",
                "fingerprint": "0" * 64,
                "authorizesWork": False,
                "authorizesEffects": False,
                "authorizesDispatch": False,
            },
            expectedInputChars=8,
            codexBudget={"limit": 0, "reserved": 0, "consumed": 0},
        )
        view = controller.compile_controller(snapshot)
        self.assertEqual(view["transition"]["kind"], "complete")

    def test_paused_or_blocked_state_fails_closed(self):
        for status in ("paused", "blocked"):
            with self.subTest(status=status):
                view = compiled(step={"ref": "step:x", "generation": 1, "status": status})
                self.assertEqual(view["transition"]["kind"], "blocked")

    def test_codex_local_reserves_budget_and_can_be_blocked(self):
        snapshot = controller.make_snapshot()
        snapshot["routeDecision"]["route"] = "codex-local"
        snapshot["expectedInputChars"] = 400
        snapshot["codexBudget"] = {"limit": 99, "reserved": 0, "consumed": 0}
        view = controller.compile_controller(snapshot)
        self.assertEqual(view["cost"]["codexReservationTokens"], 100)
        self.assertFalse(view["cost"]["admitted"])
        self.assertEqual(view["transition"]["kind"], "blocked")

    def test_future_exact_deadline_wakes_without_polling(self):
        snapshot = controller.make_snapshot(
            event={"ref": "event:deadline", "generation": 1, "kind": "deadline"},
            deadlineAt="2026-08-31T00:05:00Z",
        )
        view = controller.compile_controller(snapshot)
        self.assertEqual(view["transition"]["kind"], "wake")
        self.assertEqual(view["transition"]["wakeAt"], "2026-08-31T00:05:00Z")

    def test_due_deadline_requests_one_owner_observation(self):
        snapshot = controller.make_snapshot(
            event={"ref": "event:deadline", "generation": 1, "kind": "deadline"},
            deadlineAt="2026-08-30T23:59:00Z",
        )
        view = controller.compile_controller(snapshot)
        self.assertEqual(view["transition"]["kind"], "observe")
        self.assertEqual(view["transition"]["reason"], "deadline-due-owner-observation")

    def test_deadline_crossing_rotates_decision_even_when_semantics_are_unchanged(self):
        snapshot = controller.make_snapshot(
            event={"ref": "event:deadline", "generation": 1, "kind": "deadline"},
            deadlineAt="2026-08-31T00:05:00Z",
        )
        future = controller.compile_controller(snapshot)
        snapshot["previousSnapshotSha256"] = future["snapshotSha256"]
        snapshot["observedAt"] = "2026-08-31T00:06:00Z"
        due = controller.compile_controller(snapshot)
        self.assertEqual(due["snapshotSha256"], future["snapshotSha256"])
        self.assertNotEqual(due["decisionFingerprint"], future["decisionFingerprint"])
        self.assertEqual(due["transition"]["kind"], "observe")

    def test_deadline_shape_mismatch_fails_closed(self):
        with self.assertRaisesRegex(controller.ControllerError, "supplied together"):
            compiled(deadlineAt="2026-08-31T00:05:00Z")

    def test_owner_event_requests_bounded_observation_not_effect(self):
        view = compiled(event={"ref": "event:owner-2", "generation": 2, "kind": "owner-event"})
        self.assertEqual(view["transition"]["kind"], "observe")
        self.assertEqual(view["transition"]["reason"], "owner-event-bounded-observation")

    def test_settlement_identity_is_required(self):
        snapshot = controller.make_snapshot()
        snapshot["effectSettlement"]["state"] = "issued"
        with self.assertRaisesRegex(controller.ControllerError, "requires ref and generation"):
            controller.compile_controller(snapshot)

    def test_material_delta_identity_is_all_or_nothing(self):
        snapshot = controller.make_snapshot(materialDelta={"present": True, "generation": 1, "fingerprint": None})
        with self.assertRaisesRegex(controller.ControllerError, "presence must match"):
            controller.compile_controller(snapshot)

    def test_compilation_is_byte_for_byte_deterministic(self):
        snapshot = controller.make_snapshot(expectedInputChars=123, expectedOutputChars=456)
        first = controller.compile_controller(copy.deepcopy(snapshot))
        second = controller.compile_controller(copy.deepcopy(snapshot))
        self.assertEqual(first, second)

    def test_cli_run_writes_private_content_free_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "snapshot.json"
            input_path.write_text(json.dumps(controller.make_snapshot()), encoding="utf-8")
            output = root / "result"
            receipt = controller.run(input_path, output)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            for name in ("view.json", "view.md", "receipt.json"):
                self.assertEqual(stat.S_IMODE((output / name).stat().st_mode), 0o600)
            self.assertFalse(receipt["rawContentEmitted"])
            self.assertFalse(receipt["authorizesDispatch"])

    def test_cli_exact_replay_reuses_identical_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "snapshot.json"
            input_path.write_text(json.dumps(controller.make_snapshot()), encoding="utf-8")
            output = root / "result"
            first = controller.run(input_path, output)
            second = controller.run(input_path, output)
            self.assertEqual(first, second)

    def test_cli_timestamp_only_refresh_reuses_identical_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "snapshot.json"
            snapshot = controller.make_snapshot()
            input_path.write_text(json.dumps(snapshot), encoding="utf-8")
            output = root / "result"
            first = controller.run(input_path, output)
            snapshot["observedAt"] = "2026-08-31T00:10:00Z"
            input_path.write_text(json.dumps(snapshot), encoding="utf-8")
            second = controller.run(input_path, output)
            self.assertEqual(first, second)

    def test_cli_changed_replay_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "snapshot.json"
            input_path.write_text(json.dumps(controller.make_snapshot()), encoding="utf-8")
            output = root / "result"
            controller.run(input_path, output)
            changed = controller.make_snapshot(step={"ref": "step:changed", "generation": 2, "status": "active"})
            input_path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(controller.ControllerError, "replay conflicts"):
                controller.run(input_path, output)

    def test_cli_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "snapshot.json"
            input_path.write_text('{"schema":"one","schema":"two"}', encoding="utf-8")
            with self.assertRaisesRegex(controller.ControllerError, "duplicate JSON object key"):
                controller.run(input_path, root / "result")

    def test_lazy_campaign_uses_durable_private_default_space(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            input_path = root / "snapshot.json"
            input_path.write_text(json.dumps(controller.make_snapshot()), encoding="utf-8")
            command = [
                sys.executable,
                str(LAZY_SCRIPT),
                "--state-root",
                str(state),
                "campaign",
                "--input",
                str(input_path),
            ]
            first = subprocess.run(command, check=False, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            receipt = json.loads(first.stdout)
            output = state / "campaigns" / receipt["decisionFingerprint"]
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            second = subprocess.run(command, check=False, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(second.stdout), receipt)


if __name__ == "__main__":
    unittest.main()
