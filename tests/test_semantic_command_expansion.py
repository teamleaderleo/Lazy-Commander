from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"
RUNNER = SCRIPTS / "semantic_command.py"
sys.path.insert(0, str(SCRIPTS))
from semantic_command import LINE_NORMALIZATION_VERSION, focused_line_count  # noqa: E402
from semantic_command_hook import store as store_request  # noqa: E402


class SemanticCommandExpansionTests(unittest.TestCase):
    def test_dense_and_cross_chunk_literal_counts_match_raw_bytes(self):
        from semantic_command import stored_lines

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.log"
            for needle, raw in (
                ("a", b"a" * 196_609),
                ("aaa", b"a" * 196_609),
                ("abb", b"a" * 65_535 + b"bb" + b"abb" * 30_000),
                ("雪b", b"x" + "雪b".encode() * 30_000),
            ):
                path.write_bytes(raw)
                records = list(stored_lines(path, needle))
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0][4], raw.count(needle.encode()))

    def store_raw(
        self, root: Path, raw: bytes, command: str = "printf captured", settled: bool = True
    ) -> str:
        payload = {
            "cwd": str(root),
            "tool_input": {"command": command},
            "session_id": "expansion-session",
            "turn_id": "expansion-turn",
            "tool_use_id": "expansion-tool",
        }
        with mock.patch.dict(os.environ, {"LAZY_COMMAND_STATE_ROOT": str(root / "state")}):
            identifier = store_request(payload, command)
        run_dir = root / "state" / identifier
        (run_dir / "raw.log").write_bytes(raw)
        if settled:
            request = json.loads((run_dir / "request.json").read_text())
            (run_dir / "receipt.json").write_text(json.dumps({
                "schema": "lazy-semantic-command-receipt/v1",
                "id": identifier,
                "request_sha256": request["request_sha256"],
                "output_bytes": len(raw),
                "output_lines": len(raw.decode("utf-8", "replace").splitlines()),
                "line_normalization_version": LINE_NORMALIZATION_VERSION,
            }))
        return identifier

    def show(self, root: Path, identifier: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RUNNER), "s", identifier, *arguments],
            text=True,
            capture_output=True,
            env={**os.environ, "LAZY_COMMAND_STATE_ROOT": str(root / "state")},
            check=False,
        )

    def test_line_range_recovers_distant_traceback_with_original_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [f"ordinary line {number}" for number in range(1, 181)]
            rows[149:155] = [
                "Traceback (most recent call last):",
                '  File "/srv/app/worker.py", line 81, in run',
                "    response = execute(job)   ",
                "",
                "RuntimeError: distant failure",
                "context after failure",
            ]
            identifier = self.store_raw(root, ("\n".join(rows) + "\n").encode())

            result = self.show(root, identifier, "--lines", "148:157")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("lines 148:157 · showing 10 of 10 requested · 180 captured", result.stdout)
            self.assertIn("150 | Traceback (most recent call last):", result.stdout)
            self.assertIn('151 |   File "/srv/app/worker.py", line 81, in run', result.stdout)
            self.assertIn("152 |     response = execute(job)   \n", result.stdout)
            self.assertIn("153 | \n", result.stdout)
            self.assertIn("154 | RuntimeError: distant failure", result.stdout)
            self.assertLessEqual(len(result.stdout.rstrip("\n")), 7_000)

    def test_line_numbers_follow_carriage_return_projection_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identifier = self.store_raw(
                root, "step one\rstep two\r\nstep three\u2028step four\u2029final\n".encode()
            )

            result = self.show(root, identifier, "--lines", "1:5")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("1 | step one\n", result.stdout)
            self.assertIn("2 | step two\n", result.stdout)
            self.assertIn("3 | step three\n", result.stdout)
            self.assertIn("4 | step four\n", result.stdout)
            self.assertIn("5 | final\n", result.stdout)

    def test_legacy_receipt_recounts_lines_without_mutating_the_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identifier = self.store_raw(root, b"one\rtwo\rthree\rfour\r")
            receipt_path = root / "state" / identifier / "receipt.json"
            receipt = json.loads(receipt_path.read_text())
            receipt.pop("line_normalization_version", None)
            receipt["output_lines"] = 2
            receipt_path.write_text(json.dumps(receipt))
            before = receipt_path.read_bytes()

            result = self.show(root, identifier, "--lines", "3:4")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("showing 2 of 2 requested · 4 captured", result.stdout)
            self.assertIn("3 | three\n", result.stdout)
            self.assertIn("4 | four\n", result.stdout)
            self.assertEqual(receipt_path.read_bytes(), before)

    def test_current_line_count_version_uses_receipt_fast_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.log"
            path.write_bytes(b"one\ntwo\ntrailing data that need not be counted\n")
            receipt = {
                "line_normalization_version": LINE_NORMALIZATION_VERSION,
                "output_lines": 3,
            }
            with mock.patch(
                "semantic_command.stored_lines",
                side_effect=AssertionError("current receipt must not be recounted"),
            ):
                self.assertEqual(focused_line_count(path, receipt), 3)

    def test_literal_search_merges_overlapping_context_and_counts_occurrences(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [f"line-{number}" for number in range(1, 21)]
            rows[9] = "first NEEDLE match"
            rows[11] = "NEEDLE twice NEEDLE"
            identifier = self.store_raw(root, ("\n".join(rows) + "\n").encode())

            result = self.show(root, identifier, "--find", "NEEDLE", "--context", "2")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("3 matches on 2 lines", result.stdout)
            self.assertIn("showing 7 of 7 context lines · 20 captured · context 2", result.stdout)
            for number in range(8, 15):
                self.assertEqual(result.stdout.count(f"{number:>2} | "), 1)
            self.assertNotIn("matches not shown", result.stdout)

    def test_literal_search_handles_regex_metacharacters_and_no_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identifier = self.store_raw(root, b"literal a+b[0] here\nregex aaab0 does not count\n")

            literal = self.show(root, identifier, "--find", "a+b[0]", "--context", "0")
            missing = self.show(root, identifier, "--find", "a.*b", "--context", "0")

            self.assertEqual(literal.returncode, 0, literal.stderr)
            self.assertIn("1 matches on 1 lines", literal.stdout)
            self.assertIn("1 | literal a+b[0] here", literal.stdout)
            self.assertEqual(missing.returncode, 0, missing.stderr)
            self.assertIn("0 matches on 0 lines", missing.stdout)
            self.assertNotIn(" | ", missing.stdout)

    def test_search_over_budget_reports_remaining_matches_and_narrowing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [f"NEEDLE match-{number:03d} " + "x" * 90 for number in range(1, 101)]
            identifier = self.store_raw(root, ("\n".join(rows) + "\n").encode())

            result = self.show(root, identifier, "--find", "NEEDLE", "--context", "0")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("100 matches on 100 lines", result.stdout)
            self.assertRegex(result.stdout, r"\[… \d+ matches not shown …\]")
            self.assertRegex(result.stdout, rf"narrow: lc s {identifier} --lines \d+:\d+")
            self.assertLessEqual(len(result.stdout.rstrip("\n")), 7_000)
            for line in result.stdout.splitlines():
                if " | NEEDLE " in line:
                    self.assertTrue(line.endswith("x" * 90))

    def test_search_reports_context_omission_after_showing_every_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = ["NEEDLE only match", *("context " + "c" * 100 for _ in range(100))]
            identifier = self.store_raw(root, ("\n".join(rows) + "\n").encode())

            result = self.show(root, identifier, "--find", "NEEDLE", "--context", "100")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("matches not shown", result.stdout)
            self.assertRegex(result.stdout, r"\[… \d+ selected context lines not shown …\]")
            self.assertRegex(result.stdout, rf"narrow: lc s {identifier} --lines \d+:\d+")
            self.assertNotIn("--lines 1:101", result.stdout)
            self.assertLessEqual(len(result.stdout.rstrip("\n")), 7_000)

    def test_line_range_over_budget_reports_gap_and_narrowing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [f"range-line-{number:03d} " + "q" * 90 for number in range(1, 101)]
            identifier = self.store_raw(root, ("\n".join(rows) + "\n").encode())

            result = self.show(root, identifier, "--lines", "1:100")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r"\[… \d+ requested lines not shown …\]")
            self.assertRegex(result.stdout, rf"narrow: lc s {identifier} --lines \d+:\d+")
            self.assertLessEqual(len(result.stdout.rstrip("\n")), 7_000)
            for line in result.stdout.splitlines():
                if " | range-line-" in line:
                    self.assertTrue(line.endswith("q" * 90))

    def test_huge_matching_line_points_to_raw_byte_page(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            huge = "z" * 65_528 + "NEEDLE" + "z" * 5_000
            identifier = self.store_raw(root, f"head\n{huge}\ntail\n".encode())

            result = self.show(root, identifier, "--find", "NEEDLE", "--context", "0")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"line 2 is {len(huge.encode()) + 1} bytes", result.stdout)
            self.assertIn(
                f"lc s -r {identifier} --offset 5 --limit 7000", result.stdout
            )
            self.assertIn("1 matches not shown", result.stdout)
            self.assertNotIn("z" * 100, result.stdout)
            self.assertLessEqual(len(result.stdout.rstrip("\n")), 7_000)

    def test_invalid_focused_options_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identifier = self.store_raw(root, b"one\ntwo\n")
            cases = [
                (("--lines", "0:2"), "positive 1-based"),
                (("--lines", "2:1"), "START must not exceed END"),
                (("--lines", "bad"), "START:END"),
                (("--lines", "1:2", "--find", "one"), "mutually exclusive"),
                (("--raw", "--lines", "1:2"), "mutually exclusive"),
                (("--find", "one", "--offset", "0"), "cannot be combined"),
                (("--context", "2"), "requires --find"),
                (("--find", ""), "must not be empty"),
                (("--find", "one\ntwo"), "single line"),
                (("--find", "one\u2028two"), "single line"),
                (("--find", "one", "--context", "-1"), "between 0 and 100"),
                (("--find", "one", "--context", "101"), "between 0 and 100"),
            ]
            for arguments, message in cases:
                with self.subTest(arguments=arguments):
                    result = self.show(root, identifier, *arguments)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(message, result.stderr)

    def test_focused_reads_require_a_settled_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identifier = self.store_raw(root, b"partial output\n", settled=False)

            focused = self.show(root, identifier, "--lines", "1:1")
            raw = self.show(root, identifier, "--raw", "--limit", "20")

            self.assertNotEqual(focused.returncode, 0)
            self.assertIn("requires a settled semantic-command receipt", focused.stderr)
            self.assertEqual(raw.returncode, 0, raw.stderr)
            self.assertEqual(raw.stdout, "partial output\n")

            receipt_path = root / "state" / identifier / "receipt.json"
            for receipt in ([], {"schema": "lazy-semantic-command-receipt/v1", "id": identifier}):
                with self.subTest(receipt=receipt):
                    receipt_path.write_text(json.dumps(receipt))
                    invalid = self.show(root, identifier, "--find", "output")
                    self.assertNotEqual(invalid.returncode, 0)
                    self.assertIn("receipt does not match", invalid.stderr)

    def test_focused_reads_never_rerun_the_stored_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "runs.log"
            command = (
                f"printf 'run\\n' >> {shlex.quote(str(marker))}; "
                "printf 'alpha\\nNEEDLE result\\nomega\\n'"
            )
            payload = {
                "cwd": str(root),
                "tool_input": {"command": command},
                "session_id": "no-rerun-session",
                "turn_id": "no-rerun-turn",
                "tool_use_id": "no-rerun-tool",
            }
            with mock.patch.dict(os.environ, {"LAZY_COMMAND_STATE_ROOT": str(root / "state")}):
                identifier = store_request(payload, command)
            environment = {**os.environ, "LAZY_COMMAND_STATE_ROOT": str(root / "state")}
            run = subprocess.run(
                [sys.executable, str(RUNNER), "run", identifier],
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(marker.read_text(), "run\n")
            receipt = json.loads(
                (root / "state" / identifier / "receipt.json").read_text()
            )
            self.assertEqual(
                receipt["line_normalization_version"], LINE_NORMALIZATION_VERSION
            )

            found = self.show(root, identifier, "--find", "NEEDLE")
            lines = self.show(root, identifier, "--lines", "1:3")

            self.assertEqual(found.returncode, 0, found.stderr)
            self.assertEqual(lines.returncode, 0, lines.stderr)
            self.assertIn("context 3", found.stdout)
            self.assertEqual(marker.read_text(), "run\n")


if __name__ == "__main__":
    unittest.main()
