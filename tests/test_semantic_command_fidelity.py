from __future__ import annotations

import json
import os
import re
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
from semantic_command_hook import store as store_request  # noqa: E402
from semantic_command import project  # noqa: E402


class SemanticCommandFidelityTests(unittest.TestCase):
    def store(self, root: Path, command: str) -> str:
        payload = {
            "cwd": str(root),
            "tool_input": {"command": command},
            "session_id": "fidelity-session",
            "turn_id": "fidelity-turn",
            "tool_use_id": "fidelity-tool",
        }
        with mock.patch.dict(os.environ, {"LAZY_COMMAND_STATE_ROOT": str(root / "state")}):
            return store_request(payload, command)

    def run_stored(
        self, root: Path, command: str
    ) -> tuple[str, subprocess.CompletedProcess[str]]:
        identifier = self.store(root, command)
        result = subprocess.run(
            [sys.executable, str(RUNNER), "run", identifier],
            text=True,
            capture_output=True,
            env={**os.environ, "LAZY_COMMAND_STATE_ROOT": str(root / "state")},
            check=False,
        )
        return identifier, result

    def test_install_uses_its_own_runner_when_path_contains_another_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            (bin_dir / "lazy-command").symlink_to("/bin/true")
            result = subprocess.run(
                [sys.executable, str(RUNNER), "install", "--user"],
                capture_output=True, text=True,
                env={**os.environ, "HOME": str(root), "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            hooks = json.loads((root / ".codex/hooks.json").read_text())
            command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
            self.assertEqual(command, shlex.quote(str(RUNNER.resolve())) + " hook")
            self.assertEqual((root / ".local/bin/lc").resolve(), RUNNER.resolve())

    def test_gh_json_preserves_bodies_past_legacy_limits_and_full_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pr_start, pr_end = "PR-BODY-START|", "|PR-BODY-END"
            comment_start, comment_end = "COMMENT-BODY-START|", "|COMMENT-BODY-END"
            body_636 = pr_start + "a" * (636 - len(pr_start) - len(pr_end)) + pr_end
            body_4500 = (
                comment_start
                + "b" * (4500 - len(comment_start) - len(comment_end))
                + comment_end
            )
            self.assertEqual(len(body_636), 636)
            self.assertEqual(len(body_4500), 4500)
            document = {
                "number": 237,
                "author": {"id": "U_kgDOB123456789", "login": "full-author-login"},
                "body": body_636,
                "headRefOid": "0123456789abcdef0123456789abcdef01234567",
                "mergeCommit": {"oid": "fedcba9876543210fedcba9876543210fedcba98"},
                "comments": [
                    {
                        "author": {"id": "U_kgDOC987654321", "login": "reviewer-full-login"},
                        "body": body_4500,
                    }
                ],
            }
            encoded = json.dumps(document, separators=(",", ":"), ensure_ascii=False)
            self.assertLessEqual(len(encoded), 7_000)
            fixture = root / "gh.json"
            fixture.write_text(encoded + "\n", encoding="utf-8")
            command = f": gh pr view 237 --json body,author,comments; cat {shlex.quote(str(fixture))}"

            _identifier, result = self.run_stored(root, command)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(encoded, result.stdout)
            self.assertIn("PR-BODY-END", result.stdout)
            self.assertIn("COMMENT-BODY-END", result.stdout)
            self.assertIn("full-author-login", result.stdout)
            self.assertIn("reviewer-full-login", result.stdout)
            self.assertIn("0123456789abcdef0123456789abcdef01234567", result.stdout)
            self.assertIn("fedcba9876543210fedcba9876543210fedcba98", result.stdout)
            self.assertNotIn("omitted characters", result.stdout)

    def test_small_gh_json_keeps_the_requested_body_verbatim(self):
        raw = b'{"body":"Full requested explanation"}'

        payload, omitted, lines, errors = project(
            "gh pr view 1 --json body", raw, 0
        )

        self.assertEqual(payload, raw.decode())
        self.assertEqual(omitted, {})
        self.assertEqual(lines, 1)
        self.assertEqual(errors, 0)

    def test_gh_run_watch_keeps_header_and_every_terminal_job_identity(self):
        watch_header = "Refreshing run status every 10 seconds. Press Ctrl+C to quit."
        terminal_header = "✓ feature/projector CI example/repository#42 · 123456789"
        terminal_jobs = [
            "runtime-parity (ID 100000000101)",
            "test (ID 100000000102)",
            "browser-evidence (ID 100000000103)",
            "exact-ref-validation-receipt (ID 100000000104)",
            "serial-full (ID 100000000105)",
        ]

        def snapshot(generation: int, terminal: bool = False) -> list[str]:
            rows = [
                (
                    "✓" if terminal else "*"
                ) + " feature/projector CI example/repository#42 · 123456789",
                "Triggered via workflow_dispatch less than a minute ago",
                "",
                "JOBS",
            ]
            for job_index, identity in enumerate(terminal_jobs):
                marker = "✓" if terminal and job_index < 4 else "-" if terminal else "*"
                rows.append(f"{marker} {identity}")
                rows.extend(
                    f"  ✓ generation-{generation} job-{job_index} step-{step:02d} " + "x" * 36
                    for step in range(12)
                )
            return rows

        rows = [watch_header, ""]
        for generation in range(4):
            rows.extend(snapshot(generation))
        rows.extend(snapshot(4, terminal=True))

        payload, omitted, _lines, errors = project(
            "gh run watch 123456789", ("\n".join(rows) + "\n").encode(), 0
        )

        self.assertLessEqual(len(payload), 7_000)
        self.assertIn(watch_header, payload)
        self.assertIn(terminal_header, payload)
        for identity in terminal_jobs:
            self.assertIn(identity, payload)
        self.assertIn("[… lines ", payload)
        self.assertIn("lines", omitted)
        self.assertEqual(errors, 0)

    def test_large_failure_keeps_each_error_and_its_traceback_neighborhood(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = ["session header", "collecting tests"]
            expected = []
            for case in range(5):
                rows.extend(f"noise-{case}-{index:03d}-" + "x" * 45 for index in range(70))
                block = [
                    f'  File "/srv/app/case_{case}.py", line {100 + case}, in execute',
                    f"    distinct_call_{case}()",
                    "Traceback (most recent call last):",
                    f"RuntimeError: distinct failure {case}",
                    f"context-after-{case}-one",
                    f"context-after-{case}-two",
                ]
                expected.append(block)
                rows.extend(block)
            rows.extend(f"tail-{index}-" + "z" * 45 for index in range(20))
            fixture = root / "failure.log"
            fixture.write_text("\n".join(rows) + "\n", encoding="utf-8")
            command = f": pytest -q; cat {shlex.quote(str(fixture))}; exit 9"

            _identifier, result = self.run_stored(root, command)

            self.assertEqual(result.returncode, 9)
            self.assertIn("FAIL 9 · Command", result.stdout)
            for block in expected:
                for row in block:
                    self.assertIn(row, result.stdout)
            self.assertRegex(result.stdout, r"\[… lines \d+–\d+ omitted …\]")
            self.assertRegex(
                result.stdout,
                r"expand: lc s [0-9a-f]{16} --find TEXT \(or --lines START:END\)",
            )

    def test_large_head_cannot_displace_later_diagnostics(self):
        progress = [f"progress-{index}-" + "p" * 1_990 for index in range(3)]
        context = [f"context-{index}-" + "c" * 190 for index in range(3)]
        errors = [f"ERROR sentinel-{index}-" + "e" * 180 for index in range(6)]
        raw = ("\n".join(progress + context + errors) + "\n").encode()
        self.assertGreater(len(raw), 7_000)

        payload, omitted, _lines, error_count = project("pytest", raw, 1)

        self.assertLessEqual(len(payload), 7_000)
        self.assertEqual(error_count, len(errors))
        for line in errors:
            self.assertIn(line, payload)
        self.assertIn("lines", omitted)

    def test_adjacent_package_prefixes_are_factored_reversibly(self):
        rows = [
            "Setting up libsubid0:amd64 (1:4.17.4-2ubuntu3)",
            "Setting up uidmap (1:4.17.4-2ubuntu3)",
            "Setting up virtiofsd (1.13.2-6ubuntu0)",
            "Processing triggers for man-db (2.13.1-1)",
            "Processing triggers for libc-bin (2.41-6ubuntu1)",
        ]

        payload, omitted, _lines, errors = project(
            "apt-get install uidmap virtiofsd", ("\n".join(rows) + "\n").encode(), 0
        )

        self.assertIn('prefix "Setting up "', payload)
        self.assertIn('+ "libsubid0:amd64 (1:4.17.4-2ubuntu3)"', payload)
        self.assertIn('prefix "Processing triggers for "', payload)
        self.assertIn('+ "libc-bin (2.41-6ubuntu1)"', payload)
        reconstructed = []
        prefix = ""
        for line in payload.splitlines():
            if line.startswith("prefix "):
                prefix = json.loads(line.removeprefix("prefix "))
            elif line.startswith("+ "):
                reconstructed.append(prefix + json.loads(line.removeprefix("+ ")))
            else:
                prefix = ""
                reconstructed.append(line)
        self.assertEqual(reconstructed, rows)
        self.assertEqual(omitted, {})
        self.assertEqual(errors, 0)

    def test_prefix_factoring_preserves_warnings_json_and_diagnostic_indentation(self):
        warning_rows = [
            "WARNING dependency installer selected package alpha detail=keep-a",
            "WARNING dependency installer selected package beta detail=keep-b",
            "WARNING dependency installer selected package gamma detail=keep-c",
        ]
        warning_payload, _omitted, _lines, _errors = project(
            "apt-get install packages", ("\n".join(warning_rows) + "\n").encode(), 0
        )
        self.assertIn('prefix "WARNING dependency installer selected package "', warning_payload)
        self.assertIn('detail=keep-a"', warning_payload)
        self.assertIn('detail=keep-b"', warning_payload)
        self.assertIn('detail=keep-c"', warning_payload)

        json_rows = [
            '{"message":"Setting up alpha"}',
            '{"message":"Setting up beta"}',
        ]
        json_payload, _omitted, _lines, _errors = project(
            "command", ("\n".join(json_rows) + "\n").encode(), 0
        )
        self.assertEqual(json_payload, "\n".join(json_rows))

        diagnostic_rows = [
            "    package frame alpha detail",
            "    package frame beta detail",
        ]
        diagnostic_payload, _omitted, _lines, _errors = project(
            "command", ("\n".join(diagnostic_rows) + "\n").encode(), 0
        )
        self.assertEqual(diagnostic_payload, "\n".join(diagnostic_rows))

    def test_unittest_progress_success_uses_closed_grammar(self):
        raw = (
            "...sx\n"
            + "-" * 70
            + "\nRan 5 tests in 0.001s\n\nOK (skipped=1, expected failures=1)\n"
        ).encode()

        payload, omitted, lines, errors = project(
            "python3 -m unittest discover", raw, 0
        )

        self.assertEqual(
            payload,
            "unittest progress 5 · passed 3, skipped 1, expected failures 1\n"
            "Ran 5 tests in 0.001s\n"
            "OK (skipped=1, expected failures=1)",
        )
        self.assertEqual(omitted, {"unittest_progress": 5, "separator": 1})
        self.assertEqual(lines, 5)
        self.assertEqual(errors, 0)

    def test_unittest_compaction_declines_unexpected_output(self):
        separator = "-" * 70
        cases = [
            (
                "python3 -m unittest discover",
                f".\nDeprecationWarning: keep this detail\n{separator}\nRan 1 test in 0.001s\n\nOK\n",
                0,
            ),
            (
                "python3 -m unittest discover",
                f"F\n{separator}\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n",
                1,
            ),
            (
                "python3 -m unittest discover",
                f"..\n{separator}\nRan 3 tests in 0.001s\n\nOK\n",
                0,
            ),
            (
                "python3 -m unittest discover -v",
                f"test_one (suite.Case.test_one) ... ok\n{separator}\nRan 1 test in 0.001s\n\nOK\n",
                0,
            ),
            (
                "pytest",
                f".\n{separator}\nRan 1 test in 0.001s\n\nOK\n",
                0,
            ),
            (
                "python3 -m unittest discover",
                f".s\n{separator}\nRan 2 tests in 0.001s\n\nOK\n",
                0,
            ),
        ]
        for command, text, exit_code in cases:
            with self.subTest(command=command, first=text.splitlines()[0]):
                payload, omitted, _lines, _errors = project(command, text.encode(), exit_code)
                self.assertEqual(payload, "\n".join(text.splitlines()))
                self.assertEqual(omitted, {})

    def test_adjacent_exact_line_fold_is_reversible_from_private_raw(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "repeat.log"
            fixture.write_bytes(b"before\nsame warning\nsame warning\nsame warning\nafter\n")
            command = f": pytest -q; cat {shlex.quote(str(fixture))}"

            identifier, result = self.run_stored(root, command)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.count("same warning"), 1)
            self.assertIn("[repeat ×3]", result.stdout)
            receipt = json.loads((root / "state" / identifier / "receipt.json").read_text())
            self.assertEqual(receipt["omitted"], {"repeat": 2})
            shown = subprocess.run(
                [sys.executable, str(RUNNER), "show", "--raw", identifier],
                capture_output=True,
                env={**os.environ, "LAZY_COMMAND_STATE_ROOT": str(root / "state")},
                check=False,
            )
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertEqual(shown.stdout, fixture.read_bytes())

    def test_raw_byte_pages_are_exact_and_limit_is_capped_at_eight_kilobytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = bytes(range(256)) * 40
            fixture = root / "binary.log"
            fixture.write_bytes(raw)
            command = f": pytest -q; cat {shlex.quote(str(fixture))}"
            identifier, run = self.run_stored(root, command)
            self.assertEqual(run.returncode, 0, run.stderr)
            env = {**os.environ, "LAZY_COMMAND_STATE_ROOT": str(root / "state")}

            page = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "show",
                    "--raw",
                    identifier,
                    "--offset",
                    "137",
                    "--limit",
                    "257",
                ],
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(page.returncode, 0, page.stderr)
            self.assertEqual(page.stdout, raw[137:394])
            self.assertIn(
                f"next: lc s -r {identifier} --offset 394 --limit 257".encode(),
                page.stderr,
            )

            capped = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "show",
                    "--raw",
                    identifier,
                    "--offset",
                    "0",
                    "--limit",
                    "99999",
                ],
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(capped.returncode, 0, capped.stderr)
            self.assertEqual(capped.stdout, raw[:8_000])
            self.assertIn(
                f"next: lc s -r {identifier} --offset 8000 --limit 8000".encode(),
                capped.stderr,
            )


if __name__ == "__main__":
    unittest.main()
