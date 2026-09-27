from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from compaction_classifier import (
    build_card,
    normalize_classification,
    run_codex_classifier,
    validate_card,
)


class CompactionClassifierTests(unittest.TestCase):
    def checkpoint(self):
        return {
            "durableCheckpointReady": True,
            "nextActionPresent": True,
            "effectState": "settled",
            "boundaryEvidence": ["phase-complete", "same-task-family"],
        }

    def test_card_is_closed_and_contains_no_conversation(self):
        card = build_card(self.checkpoint(), checkpoint_fresh=True, goal_status="active")
        self.assertEqual(card["boundaryEvidence"], ["phase-complete", "same-task-family"])
        self.assertTrue(card["goalActive"])
        self.assertNotIn("message", json.dumps(card).lower())
        with self.assertRaises(ValueError):
            validate_card({**card, "boundaryEvidence": ["free-form prose"]})

    def test_runner_returns_closed_result_and_bounded_metrics(self):
        card = build_card(self.checkpoint(), checkpoint_fresh=True, goal_status="active")
        observed_command = []

        def fake_run(command, **kwargs):
            observed_command.extend(command)
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text('{"decision":"now","reason":"stable-boundary"}\n')
            kwargs["stdout"].write(
                b'not-json\n[]\n'
                b'{"type":"turn.completed","usage":{"input_tokens":8000,'
                b'"cached_input_tokens":7000,"cache_write_input_tokens":0,'
                b'"output_tokens":8,"reasoning_output_tokens":0}}\n'
            )
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as tmp, patch(
            "compaction_classifier.subprocess.run", side_effect=fake_run
        ):
            result, metrics = run_codex_classifier("codex", Path(tmp), card)
        self.assertEqual(result["decision"], "now")
        self.assertEqual(metrics["reportedTokensUsed"], 8_008)
        self.assertEqual(metrics["tokenUsage"]["input_tokens"], 8_000)
        self.assertEqual(metrics["tokenUsage"]["cached_input_tokens"], 7_000)
        self.assertFalse(metrics["rawConversationProvided"])
        self.assertEqual(len(metrics["cardSha256"]), 64)
        self.assertIn("--json", observed_command)
        disabled = [
            observed_command[index + 1]
            for index, value in enumerate(observed_command[:-1])
            if value == "--disable"
        ]
        self.assertEqual(disabled, ["apps", "plugins", "shell_tool", "image_generation"])

    def test_boundary_timing_is_derived_from_checkpoint_state(self):
        ready = build_card(self.checkpoint(), checkpoint_fresh=True, goal_status="active")
        missing = {
            **ready,
            "checkpointFresh": False,
            "durableCheckpointReady": False,
            "nextActionPresent": False,
        }
        raw_now = {"decision": "now", "reason": "stable-boundary"}
        raw_soon = {"decision": "soon", "reason": "checkpoint-first"}
        self.assertEqual(normalize_classification(ready, raw_soon)["decision"], "now")
        self.assertEqual(normalize_classification(missing, raw_now)["decision"], "soon")
        self.assertEqual(
            normalize_classification(
                missing, {"decision": "keep", "reason": "active-phase"}
            )["decision"],
            "keep",
        )


if __name__ == "__main__":
    unittest.main()
