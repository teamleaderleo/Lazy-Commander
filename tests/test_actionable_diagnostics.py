from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from semantic_command import is_error_line, project


class ActionableDiagnosticsTests(unittest.TestCase):
    def test_successful_test_names_are_not_promoted_as_failures(self):
        for line in (
            "(pass) packet builder > rejects invalid input and reports errors [0.60ms]",
            "test scope::panic_case - should panic ... ok",
            "test_error_case (tests.Errors.test_error_case) ... ok",
        ):
            self.assertFalse(is_error_line(line), line)
            self.assertEqual(project("bun test", line.encode(), 0)[0], line)
        self.assertTrue(is_error_line("(fail) packet builder > stable timestamp [0.60ms]"))
        self.assertTrue(is_error_line("error: expected timestamp did not match"))
        self.assertTrue(is_error_line("testing fatal error ... ok"))
        self.assertFalse(is_error_line("test/context-api-error-boundary.test.ts:"))
        self.assertTrue(is_error_line("fatal error opening file.test.ts:"))

    def test_bun_failure_retains_suite_stack_name_and_final_totals(self):
        rows = ["bun test v1.0", "test/previous.test.ts:"]
        rows += [f"(pass) case{i} > rejects error input [0.60ms]" for i in range(160)]
        rows += ["test/context-packets.test.ts:", "(pass) packet builder > builds packets [0.60ms]",
                 "192 | expect(first.generatedAt).toBe(run.endedAt);", "                                    ^",
                 "error: expect(received).toBe(expected)", "", 'Expected: "2026-09-01T00:00:00Z"',
                 'Received: "2026-09-04T00:00:00Z"', "",
                 "    at <anonymous> (/workspace/test/context-packets.test.ts:192:31)",
                 "(fail) packet builder > stable canonical timestamp [0.60ms]"]
        rows += [f"(pass) other{i} > handles failure input [0.10ms]" for i in range(160)]
        rows += [" 320 pass", " 1 fail", "Ran 321 tests across 3 files.", 'error: script "test" exited with code 1']
        payload, omitted, _, _ = project("bun test", "\n".join(rows).encode(), 1)
        for anchor in ("test/context-packets.test.ts:", "Expected:", "Received:",
                       "context-packets.test.ts:192:31", "stable canonical timestamp", " 1 fail", "Ran 321"):
            self.assertIn(anchor, payload)
        self.assertIn("lines", omitted)
        self.assertLess(len(payload), 2500)

    def test_terminal_failure_summary_survives_many_earlier_error_matches(self):
        rows = [f"{i}:(fail) integration > case {i} keeps its full identity " + "x" * 80 for i in range(100)]
        rows += ["4050 pass", "30 fail", 'error: script "test" exited with code 1']
        payload, omitted, _, _ = project("rg failure results.log", "\n".join(rows).encode(), 0)
        self.assertIn("30 fail", payload)
        self.assertIn('error: script "test" exited with code 1', payload)
        self.assertLessEqual(len(payload), 7000)
        self.assertIn("lines", omitted)

    def test_failed_rust_binary_does_not_fill_spare_budget_with_passed_tests(self):
        rows = ["   Compiling example", "    Running tests/observe.rs (target/debug/deps/observe-123)"]
        rows += [f"test case_{i} ... ok" for i in range(400)]
        rows += ["failures:", "---- observation stdout ----", "thread 'observation' panicked at tests/observe.rs:81:5:",
                 "assertion failed: output.status.success()", "failures:", "    observation",
                 "test result: FAILED. 400 passed; 1 failed", "error: test failed, to rerun pass `--test observe`"]
        payload, omitted, _, _ = project("cargo test", "\n".join(rows).encode(), 101)
        self.assertIn("Running tests/observe.rs", payload)
        self.assertIn("tests/observe.rs:81:5", payload)
        self.assertIn("--test observe", payload)
        self.assertLess(len(payload), 2000)
        self.assertIn("lines", omitted)

    def test_huge_diagnostic_remains_visible_alongside_terminal_status(self):
        raw = ("start\nERROR diagnostic-start " + "x" * 10_000 + " diagnostic-end\nterminal failure\n").encode()
        payload, omitted, _, _ = project("cargo test", raw, 1)
        self.assertIn("ERROR diagnostic-start", payload)
        self.assertIn("diagnostic-end", payload)
        self.assertIn("terminal failure", payload)
        self.assertIn("middle of block omitted", payload)
        self.assertGreater(omitted["characters"], 0)
        self.assertLessEqual(len(payload), 7000)


if __name__ == "__main__":
    unittest.main()
