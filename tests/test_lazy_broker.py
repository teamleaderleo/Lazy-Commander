from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "lazy.py"
sys.path.insert(0, str(SCRIPT.parent))
import campaign_controller as campaign  # noqa: E402


class LazyBrokerTests(unittest.TestCase):
    def call(self, root: Path, *arguments: str, cwd: Path | None = None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--state-root", str(root), *arguments],
            check=False,
            capture_output=True,
            text=True,
            cwd=cwd,
        )

    def test_defer_claim_execute_and_show_without_sleeping(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            state = work / "state"
            (work / "source.txt").write_text(
                "one\ntwo\nthree\nfour\nfive\n", encoding="utf-8"
            )
            past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            deferred = self.call(
                state,
                "defer",
                "--at",
                past,
                "--cwd",
                str(work),
                "--",
                "sed",
                "-n",
                "1,5p",
                "source.txt",
            )
            self.assertEqual(deferred.returncode, 0, deferred.stderr)
            task_id = json.loads(deferred.stdout)["task_id"]
            due = self.call(state, "due")
            self.assertEqual(json.loads(due.stdout)["due"][0]["id"], task_id)
            claimed = self.call(
                state, "claim", "--worker", "test-worker", "--lease-seconds", "60"
            )
            self.assertEqual(claimed.returncode, 0, claimed.stderr)
            claim_path = Path(json.loads(claimed.stdout)["claim"])
            claim = json.loads(claim_path.read_text(encoding="utf-8"))
            connection = sqlite3.connect(state / "broker.sqlite3")
            stored = connection.execute(
                "SELECT status, lease_token_sha256 FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            connection.close()
            self.assertEqual(stored[0], "leased")
            self.assertNotEqual(stored[1], claim["lease_token"])
            self.assertEqual(
                stored[1], hashlib.sha256(claim["lease_token"].encode()).hexdigest()
            )
            executed = self.call(state, "execute", "--claim", str(claim_path))
            self.assertEqual(executed.returncode, 0, executed.stderr)
            result = json.loads(executed.stdout)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["route"], "bounded-read")
            view = Path(result["view"]).read_text(encoding="utf-8")
            self.assertIn("one", view)
            shown = self.call(state, "show", "--id", task_id)
            projection = json.loads(shown.stdout)
            self.assertEqual(projection["task"]["status"], "complete")
            self.assertEqual(
                [row["kind"] for row in projection["events"]],
                ["queued", "leased", "execution-started", "execution-settled"],
            )

    def test_expired_lease_is_requeued_without_a_polling_sleep(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            state = work / "state"
            past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            deferred = self.call(
                state,
                "defer",
                "--at",
                past,
                "--cwd",
                str(work),
                "--",
                "true",
            )
            task_id = json.loads(deferred.stdout)["task_id"]
            claimed = self.call(
                state, "claim", "--worker", "first-worker", "--lease-seconds", "60"
            )
            self.assertTrue(json.loads(claimed.stdout)["claimed"])
            connection = sqlite3.connect(state / "broker.sqlite3")
            connection.execute(
                "UPDATE tasks SET lease_expires_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), task_id),
            )
            connection.commit()
            connection.close()
            due = self.call(state, "due")
            projection = json.loads(due.stdout)
            self.assertEqual(projection["reclaimed_expired_leases"], 1)
            self.assertEqual(projection["due"][0]["id"], task_id)
            reclaimed = self.call(
                state, "claim", "--worker", "second-worker", "--lease-seconds", "60"
            )
            self.assertTrue(json.loads(reclaimed.stdout)["claimed"])

    def test_deferred_effect_is_rejected_before_queue_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            state = work / "state"
            result = self.call(
                state,
                "defer",
                "--after",
                "1s",
                "--cwd",
                str(work),
                "--",
                "touch",
                "should-not-exist",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not mechanically executable", result.stderr)
            self.assertFalse((work / "should-not-exist").exists())
            if (state / "broker.sqlite3").exists():
                connection = sqlite3.connect(state / "broker.sqlite3")
                count = connection.execute("SELECT count(*) FROM tasks").fetchone()[0]
                connection.close()
            else:
                count = 0
            self.assertEqual(count, 0)

    def test_run_creates_private_default_artifacts_from_explicit_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            state = work / "state"
            result = self.call(
                state,
                "run",
                "--cwd",
                str(work),
                "--",
                sys.executable,
                "-c",
                "print('OK')",
                cwd=Path("/"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            projection = json.loads(result.stdout)
            self.assertTrue(projection["executed"])
            self.assertIn("OK", Path(projection["view"]).read_text(encoding="utf-8"))

    def test_observe_routes_raw_browser_read_to_semantic_delta(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            result = self.call(
                state,
                "observe",
                "--kind",
                "browser",
                "--operation",
                "domSnapshot",
                "--semantic-delta",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            projection = json.loads(result.stdout)
            self.assertEqual(projection["route"], "semantic-delta")
            self.assertFalse(projection["original_action_executable"])
            decision = json.loads(Path(projection["decision"]).read_text(encoding="utf-8"))
            self.assertFalse(decision["raw_content_emitted"])
            for name in ("request.json", "decision.json", "receipt.json"):
                path = Path(projection["decision"]).parent / name
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_tick_collapses_due_claim_and_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            state = work / "state"
            (work / "source.txt").write_text("bounded\n", encoding="utf-8")
            deferred = self.call(
                state,
                "defer",
                "--after",
                "0s",
                "--cwd",
                str(work),
                "--",
                "sed",
                "-n",
                "1p",
                "source.txt",
            )
            task_id = json.loads(deferred.stdout)["task_id"]
            tick = self.call(
                state,
                "tick",
                "--worker",
                "test-worker",
                "--lease-seconds",
                "60",
            )
            self.assertEqual(tick.returncode, 0, tick.stderr)
            report = json.loads(tick.stdout)
            self.assertEqual(report["processed"], 1)
            self.assertEqual(report["tasks"][0]["task_id"], task_id)
            self.assertEqual(report["tasks"][0]["status"], "complete")

    def test_verified_no_effect_requeues_at_an_exact_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            state = work / "state"
            deferred = self.call(
                state,
                "defer",
                "--after",
                "1h",
                "--cwd",
                str(work),
                "--",
                "true",
            )
            task_id = json.loads(deferred.stdout)["task_id"]
            settled = self.call(
                state,
                "settle",
                "--id",
                task_id,
                "--outcome",
                "verified-no-effect",
                "--after",
                "2m",
            )
            self.assertEqual(settled.returncode, 0, settled.stderr)
            projection = json.loads(settled.stdout)
            self.assertEqual(projection["status"], "queued")
            shown = json.loads(self.call(state, "show", "--id", task_id).stdout)
            self.assertEqual(shown["events"][-1]["kind"], "operator-settled")

    def test_status_is_a_content_free_queue_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            state = work / "state"
            self.call(
                state,
                "defer",
                "--after",
                "1h",
                "--cwd",
                str(work),
                "--",
                "true",
            )
            result = self.call(state, "status")
            self.assertEqual(result.returncode, 0, result.stderr)
            projection = json.loads(result.stdout)
            self.assertEqual(projection["counts"]["queued"], 1)
            self.assertEqual(projection["due"], 0)
            self.assertFalse(projection["raw_content_emitted"])

    def test_worker_lock_rejects_a_second_resident_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            first = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--state-root",
                    str(state),
                    "worker",
                    "--worker",
                    "first",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 3
            while not (state / "wake.sock").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((state / "wake.sock").exists())
            second = self.call(state, "worker", "--worker", "second")
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("another Lazy Commander worker", second.stderr)
            first.terminate()
            first.wait(timeout=3)

    def test_route_cli_persists_content_free_carrier_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            result = self.call(
                state,
                "route",
                "--task-ref",
                "task:research",
                "--kind",
                "research",
                "--urgency",
                "background",
                "--carrier-context",
                "--parallelizable",
                "--expected-chars",
                "30000",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            projection = json.loads(result.stdout)
            self.assertEqual(projection["route"], "chatgpt-carrier")
            self.assertTrue(projection["browser_message_requires_action_time_confirmation"])
            self.assertEqual(projection["campaign_summary"]["route"], "chatgpt-carrier")
            self.assertEqual(len(projection["campaign_summary"]["fingerprint"]), 64)
            self.assertFalse(projection["campaign_summary"]["authorizesDispatch"])
            decision = Path(projection["decision"])
            for name in ("request.json", "decision.json", "receipt.json"):
                self.assertEqual(stat.S_IMODE((decision.parent / name).stat().st_mode), 0o600)

    def test_plan_lanes_cli_does_not_execute_requested_parking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            inventory = root / "inventory.json"
            inventory.write_text(
                json.dumps(
                    {
                        "schema": "lazy-elatura-lane-inventory/v1",
                        "observedAt": "2026-08-31T00:00:00Z",
                        "lanes": [
                            {
                                "laneRef": f"lane:{index}",
                                "laneGeneration": 1,
                                "state": "responsive",
                                "activity": "idle",
                                "protected": False,
                                "blockers": [],
                                "lastUsedAt": f"2026-08-{index + 1:02d}T00:00:00Z",
                            }
                            for index in range(12)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = self.call(
                state,
                "plan-lanes",
                "--inventory",
                str(inventory),
                "--desired-new",
                "1",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            projection = json.loads(result.stdout)
            self.assertEqual(projection["outcome"], "park-before-admit")
            self.assertFalse(projection["admit_now"])
            self.assertEqual(projection["requested_lifecycle_actions"][0]["laneRef"], "lane:0")
            self.assertEqual(projection["campaign_summary"]["status"], "blocked")
            self.assertFalse(projection["campaign_summary"]["authorizesEffects"])

    def test_ambiguous_carrier_settlement_is_private_and_one_shot(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            routed = self.call(
                state,
                "route",
                "--task-ref",
                "task:research",
                "--kind",
                "research",
                "--urgency",
                "background",
                "--carrier-context",
                "--parallelizable",
                "--expected-chars",
                "30000",
            )
            run_id = json.loads(routed.stdout)["run_id"]
            settled = self.call(
                state,
                "settle-route",
                "--run-id",
                run_id,
                "--outcome",
                "ambiguous",
            )
            self.assertEqual(settled.returncode, 0, settled.stderr)
            projection = json.loads(settled.stdout)
            self.assertFalse(projection["retry_authorized"])
            self.assertTrue(projection["requires_reconciliation"])
            settlement = Path(projection["settlement"])
            self.assertEqual(stat.S_IMODE(settlement.stat().st_mode), 0o600)
            repeated = self.call(
                state,
                "settle-route",
                "--run-id",
                run_id,
                "--outcome",
                "verified-no-effect",
            )
            self.assertNotEqual(repeated.returncode, 0)
            self.assertIn("already settled", repeated.stderr)

    def test_worker_execution_settlement_is_private_and_one_shot(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            routed = self.call(
                state,
                "route",
                "--task-ref",
                "task:observation",
                "--kind",
                "verification",
                "--deterministic-observation",
                "--owner-query",
                "--expected-chars",
                "6000",
            )
            run_id = json.loads(routed.stdout)["run_id"]
            settled = self.call(
                state,
                "settle-worker",
                "--run-id",
                run_id,
                "--outcome",
                "committed",
                "--execution-ref",
                "execution:one",
                "--artifact-sha256",
                "a" * 64,
                "--artifact-bytes",
                "4159",
            )
            self.assertEqual(settled.returncode, 0, settled.stderr)
            projection = json.loads(settled.stdout)
            self.assertEqual(projection["next"], "owner-acceptance-check")
            settlement = Path(projection["settlement"])
            self.assertEqual(stat.S_IMODE(settlement.stat().st_mode), 0o600)
            repeated = self.call(
                state,
                "settle-worker",
                "--run-id",
                run_id,
                "--outcome",
                "ambiguous",
                "--execution-ref",
                "execution:one",
            )
            self.assertNotEqual(repeated.returncode, 0)
            self.assertIn("already settled", repeated.stderr)

    def test_plan_controls_keeps_fresh_primary_without_opening_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            inventory = root / "controls.json"
            inventory.write_text(
                json.dumps(
                    {
                        "schema": "lazy-control-service-inventory/v1",
                        "observedAt": "2026-08-31T00:10:00Z",
                        "browserFamily": "edge",
                        "services": [
                            {
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
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = self.call(
                state,
                "plan-controls",
                "--inventory",
                str(inventory),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            projection = json.loads(result.stdout)
            self.assertEqual(projection["outcome"], "keep-primary")
            self.assertFalse(projection["new_window_recommended"])
            self.assertEqual(projection["campaign_summary"]["status"], "ready")
            self.assertFalse(projection["campaign_summary"]["authorizesDispatch"])
            decision = Path(projection["decision"])
            for name in ("request.json", "decision.json", "receipt.json"):
                self.assertEqual(stat.S_IMODE((decision.parent / name).stat().st_mode), 0o600)

    def test_campaign_acceptance_reserves_once_and_rejects_stale_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            snapshot_path = root / "snapshot.json"
            snapshot = campaign.make_snapshot(
                routeDecision={
                    "ref": "route:codex",
                    "generation": 1,
                    "route": "codex-local",
                    "fingerprint": "0" * 64,
                    "authorizesWork": False,
                    "authorizesEffects": False,
                    "authorizesDispatch": False,
                },
                expectedInputChars=400,
            )
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            output = root / "compiled-1"
            compiled = self.call(
                state, "campaign", "--input", str(snapshot_path), "--output-dir", str(output)
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            view = json.loads((output / "view.json").read_text(encoding="utf-8"))
            accept_args = (
                "accept-campaign", "--view", str(output / "view.json"),
                "--owner-generation", "1",
                "--owner-fingerprint", view["snapshotSha256"],
                "--budget-ref", "budget:test",
                "--budget-generation", "1",
                "--budget-limit", "1000",
                "--budget-reserved", "0",
                "--budget-consumed", "0",
            )
            accepted = self.call(state, *accept_args)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(json.loads(accepted.stdout)["outcome"], "accepted")
            self.assertEqual(json.loads(accepted.stdout)["reserved_tokens_after"], 100)
            replay = self.call(state, *accept_args)
            self.assertEqual(replay.returncode, 0, replay.stderr)
            self.assertEqual(json.loads(replay.stdout)["outcome"], "replay")

            snapshot["step"]["generation"] = 2
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            output2 = root / "compiled-2"
            self.assertEqual(
                self.call(state, "campaign", "--input", str(snapshot_path), "--output-dir", str(output2)).returncode,
                0,
            )
            view2 = json.loads((output2 / "view.json").read_text(encoding="utf-8"))
            stale = self.call(
                state,
                "accept-campaign", "--view", str(output2 / "view.json"),
                "--owner-generation", "1",
                "--owner-fingerprint", view2["snapshotSha256"],
                "--budget-ref", "budget:test",
                "--budget-generation", "1",
                "--budget-limit", "1000",
                "--budget-reserved", "0",
                "--budget-consumed", "0",
            )
            self.assertNotEqual(stale.returncode, 0)
            self.assertIn("changed before campaign acceptance", stale.stderr)
            connection = sqlite3.connect(state / "broker.sqlite3")
            budget = connection.execute(
                "SELECT generation, reserved_tokens FROM campaign_budgets WHERE budget_ref='budget:test'"
            ).fetchone()
            count = connection.execute("SELECT COUNT(*) FROM campaign_acceptances").fetchone()[0]
            connection.close()
            self.assertEqual(budget, (2, 100))
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
