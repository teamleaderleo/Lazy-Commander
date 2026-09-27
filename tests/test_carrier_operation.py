from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPTS = Path(__file__).parents[1] / "scripts"
SCRIPT = SCRIPTS / "carrier_operation.py"
CONTROL_SCRIPT = SCRIPTS / "control_stability.py"
sys.path.insert(0, str(SCRIPTS))
from offload_router import digest, validate_control_inventory  # noqa: E402


def request(operation_id="op-1", prompt="bounded prompt"):
    return {
        "schema": "lazy-carrier-operation-request/v1",
        "operationId": operation_id,
        "routeRunId": "route-1",
        "routeGeneration": 1,
        "promptSha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "promptCharacters": len(prompt),
        "projectRef": "project:lazy-legion-lab",
        "laneRef": "lane:edge-primary",
        "laneGeneration": 7,
        "surfaceProfile": "chatgpt-project-v1",
        "actionConfirmationRef": "confirmation:user-goal-1",
    }


class CarrierOperationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.control_state = self.root / "control-state"
        self.gate_counter = 0

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, *args, success=True):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--state-root", str(self.state), *args],
            check=False, capture_output=True, text=True,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def prepare(self, value=None):
        source = self.root / "request.json"
        source.write_text(json.dumps(value or request()), encoding="utf-8")
        return self.run_cli("prepare", "--request", str(source))

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def gate(self, *, operation_request=None, control_stale=False, stability_status="stable", stability_generation=None):
        operation_request = operation_request or request()
        self.gate_counter += 1
        observed = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=1)
        observed_text = observed.isoformat()
        request_path = self.write_json("gate-request.json", operation_request)
        route_path = self.write_json("route-receipt.json", {
            "schema": "lazy-routing-receipt/v1",
            "run_id": operation_request["routeRunId"],
            "route": "chatgpt-carrier",
        })
        lane_path = self.write_json("lanes.json", {
            "schema": "lazy-elatura-lane-inventory/v1",
            "observedAt": observed_text,
            "lanes": [{
                "laneRef": operation_request["laneRef"],
                "laneGeneration": operation_request["laneGeneration"],
                "state": "responsive",
                "activity": "idle",
                "protected": True,
                "blockers": [],
                "lastUsedAt": (observed - timedelta(minutes=1)).isoformat(),
            }],
        })
        control_time = observed - (timedelta(hours=2) if control_stale else timedelta(seconds=30))
        controls = {
            "schema": "lazy-control-service-inventory/v1",
            "observedAt": observed_text,
            "browserFamily": "edge",
            "services": [{
                "serviceRef": "edge-control:primary",
                "serviceGeneration": 3,
                "role": "primary",
                "state": "connected",
                "heartbeatAt": control_time.isoformat(),
                "inventoryVerifiedAt": control_time.isoformat(),
                "surfaceCount": 2,
                "protected": False,
                "blockers": [],
            }],
        }
        controls_path = self.write_json("controls.json", controls)
        inventory_sha = digest(validate_control_inventory(controls))
        scope = {
            "browserFamily": "edge",
            "surfaceProfile": operation_request["surfaceProfile"],
            "projectRef": operation_request["projectRef"],
            "laneRef": operation_request["laneRef"],
            "laneGeneration": operation_request["laneGeneration"],
            "controlInventorySha256": inventory_sha,
            "probeVersion": "carrier-probe-v1",
        }
        generation = stability_generation or 3
        self.last_control_scope = scope
        self.last_control_generation = generation
        self.last_control_observed = observed
        self.last_operation_request = operation_request
        if stability_status == "stable":
            moments = (observed - timedelta(seconds=60), observed - timedelta(seconds=30), observed)
            successes = (True, True, True)
        else:
            moments = (observed,)
            successes = (stability_status != "rotate",)
        identity = f"{inventory_sha[:12]}-{generation}-{stability_status}"
        for index, (moment, success) in enumerate(zip(moments, successes), start=1):
            event_path = self.write_json(f"control-event-{self.gate_counter}-{index}.json", {
                "schema": "lazy-control-stability-observation/v2",
                "eventId": f"event-{identity}-{index}",
                "controlRef": "edge-control:primary",
                "controlGeneration": generation,
                "observedAt": moment.isoformat(),
                "scope": scope,
                "capabilities": {
                    "inventoryReadable": success,
                    "projectSurfaceIdentified": success,
                    "composerWritable": success,
                    "sendControlIdentified": success,
                },
            })
            result = subprocess.run(
                [
                    sys.executable, str(CONTROL_SCRIPT), "--state-root", str(self.control_state),
                    "record", "--observation", str(event_path),
                ],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        scope_path = self.write_json(f"control-scope-{self.gate_counter}.json", scope)
        stability_path = self.root / f"stability-{self.gate_counter}.json"
        result = subprocess.run(
            [
                sys.executable, str(CONTROL_SCRIPT), "--state-root", str(self.control_state),
                "status", "--control-ref", "edge-control:primary",
                "--control-generation", str(generation), "--evaluated-at", observed_text,
                "--scope", str(scope_path), "--output", str(stability_path),
            ],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.last_stability_path = stability_path
        self.last_gate_cli_args = (
            "gate", "--request", str(request_path),
            "--route-receipt", str(route_path),
            "--lane-inventory", str(lane_path),
            "--control-inventory", str(controls_path),
            "--control-stability", str(stability_path),
            "--control-stability-state-root", str(self.control_state),
        )
        return self.run_cli(*self.last_gate_cli_args)

    def issue(self, prepared, *, operation_request=None):
        operation_request = operation_request or request(operation_id=prepared["operationId"])
        gate = self.gate(operation_request=operation_request)
        self.assertEqual(gate["outcome"], "ready-to-confirm-and-issue")
        return self.run_cli(
            "issue", "--operation-id", prepared["operationId"],
            "--binding-sha256", prepared["bindingSha256"],
            "--gate-receipt-sha256", gate["gateReceiptSha256"],
            "--control-stability-state-root", str(self.control_state),
        )

    def test_issue_is_admitted_once_and_crash_resume_never_resends(self):
        prepared = self.prepare()
        self.assertEqual(prepared["laneRef"], "lane:edge-primary")
        self.assertEqual(prepared["promptCharacters"], len("bounded prompt"))
        self.assertFalse(prepared["rawContentEmitted"])
        gate = self.gate()
        issue_args = (
            "issue", "--operation-id", "op-1",
            "--binding-sha256", prepared["bindingSha256"],
            "--gate-receipt-sha256", gate["gateReceiptSha256"],
            "--control-stability-state-root", str(self.control_state),
        )
        issued = self.run_cli(*issue_args)
        self.assertTrue(issued["effectAttemptAdmitted"])
        with closing(sqlite3.connect(self.state / "carrier-operations.sqlite3")) as database:
            database.execute(
                "UPDATE operations SET gate_expires_at='2000-01-01T00:00:00+00:00' WHERE operation_id='op-1'"
            )
        replay = self.run_cli(*issue_args)
        self.assertFalse(replay["effectAttemptAdmitted"])
        self.assertEqual(replay["attemptRef"], issued["attemptRef"])
        resume = self.run_cli("resume", "--operation-id", "op-1")
        self.assertEqual(resume["next"], "reconcile-effect-only")
        self.assertFalse(resume["sendAuthorized"])
        self.assertFalse(resume["retryAuthorized"])

    def test_settlement_and_response_progress_are_exact_and_monotonic(self):
        prepared = self.prepare()
        issued = self.issue(prepared)
        settle_args = (
            "settle", "--operation-id", "op-1", "--attempt-ref", issued["attemptRef"],
            "--outcome", "committed", "--conversation-ref", "chatgpt:conversation-1",
        )
        settled = self.run_cli(*settle_args)
        self.assertEqual(settled["effectState"], "committed")
        self.assertEqual(self.run_cli(*settle_args)["outcome"], "replay")
        self.run_cli(
            "settle", "--operation-id", "op-1", "--attempt-ref", issued["attemptRef"],
            "--outcome", "ambiguous", success=False,
        )
        pending = self.run_cli("record-response", "--operation-id", "op-1", "--state", "pending")
        self.assertEqual(pending["responseState"], "pending")
        complete_args = (
            "record-response", "--operation-id", "op-1", "--state", "complete",
            "--artifact-ref", "private:artifact-1", "--artifact-sha256", "a" * 64,
            "--artifact-bytes", "1234",
        )
        complete = self.run_cli(*complete_args)
        self.assertEqual(complete["responseState"], "complete")
        self.assertEqual(self.run_cli(*complete_args)["outcome"], "replay")
        self.run_cli("record-response", "--operation-id", "op-1", "--state", "pending", success=False)
        resume = self.run_cli("resume", "--operation-id", "op-1")
        self.assertEqual(resume["next"], "terminal")

    def test_issue_rejects_wrong_binding_sha(self):
        first = self.prepare()
        self.assertEqual(first["outcome"], "prepared")
        self.assertEqual(self.prepare()["outcome"], "replay")
        gate = self.gate()
        refused = self.run_cli(
            "issue", "--operation-id", "op-1", "--binding-sha256", "b" * 64,
            "--gate-receipt-sha256", gate["gateReceiptSha256"],
            "--control-stability-state-root", str(self.control_state),
            success=False,
        )
        self.assertIn("does not match", refused.stderr)

    def test_changed_binding_same_operation_is_refused_and_state_is_private(self):
        self.prepare()
        changed_path = self.root / "changed.json"
        changed_path.write_text(json.dumps(request(prompt="changed")), encoding="utf-8")
        refused = self.run_cli("prepare", "--request", str(changed_path), success=False)
        self.assertIn("changed stable binding", refused.stderr)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        database = self.state / "carrier-operations.sqlite3"
        self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)

    def test_character_count_is_identity_metadata_not_an_acceptance_ceiling(self):
        value = request()
        value["promptCharacters"] = 10**30
        prepared = self.prepare(value)
        self.assertEqual(prepared["promptCharacters"], 10**30)
        self.assertEqual(prepared["outcome"], "prepared")

    def test_noncommit_settlements_are_terminal_and_never_authorize_retry(self):
        for index, outcome in enumerate(("verified-no-effect", "ambiguous"), start=1):
            operation_id = f"op-{index}"
            operation_request = request(operation_id=operation_id)
            prepared = self.prepare(operation_request)
            issued = self.issue(prepared, operation_request=operation_request)
            settled = self.run_cli(
                "settle", "--operation-id", operation_id,
                "--attempt-ref", issued["attemptRef"], "--outcome", outcome,
            )
            self.assertEqual(settled["effectState"], outcome)
            self.assertFalse(settled["retryAuthorized"])
            resume = self.run_cli("resume", "--operation-id", operation_id)
            expected = "terminal" if outcome == "verified-no-effect" else "reconcile-effect-only"
            self.assertEqual(resume["next"], expected)
            self.assertFalse(resume["sendAuthorized"])

    def test_unissued_prepared_operation_cancels_once_and_can_never_issue(self):
        prepared = self.prepare()
        gate = self.gate()
        cancel_args = (
            "cancel", "--operation-id", "op-1",
            "--reason-code", "lane-rotated-before-issue",
        )
        cancelled = self.run_cli(*cancel_args)
        self.assertEqual(cancelled["effectState"], "cancelled")
        self.assertEqual(cancelled["cancelReason"], "lane-rotated-before-issue")
        self.assertEqual(self.run_cli(*cancel_args)["outcome"], "replay")
        self.run_cli(
            "cancel", "--operation-id", "op-1",
            "--reason-code", "different-reason", success=False,
        )
        self.run_cli(
            "issue", "--operation-id", "op-1",
            "--binding-sha256", prepared["bindingSha256"],
            "--gate-receipt-sha256", gate["gateReceiptSha256"],
            "--control-stability-state-root", str(self.control_state), success=False,
        )
        resume = self.run_cli("resume", "--operation-id", "op-1")
        self.assertEqual(resume["next"], "terminal")
        self.assertFalse(resume["sendAuthorized"])

    def test_preparation_gate_runs_control_and_lane_checks_before_prepare_or_issue(self):
        unprepared = self.gate()
        self.assertEqual(unprepared["outcome"], "ready-to-prepare")
        self.assertTrue(unprepared["controlReady"])
        self.assertTrue(unprepared["laneReady"])
        prepared = self.prepare()
        ready = self.gate()
        self.assertEqual(ready["outcome"], "ready-to-confirm-and-issue")
        stale = self.gate(control_stale=True)
        self.assertEqual(stale["outcome"], "cancel-prepared-operation")
        self.assertEqual(stale["cancelReason"], "control-not-ready-before-issue")
        self.run_cli(
            "cancel", "--operation-id", "op-1",
            "--reason-code", stale["cancelReason"],
        )
        self.assertEqual(self.gate(control_stale=True)["outcome"], "terminal")

    def test_preparation_gate_never_downgrades_issued_state_to_rotation(self):
        prepared = self.prepare()
        self.issue(prepared)
        decision = self.gate(control_stale=True)
        self.assertEqual(decision["outcome"], "reconcile-only")
        self.assertEqual(decision["next"], "reconcile-effect-only")
        self.assertFalse(decision["retryAuthorized"])

    def test_preparation_gate_waits_for_stability_and_cancels_if_it_is_lost(self):
        waiting = self.gate(stability_status="wait")
        self.assertEqual(waiting["outcome"], "wait-for-control-stability-before-prepare")
        self.assertTrue(waiting["controlLeaseReady"])
        self.assertFalse(waiting["controlStabilityReady"])
        self.prepare()
        cancel = self.gate(stability_status="wait")
        self.assertEqual(cancel["outcome"], "cancel-prepared-operation")
        self.assertEqual(cancel["cancelReason"], "control-stability-lost-before-issue")

    def test_preparation_gate_refuses_stability_for_another_control_generation(self):
        decision = self.gate(stability_generation=4)
        self.assertEqual(decision["outcome"], "wait-for-control-stability-before-prepare")
        self.assertFalse(decision["controlStabilityReady"])

    def test_issue_cannot_bypass_a_durable_gate_acceptance(self):
        prepared = self.prepare()
        refused = self.run_cli(
            "issue", "--operation-id", "op-1",
            "--binding-sha256", prepared["bindingSha256"],
            "--gate-receipt-sha256", "a" * 64,
            "--control-stability-state-root", str(self.control_state),
            success=False,
        )
        self.assertIn("exact durable gate acceptance", refused.stderr)

    def test_self_hashed_fabricated_stability_receipt_has_no_provenance(self):
        self.gate()
        forged = json.loads(self.last_stability_path.read_text())
        forged["eventSetSha256"] = "c" * 64
        unsigned = {key: value for key, value in forged.items() if key != "receiptSha256"}
        forged["receiptSha256"] = hashlib.sha256(
            json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        self.last_stability_path.write_text(json.dumps(forged), encoding="utf-8")
        refused = self.run_cli(*self.last_gate_cli_args, success=False)
        self.assertIn("lacks durable ledger provenance", refused.stderr)

    def test_policy_downgrade_is_refused_even_when_self_hashed(self):
        self.gate()
        forged = json.loads(self.last_stability_path.read_text())
        forged["policy"] = {
            "requiredSuccesses": 1,
            "minSpanSeconds": 1,
            "maxAgeSeconds": 999999,
        }
        forged["policySha256"] = hashlib.sha256(
            json.dumps(forged["policy"], ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        unsigned = {key: value for key, value in forged.items() if key != "receiptSha256"}
        forged["receiptSha256"] = hashlib.sha256(
            json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        self.last_stability_path.write_text(json.dumps(forged), encoding="utf-8")
        refused = self.run_cli(*self.last_gate_cli_args, success=False)
        self.assertIn("not the approved policy", refused.stderr)

    def test_new_failure_after_gate_prevents_issue(self):
        prepared = self.prepare()
        gate = self.gate()
        failure_path = self.write_json("post-gate-failure.json", {
            "schema": "lazy-control-stability-observation/v2",
            "eventId": "event-post-gate-failure",
            "controlRef": "edge-control:primary",
            "controlGeneration": self.last_control_generation,
            "observedAt": datetime.now(timezone.utc).isoformat(),
            "scope": self.last_control_scope,
            "capabilities": {
                "inventoryReadable": False,
                "projectSurfaceIdentified": False,
                "composerWritable": False,
                "sendControlIdentified": False,
            },
        })
        recorded = subprocess.run(
            [
                sys.executable, str(CONTROL_SCRIPT), "--state-root", str(self.control_state),
                "record", "--observation", str(failure_path),
            ],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        refused = self.run_cli(
            "issue", "--operation-id", "op-1",
            "--binding-sha256", prepared["bindingSha256"],
            "--gate-receipt-sha256", gate["gateReceiptSha256"],
            "--control-stability-state-root", str(self.control_state),
            success=False,
        )
        self.assertIn("no longer valid", refused.stderr)


if __name__ == "__main__":
    unittest.main()
