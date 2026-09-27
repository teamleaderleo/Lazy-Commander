from __future__ import annotations

import unittest
from pathlib import Path
import json
import subprocess
import sys
import tempfile


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from compaction_advisor import advance_pending, advise, resolve_classification


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compaction_advisor.py"


def state(**overrides):
    value = {
        "schema": "lazy-compaction-checkpoint/v1",
        "threadRef": "thread:one",
        "usedTokens": 130_000,
        "contextWindow": 258_400,
        "checkpointGeneration": 2,
        "lastCompactedGeneration": 1,
        "durableCheckpointReady": True,
        "semanticBoundary": "yes",
        "effectState": "settled",
    }
    value.update(overrides)
    return value


class CompactionAdvisorTests(unittest.TestCase):
    def test_boundary_compacts_even_below_half_window(self):
        decision = advise(state(usedTokens=20_000))
        self.assertEqual(decision["action"], "compact")
        self.assertTrue(decision["authorizesCompaction"])

    def test_uncertain_boundary_asks_cheap_classifier_even_below_half(self):
        decision = advise(
            state(
                usedTokens=20_000,
                semanticBoundary="uncertain",
                durableCheckpointReady=False,
                checkpointGeneration=1,
            )
        )
        self.assertEqual(decision["action"], "classify-boundary")
        self.assertEqual(decision["classifierContract"]["answers"], ["now", "soon", "keep"])

    def test_low_usage_without_boundary_continues(self):
        decision = advise(state(usedTokens=20_000, semanticBoundary="no"))
        self.assertEqual(decision["action"], "continue")
        self.assertIsNone(decision["classifierContract"])

    def test_uncertain_boundary_gets_tiny_classifier_contract(self):
        decision = advise(state(semanticBoundary="uncertain"))
        self.assertEqual(decision["action"], "classify-boundary")
        self.assertEqual(decision["classifierContract"]["answers"], ["now", "soon", "keep"])
        self.assertFalse(decision["rawConversationRequired"])

    def test_low_usage_boundary_checkpoints_then_compacts(self):
        missing = advise(
            state(
                usedTokens=20_000,
                durableCheckpointReady=False,
                checkpointGeneration=1,
            )
        )
        ready = advise(state(usedTokens=20_000, checkpointGeneration=2))
        self.assertEqual(missing["action"], "checkpoint-now")
        self.assertEqual(ready["action"], "compact")

    def test_soon_becomes_now_after_checkpoint(self):
        missing = state(
            usedTokens=20_000,
            durableCheckpointReady=False,
            semanticBoundary="uncertain",
            checkpointGeneration=1,
        )
        resolution = resolve_classification(
            missing,
            {"decision": "soon", "reason": "checkpoint-first"},
        )
        self.assertEqual(resolution["action"], "checkpoint-now")
        self.assertEqual(resolution["pending"]["afterCheckpointGeneration"], 1)

        ready = state(usedTokens=20_000, checkpointGeneration=2)
        advanced = advance_pending(ready, resolution["pending"])
        self.assertEqual(advanced["action"], "compact")
        self.assertIsNone(advanced["pending"])
        self.assertTrue(advanced["authorizesCompaction"])

    def test_keep_does_not_create_pending_work(self):
        decision = resolve_classification(
            state(semanticBoundary="uncertain"),
            {"decision": "keep", "reason": "active-phase"},
        )
        self.assertEqual(decision["action"], "continue")
        self.assertIsNone(decision["pending"])

    def test_pressure_requires_checkpoint_then_compacts(self):
        missing = advise(state(usedTokens=190_000, durableCheckpointReady=False))
        ready = advise(state(usedTokens=190_000, semanticBoundary="no"))
        self.assertEqual(missing["action"], "checkpoint-now")
        self.assertEqual(ready["action"], "compact")

    def test_same_checkpoint_generation_is_not_reused(self):
        decision = advise(state(usedTokens=190_000, checkpointGeneration=1))
        self.assertEqual(decision["action"], "checkpoint-now")

    def test_ambiguous_effect_wins_over_pressure(self):
        decision = advise(state(usedTokens=250_000, effectState="ambiguous"))
        self.assertEqual(decision["action"], "reconcile-effect")
        self.assertFalse(decision["authorizesCompaction"])

    def test_rejects_nonopaque_identity_and_backwards_generation(self):
        with self.assertRaises(ValueError):
            advise(state(threadRef="private thread name with spaces"))
        with self.assertRaises(ValueError):
            advise(state(checkpointGeneration=1, lastCompactedGeneration=2))

    def test_cli_serializes_classifier_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = Path(tmp) / "request.json"
            request.write_text(json.dumps(state(semanticBoundary="uncertain")))
            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(request)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            parsed = json.loads(result.stdout)
            self.assertEqual(parsed["classifierContract"]["answers"], ["now", "soon", "keep"])


if __name__ == "__main__":
    unittest.main()
