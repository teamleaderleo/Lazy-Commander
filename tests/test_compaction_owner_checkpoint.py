from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from compaction_owner_checkpoint import load_checkpoint, record


def checkpoint(**overrides):
    value = {
        "schema": "lazy-compaction-owner-checkpoint/v1",
        "threadRef": "root-one",
        "checkpointGeneration": 1,
        "durableCheckpointReady": True,
        "nextActionPresent": True,
        "semanticBoundary": "yes",
        "effectState": "settled",
    }
    value.update(overrides)
    return value


class CompactionOwnerCheckpointTests(unittest.TestCase):
    def test_private_monotonic_record_and_exact_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, replay = record(checkpoint(), root)
            self.assertFalse(replay)
            self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertTrue(record(checkpoint(), root)[1])
            record(checkpoint(checkpointGeneration=2), root)
            self.assertEqual(load_checkpoint(root, "root-one")["checkpointGeneration"], 2)
            with self.assertRaisesRegex(ValueError, "backwards"):
                record(checkpoint(checkpointGeneration=1), root)

    def test_same_generation_cannot_change_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record(checkpoint(), root)
            with self.assertRaisesRegex(ValueError, "changed content"):
                record(checkpoint(semanticBoundary="no"), root)


if __name__ == "__main__":
    unittest.main()
