from __future__ import annotations

import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "compaction_activity_view.py"


class CompactionActivityViewTests(unittest.TestCase):
    def test_projects_compaction_metadata_without_bodies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout = root / "rollout.jsonl"
            output = root / "out"
            rows = [
                {"type": "session_meta", "payload": {"secret": "SECRET_PROMPT"}},
                {"timestamp": "2026-08-31T00:00:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 800}, "model_context_window": 1000}}},
                {"timestamp": "2026-08-31T00:01:00Z", "type": "compacted", "payload": {"window_number": 1, "replacement_history": [{"secret": "SECRET_SUMMARY"}]}},
                {"timestamp": "2026-08-31T00:01:01Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 0}, "model_context_window": 1000}}},
                {"timestamp": "2026-08-31T00:02:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 200}, "model_context_window": 1000}}},
            ]
            rollout.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(rollout), "--output-dir", str(output)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report_text = (output / "activity.json").read_text(encoding="utf-8")
            report = json.loads(report_text)
            row = report["compactions"]["rows"][0]
            self.assertEqual(row["beforeWindowPct"], 80.0)
            self.assertEqual(row["afterInputTokens"], 200)
            self.assertEqual(row["inputReductionPct"], 75.0)
            self.assertNotIn("SECRET_PROMPT", report_text)
            self.assertNotIn("SECRET_SUMMARY", report_text)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            for name in ("activity.json", "view.md", "receipt.json"):
                self.assertEqual(stat.S_IMODE((output / name).stat().st_mode), 0o600)

    def test_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout = root / "rollout.jsonl"
            output = root / "out"
            rollout.write_text("{}\n", encoding="utf-8")
            command = [sys.executable, str(SCRIPT), str(rollout), "--output-dir", str(output)]
            self.assertEqual(
                subprocess.run(command, check=False, capture_output=True).returncode,
                0,
            )
            second = subprocess.run(command, check=False, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("refusing to overwrite", second.stderr)


if __name__ == "__main__":
    unittest.main()
