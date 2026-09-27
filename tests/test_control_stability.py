from __future__ import annotations

import json
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "control_stability.py"


def scope(*, lane="lane:edge-primary", generation=7):
    return {
        "browserFamily": "edge",
        "surfaceProfile": "chatgpt-project-v1",
        "projectRef": "project:lazy-legion-lab",
        "laneRef": lane,
        "laneGeneration": generation,
        "controlInventorySha256": "a" * 64,
        "probeVersion": "carrier-probe-v1",
    }


def observation(event_id: str, observed_at: str, *, generation: int = 3, success: bool = True):
    return {
        "schema": "lazy-control-stability-observation/v2",
        "eventId": event_id,
        "controlRef": "edge-control:primary",
        "controlGeneration": generation,
        "observedAt": observed_at,
        "scope": scope(),
        "capabilities": {
            "inventoryReadable": success,
            "projectSurfaceIdentified": success,
            "composerWritable": success,
            "sendControlIdentified": success,
        },
    }


class ControlStabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, *args, success=True):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--state-root", str(self.state), *args],
            check=False, capture_output=True, text=True,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout, json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def record(self, value):
        source = self.root / f"{value['eventId']}.json"
        source.write_text(json.dumps(value), encoding="utf-8")
        return self.run_cli("record", "--observation", str(source))

    def status(self, evaluated_at: str, *, generation: int = 3):
        scope_path = self.root / "scope.json"
        scope_path.write_text(json.dumps(scope()), encoding="utf-8")
        return self.run_cli(
            "status", "--control-ref", "edge-control:primary",
            "--control-generation", str(generation), "--evaluated-at", evaluated_at,
            "--scope", str(scope_path),
        )[1]

    def test_three_distinct_successes_spanning_time_become_stable(self):
        first = self.record(observation("event-1", "2026-08-31T00:00:00Z"))[1]
        self.assertEqual(first["status"], "wait")
        self.record(observation("event-2", "2026-08-31T00:00:30Z"))
        third = self.record(observation("event-3", "2026-08-31T00:01:00Z"))[1]
        self.assertEqual(third["status"], "stable")
        self.assertEqual(third["consecutiveSuccesses"], 3)
        self.assertEqual(third["successSpanSeconds"], 60)
        self.assertEqual(third["wakeAt"], "2026-08-31T00:06:00+00:00")
        self.assertFalse(third["rawContentEmitted"])
        self.assertFalse(third["authorizesEffects"])

    def test_exact_replay_is_byte_stable_and_changed_event_conflicts(self):
        value = observation("event-1", "2026-08-31T00:00:00Z")
        first_text, _ = self.record(value)
        second_text, _ = self.record(value)
        self.assertEqual(first_text, second_text)
        changed = observation("event-1", "2026-08-31T00:00:01Z")
        source = self.root / "changed.json"
        source.write_text(json.dumps(changed), encoding="utf-8")
        refused = self.run_cli("record", "--observation", str(source), success=False)
        self.assertIn("changed stable binding", refused.stderr)

    def test_failure_resets_window_and_requests_rotation(self):
        self.record(observation("event-1", "2026-08-31T00:00:00Z"))
        self.record(observation("event-2", "2026-08-31T00:00:30Z"))
        failed = self.record(
            observation("event-3", "2026-08-31T00:00:45Z", success=False)
        )[1]
        self.assertEqual(failed["status"], "rotate")
        self.assertEqual(failed["reasonCode"], "latest-capability-observation-failed")
        recovered = self.record(observation("event-4", "2026-08-31T00:01:00Z"))[1]
        self.assertEqual(recovered["status"], "wait")
        self.assertEqual(recovered["consecutiveSuccesses"], 1)

    def test_stable_evidence_expires_at_exact_deadline(self):
        self.record(observation("event-1", "2026-08-31T00:00:00Z"))
        self.record(observation("event-2", "2026-08-31T00:00:30Z"))
        self.record(observation("event-3", "2026-08-31T00:01:00Z"))
        at_deadline = self.status("2026-08-31T00:06:00Z")
        self.assertEqual(at_deadline["status"], "stable")
        expired = self.status("2026-08-31T00:06:01Z")
        self.assertEqual(expired["status"], "rotate")
        self.assertEqual(expired["reasonCode"], "capability-evidence-expired")

    def test_generation_isolation_and_private_state(self):
        self.record(observation("event-1", "2026-08-31T00:01:00Z"))
        self.assertEqual(self.status("2026-08-31T00:01:00Z", generation=4)["observations"], 0)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.state / "control-stability.sqlite3").stat().st_mode), 0o600
        )

    def test_out_of_order_event_is_refused(self):
        self.record(observation("event-1", "2026-08-31T00:01:00Z"))
        value = observation("event-2", "2026-08-31T00:00:59Z")
        source = self.root / "older.json"
        source.write_text(json.dumps(value), encoding="utf-8")
        refused = self.run_cli("record", "--observation", str(source), success=False)
        self.assertIn("increasing time order", refused.stderr)

    def test_extra_or_textual_capability_content_is_refused(self):
        value = observation("event-1", "2026-08-31T00:00:00Z")
        value["capabilities"]["detail"] = "raw selector output"
        source = self.root / "extra.json"
        source.write_text(json.dumps(value), encoding="utf-8")
        refused = self.run_cli("record", "--observation", str(source), success=False)
        self.assertIn("capability fields are not exact", refused.stderr)

    def test_status_is_durably_stored_and_private_output_is_one_shot(self):
        self.record(observation("event-1", "2026-08-31T00:00:00Z"))
        scope_path = self.root / "scope-status.json"
        scope_path.write_text(json.dumps(scope()), encoding="utf-8")
        output = self.root / "status.json"
        _, status = self.run_cli(
            "status", "--control-ref", "edge-control:primary",
            "--control-generation", "3", "--evaluated-at", "2026-08-31T00:00:00Z",
            "--scope", str(scope_path), "--output", str(output),
        )
        self.assertEqual(json.loads(output.read_text()), status)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        refused = self.run_cli(
            "status", "--control-ref", "edge-control:primary",
            "--control-generation", "3", "--evaluated-at", "2026-08-31T00:00:00Z",
            "--scope", str(scope_path), "--output", str(output), success=False,
        )
        self.assertIn("File exists", refused.stderr)

    def test_scope_isolation_and_policy_downgrade_refusal(self):
        value = observation("event-1", "2026-08-31T00:00:00Z")
        self.record(value)
        other_scope = self.root / "other-scope.json"
        other_scope.write_text(json.dumps(scope(lane="lane:other")), encoding="utf-8")
        isolated = self.run_cli(
            "status", "--control-ref", "edge-control:primary",
            "--control-generation", "3", "--evaluated-at", "2026-08-31T00:00:00Z",
            "--scope", str(other_scope),
        )[1]
        self.assertEqual(isolated["observations"], 0)
        source = self.root / "policy.json"
        source.write_text(json.dumps(observation("event-2", "2026-08-31T00:00:30Z")), encoding="utf-8")
        refused = self.run_cli(
            "record", "--observation", str(source), "--required-successes", "1",
            success=False,
        )
        self.assertIn("unrecognized arguments", refused.stderr)

    def test_future_observation_is_refused(self):
        future = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        source = self.root / "future.json"
        source.write_text(json.dumps(observation("event-future", future)), encoding="utf-8")
        refused = self.run_cli("record", "--observation", str(source), success=False)
        self.assertIn("too far in the future", refused.stderr)


if __name__ == "__main__":
    unittest.main()
