from __future__ import annotations

import contextlib
import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from receipt_compare import compare
from semantic_command_hook import store


class ReceiptCompareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = mock.patch.dict(os.environ, {"LAZY_COMMAND_STATE_ROOT": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.sequence = 0

    def receipt(self, raw, *, code=0, command="gh pr checks 57", cwd=None, capped=False):
        self.sequence += 1
        identifier = store({
            "cwd": str(self.root), "tool_input": {"command": command},
            "session_id": "compare-tests", "turn_id": "one",
            "tool_use_id": str(self.sequence),
        }, command)
        directory = self.root / "state" / identifier
        request = json.loads((directory / "request.json").read_text())
        (directory / "raw.log").write_bytes(raw)
        (directory / "receipt.json").write_text(json.dumps({
            "schema": "lazy-semantic-command-receipt/v1", "id": identifier,
            "request_sha256": request["request_sha256"],
            "cwd": cwd or str(self.root), "exit_code": code, "output_limit_reached": capped,
        }))
        return identifier

    def output(self, before, after, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(compare(before, after, **kwargs), 0)
        return stream.getvalue()

    def test_identical_snapshots_keep_identity_and_exit_transition(self):
        first = self.receipt(b"verify pending https://example.test/job/123\n", code=8)
        second = self.receipt(b"verify pending https://example.test/job/123\n", code=0)
        output = self.output(first, second)
        self.assertIn(f"compare {first} -> {second}", output)
        self.assertIn("exit 8 -> 0", output)
        self.assertIn("output unchanged", output)
        self.assertNotIn("verify pending", output)
        self.assertEqual(len(output.splitlines()), 1)

    def test_changed_job_shows_old_new_locations_and_context(self):
        first = self.receipt(b"build pass job/111\nverify pending job/222\nscan pass job/333\n", code=8)
        second = self.receipt(b"build pass job/111\nverify failed job/222\nscan pass job/333\n", code=1)
        output = self.output(first, second)
        for required in ("@@ -1,3 +1,3 @@", "-verify pending job/222", "+verify failed job/222",
                         " build pass job/111", " scan pass job/333", "exit 8 -> 1"):
            self.assertIn(required, output)

    def test_different_command_or_directory_refuses(self):
        first = self.receipt(b"same")
        for second in (self.receipt(b"same", command="gh pr checks 58"),
                       self.receipt(b"same", cwd="/different")):
            with self.assertRaisesRegex(SystemExit, "same command and execution directory"):
                self.output(first, second)

    def test_incomplete_receipt_and_capture_refuse(self):
        first = self.receipt(b"first")
        second = self.receipt(b"second", capped=True)
        with self.assertRaisesRegex(SystemExit, "incomplete"):
            self.output(first, second)
        (self.root / "state" / second / "receipt.json").unlink()
        with self.assertRaisesRegex(SystemExit, "settled, valid receipts"):
            self.output(first, second)

    def test_terminal_and_encoding_differences_are_not_claimed_identical(self):
        first = self.receipt(b"\x1b[31mfailed\x1b[0m\n")
        second = self.receipt(b"failed\n")
        output = self.output(first, second)
        self.assertIn("raw bytes changed; terminal-normalized text is unchanged", output)
        self.assertNotIn("output unchanged", output)

    def test_long_line_is_explicitly_withheld_with_raw_recovery(self):
        first = self.receipt(b"old\n")
        second = self.receipt(b"IDENTITY-" + b"x" * 9000 + b"-END\n")
        output = self.output(first, second)
        self.assertLessEqual(len(output), 7000)
        self.assertIn("diff truncated at a complete line", output)
        self.assertNotIn("+IDENTITY", output)
        self.assertIn(f"lc s -r {second}", output)

    def test_large_diff_is_bounded_before_sequence_matching(self):
        first = self.receipt(b"x" * 256_001)
        second = self.receipt(b"new\n")
        with mock.patch("receipt_compare.difflib.unified_diff", side_effect=AssertionError("too large")):
            self.assertIn("capture exceeds 256000 bytes", self.output(first, second))
        many = self.receipt(b"x\n" * 2_001)
        self.assertIn("capture exceeds 2000 lines", self.output(many, second))

    def test_cli_never_executes_command_and_validates_context(self):
        marker = self.root / "must-not-exist"
        command = f"touch {marker}"
        first = self.receipt(b"before\n", command=command)
        second = self.receipt(b"after\n", command=command)
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "semantic_command.py"), "compare", first, second],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-before", result.stdout)
        self.assertIn("+after", result.stdout)
        self.assertFalse(marker.exists())
        with self.assertRaisesRegex(SystemExit, "context must"):
            self.output(first, second, context=-1)

    def test_since_executes_once_returns_actual_status_and_keeps_full_view(self):
        marker = self.root / "executions"
        data = self.root / "snapshot"
        data.write_text("verify pending job/222\n")
        command = f"printf x >> {marker}; cat {data}; exit 8"
        prefix = [sys.executable, str(SCRIPTS / "semantic_command.py")]
        first = subprocess.run(prefix + [command], text=True, capture_output=True)
        self.assertEqual(first.returncode, 8, first.stderr)
        baseline = re.search(r"id ([0-9a-f]{16})", first.stdout).group(1)
        second = subprocess.run(prefix + ["--since", baseline, command], text=True, capture_output=True)
        self.assertEqual(second.returncode, 8, second.stderr)
        self.assertEqual(len(second.stdout.splitlines()), 1)
        self.assertIn("exit 8 -> 8", second.stdout)
        self.assertIn("output unchanged", second.stdout)
        new_id = re.search(r"-> ([0-9a-f]{16})", second.stdout).group(1)
        stored = (self.root / "state" / new_id / "view.txt").read_text()
        self.assertIn("verify pending job/222", stored)
        self.assertEqual(marker.read_text(), "xx")
        refused = subprocess.run(prefix + ["--since", baseline, command + " "], text=True, capture_output=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("same command", refused.stderr)
        self.assertEqual(marker.read_text(), "xx")

    def test_since_rejects_missing_baseline_before_effect_and_raw_conflict(self):
        marker = self.root / "must-not-run"
        prefix = [sys.executable, str(SCRIPTS / "semantic_command.py")]
        for arguments in (["--since", "0" * 16], ["--since", "0" * 16, "-r"]):
            result = subprocess.run(prefix + arguments + [f"touch {marker}"], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())

    def test_corrupt_baseline_is_rejected_before_command_execution(self):
        marker = self.root / "must-not-run"
        command = f"touch {marker}"
        baseline = self.receipt(b"before", command=command, cwd=os.getcwd())
        path = self.root / "state" / baseline / "receipt.json"
        valid = json.loads(path.read_text())
        variants = [[], {**valid, "id": "wrong"}, {**valid, "exit_code": "0"},
                    {key: value for key, value in valid.items() if key != "exit_code"}]
        for malformed in variants:
            path.write_text(json.dumps(malformed))
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "semantic_command.py"), "--since", baseline, command],
                text=True, capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("valid receipts", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
