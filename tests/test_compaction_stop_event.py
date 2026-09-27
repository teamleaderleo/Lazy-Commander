from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "compaction_stop_event.py"
sys.path.insert(0, str(SCRIPT.parent))
from compaction_stop_event import checkpoint_from_stop, persist, project
from compaction_owner_checkpoint import load_checkpoint


def event(**overrides):
    value = {
        "session_id": "b5f6c1c2-1111-2222-3333-444455556666",
        "turn_id": "turn-123",
        "transcript_path": "/private/rollout.jsonl",
        "cwd": "/work/task",
        "hook_event_name": "Stop",
        "model": "gpt-5.6-sol",
        "permission_mode": "never",
        "stop_hook_active": False,
        "last_assistant_message": "secret final answer",
    }
    value.update(overrides)
    return value


class CompactionStopEventTests(unittest.TestCase):
    def test_receipt_is_content_free_and_never_authorizes_compaction(self):
        receipt = project(event())
        rendered = json.dumps(receipt)
        self.assertNotIn("secret final answer", rendered)
        self.assertNotIn("rollout.jsonl", rendered)
        self.assertFalse(receipt["idleProven"])
        self.assertFalse(receipt["authorizesCompaction"])
        self.assertFalse(receipt["rawConversationRetained"])

    def test_private_exact_replay_and_multiple_stops_in_one_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "events"
            path, replay = persist(project(event()), root)
            self.assertFalse(replay)
            self.assertEqual(os.stat(root).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertTrue(persist(project(event()), root)[1])
            changed, replay = persist(
                project(event(last_assistant_message="continued answer", stop_hook_active=True)),
                root,
            )
            self.assertFalse(replay)
            self.assertNotEqual(path, changed)
            self.assertEqual(len(list(root.glob("*.json"))), 2)

    def test_cli_emits_only_empty_stop_hook_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events"
            checkpoints = Path(tmp) / "checkpoints"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--state-root",
                    str(events),
                    "--checkpoint-state-root",
                    str(checkpoints),
                ],
                input=json.dumps(event()),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "{}\n")
            self.assertNotIn("secret final answer", result.stderr)
            self.assertEqual(len(list(events.glob("*.json"))), 1)
            checkpoint = load_checkpoint(checkpoints, event()["session_id"])
            self.assertEqual(checkpoint["checkpointGeneration"], 1)
            self.assertEqual(checkpoint["semanticBoundary"], "no")
            self.assertNotIn("secret final answer", json.dumps(checkpoint))

    def test_stop_checkpoint_generation_is_monotonic_and_exactly_replayable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = project(event())
            self.assertEqual(checkpoint_from_stop(first, root), (1, False))
            self.assertEqual(checkpoint_from_stop(first, root), (1, True))
            second = project(event(turn_id="turn-124", last_assistant_message="next secret"))
            self.assertEqual(checkpoint_from_stop(second, root), (2, False))
            self.assertEqual(load_checkpoint(root, event()["session_id"])["checkpointGeneration"], 2)
            rendered = "".join(path.read_text() for path in root.rglob("*.json"))
            self.assertNotIn("secret final answer", rendered)
            self.assertNotIn("next secret", rendered)

    def test_unknown_fields_and_nonopaque_identity_fail_closed(self):
        with self.assertRaises(ValueError):
            project(event(extra="no"))
        with self.assertRaises(ValueError):
            project(event(session_id="thread with spaces"))


if __name__ == "__main__":
    unittest.main()
