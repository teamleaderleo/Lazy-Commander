from __future__ import annotations

import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "bounded_command.py"


class BoundedCommandTests(unittest.TestCase):
    def test_large_output_becomes_small_signal_and_tail_view(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            program = (
                "for i in range(200): print(f'ordinary noise {i}')\n"
                "print('WARNING one useful diagnostic')\n"
                "print('Tests 12 passed successfully')\n"
                "print('OK')\n"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output-dir",
                    str(output),
                    "--tail-lines",
                    "3",
                    "--",
                    sys.executable,
                    "-c",
                    program,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0)
            receipt = json.loads((output / "receipt.json").read_text(encoding="utf-8"))
            view = (output / "view.txt").read_text(encoding="utf-8")
            raw = (output / "raw.log").read_text(encoding="utf-8")
            self.assertEqual(receipt["output_lines"], 203)
            self.assertGreater(receipt["omitted_lines"], 190)
            self.assertIn("WARNING one useful diagnostic", view)
            self.assertIn("Tests 12 passed successfully", view)
            self.assertIn("OK", view)
            self.assertNotIn("ordinary noise 0\n", view)
            self.assertIn("ordinary noise 0", raw)
            for name in ("receipt.json", "view.txt", "raw.log"):
                self.assertEqual(stat.S_IMODE((output / name).stat().st_mode), 0o600)

    def test_underlying_failure_code_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output-dir",
                    str(output),
                    "--",
                    sys.executable,
                    "-c",
                    "print('ERROR deterministic failure'); raise SystemExit(7)",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            receipt = json.loads((output / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(result.returncode, 7)
            self.assertEqual(receipt["exit_code"], 7)
            self.assertIn(
                "ERROR deterministic failure",
                (output / "view.txt").read_text(encoding="utf-8"),
            )

    def test_assertion_detail_is_retained_without_raw_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output-dir",
                    str(output),
                    "--tail-lines",
                    "1",
                    "--",
                    sys.executable,
                    "-c",
                    "print('ordinary noise\\n' * 20, end=''); "
                    "print('AssertionError: expected 2, actual 0'); raise SystemExit(1)",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1)
            view = (output / "view.txt").read_text(encoding="utf-8")
            self.assertIn("AssertionError: expected 2, actual 0", view)
            self.assertNotIn("ordinary noise 0", view)

    def test_prefix_mode_preserves_bounded_text_not_only_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output-dir",
                    str(output),
                    "--view-mode",
                    "prefix",
                    "--max-view-lines",
                    "12",
                    "--max-view-chars",
                    "2000",
                    "--",
                    sys.executable,
                    "-c",
                    "[print(f'source line {i}') for i in range(200)]",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0)
            view = (output / "view.txt").read_text(encoding="utf-8")
            receipt = json.loads((output / "receipt.json").read_text(encoding="utf-8"))
            self.assertIn("source line 0", view)
            self.assertIn("source line 11", view)
            self.assertNotIn("source line 12\n", view)
            self.assertEqual(receipt["view_mode"], "prefix")
            self.assertEqual(receipt["selected_lines"], 12)
            self.assertEqual(receipt["omitted_lines"], 188)

    def test_signal_mode_enforces_total_view_character_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            program = (
                "[print(f'ERROR {i:03d} ' + 'x' * 450) for i in range(80)]\n"
                "print('FINAL useful tail')\n"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output-dir",
                    str(output),
                    "--max-view-chars",
                    "2000",
                    "--max-view-lines",
                    "80",
                    "--tail-lines",
                    "2",
                    "--signal-lines",
                    "80",
                    "--",
                    sys.executable,
                    "-c",
                    program,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads((output / "receipt.json").read_text(encoding="utf-8"))
            view = (output / "view.txt").read_text(encoding="utf-8")
            self.assertLessEqual(len(view), 2000)
            self.assertEqual(receipt["view_character_limit"], 2000)
            self.assertEqual(receipt["view_characters"], len(view))
            self.assertTrue(receipt["view_limit_reached"])
            self.assertGreater(receipt["omitted_candidate_lines"], 0)
            self.assertIn("FINAL useful tail", view)

    def test_default_signal_view_has_real_eight_thousand_character_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output-dir",
                    str(output),
                    "--",
                    sys.executable,
                    "-c",
                    "[print('WARNING ' + 'x' * 1000) for _ in range(100)]",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads((output / "receipt.json").read_text(encoding="utf-8"))
            view = (output / "view.txt").read_text(encoding="utf-8")
            self.assertEqual(receipt["view_character_limit"], 8000)
            self.assertLessEqual(len(view), 8000)

    def test_hard_output_cap_stops_runaway_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output-dir",
                    str(output),
                    "--max-output-bytes",
                    "4096",
                    "--",
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.write('x' * 1000000)",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            receipt = json.loads((output / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(result.returncode, 125)
            self.assertTrue(receipt["output_limit_reached"])
            self.assertEqual(receipt["output_bytes"], 4096)
            self.assertEqual((output / "raw.log").stat().st_size, 4096)


if __name__ == "__main__":
    unittest.main()
