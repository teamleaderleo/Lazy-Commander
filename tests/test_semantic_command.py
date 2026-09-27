from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"
RUNNER = SCRIPTS / "semantic_command.py"
HOOK = SCRIPTS / "semantic_command_hook.py"
sys.path.insert(0, str(SCRIPTS))
from semantic_command_hook import private_group, store as store_request  # noqa: E402


class SemanticCommandTests(unittest.TestCase):
    def hook(
        self,
        root: Path,
        command: str,
        permission: str = "bypassPermissions",
        workdir: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        tool_input = {"command": command}
        if workdir is not None:
            tool_input["workdir"] = str(workdir)
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": tool_input,
            "cwd": str(root),
            "permission_mode": permission,
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_use_id": "tool-1",
        }
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env={**os.environ, "LAZY_COMMAND_STATE_ROOT": str(root / "state")},
            check=False,
        )

    def rewritten(self, result: subprocess.CompletedProcess[str]) -> str:
        return json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["command"]

    def identifier(self, result: subprocess.CompletedProcess[str]) -> str:
        match = re.search(r"\brun ([0-9a-f]{16})\b", self.rewritten(result))
        self.assertIsNotNone(match)
        return match.group(1)

    def store(self, root: Path, command: str, workdir: Path | None = None) -> str:
        payload = {
            "cwd": str(root),
            "tool_input": {"command": command},
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_use_id": "tool-1",
        }
        if workdir is not None:
            payload["tool_input"]["workdir"] = str(workdir)
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

    def test_hook_stows_noisy_git_command_and_returns_short_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.hook(root, "git status --short --branch && git pull --ff-only")
            self.assertEqual(result.returncode, 0, result.stderr)
            rewritten = self.rewritten(result)
            self.assertRegex(
                rewritten,
                r'\b(?:lazy-command|semantic_command\.py) probe [0-9a-f]{16} --cwd "\$PWD"; then .+ run [0-9a-f]{16} --cwd "\$PWD"; .+ fallback [0-9a-f]{16} --cwd "\$PWD"; fi$',
            )
            identifier = self.identifier(result)
            request = json.loads((root / "state" / identifier / "request.json").read_text())
            self.assertEqual(request["command"], "git status --short --branch && git pull --ff-only")
            self.assertEqual(oct((root / "state" / identifier / "request.json").stat().st_mode & 0o777), "0o600")

    def test_tool_workdir_overrides_session_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            hook = self.hook(root, "git status --short --branch", workdir=repository)
            identifier = self.identifier(hook)
            request = json.loads((root / "state" / identifier / "request.json").read_text())
            self.assertEqual(request["cwd"], str(repository.resolve()))

            result = subprocess.run(
                [sys.executable, str(RUNNER), "run", identifier],
                text=True,
                capture_output=True,
                env={**os.environ, "LAZY_COMMAND_STATE_ROOT": str(root / "state")},
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("not a git repository", result.stdout)

    def test_rewritten_command_captures_runtime_workdir_when_hook_omits_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            hook = self.hook(root, "git status --short --branch")
            identifier = self.identifier(hook)

            result = subprocess.run(
                ["/bin/bash", "-c", self.rewritten(hook)],
                cwd=repository,
                text=True,
                capture_output=True,
                env={**os.environ, "LAZY_COMMAND_STATE_ROOT": str(root / "state")},
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("not a git repository", result.stdout)
            receipt = json.loads((root / "state" / identifier / "receipt.json").read_text())
            self.assertEqual(receipt["cwd"], str(repository.resolve()))

    def test_group_write_acceptance_matches_group_privacy(self):
        group_is_private = private_group(RUNNER.stat().st_gid)
        mode = RUNNER.stat().st_mode
        RUNNER.chmod(stat.S_IMODE(mode) | 0o020)
        try:
            with tempfile.TemporaryDirectory() as directory:
                result = self.hook(Path(directory), "git status --short --branch")
                self.assertEqual(result.returncode, 0, result.stderr)
                if group_is_private:
                    self.assertIn("updatedInput", result.stdout)
                else:
                    self.assertEqual(result.stdout, "")
        finally:
            RUNNER.chmod(stat.S_IMODE(mode))

    def test_hook_leaves_source_reads_and_approval_sessions_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(self.hook(root, "sed -n '1,200p' AGENTS.md").stdout, "")
            self.assertEqual(self.hook(root, "git diff -- src/main.py").stdout, "")
            self.assertEqual(self.hook(root, "gh pr view 12").stdout, "")
            self.assertEqual(self.hook(root, "git status", permission="ask").stdout, "")

    def test_hook_wraps_current_codex_dont_ask_permission_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.hook(Path(directory), "git status --short", permission="dontAsk")
            self.assertIn("updatedInput", result.stdout)

    def test_hook_wraps_unbounded_searches_but_leaves_bounded_pipeline_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broad = self.hook(root, "rg -n needle /home/alice/.codex | sed -n '1,160p'")
            self.assertEqual(broad.stdout, "")
            mac_broad = self.hook(root, "find /Users/alice/.codex -maxdepth 1 -type f -print")
            self.assertIn("updatedInput", mac_broad.stdout)
            compound = self.hook(root, "rg needle repo-a; rg needle repo-b")
            self.assertIn("updatedInput", compound.stdout)
            scoped = self.hook(root, "rg -n needle src tests")
            self.assertIn("updatedInput", scoped.stdout)
            find = self.hook(root, "find src -name '*.py' -print")
            self.assertIn("updatedInput", find.stdout)

    def test_hook_does_not_admit_noop_commands_that_only_quote_noisy_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.hook(root, ": gh pr view 12 --json body; printf 'fixture\n'")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

    def test_hook_stows_codex_exec_including_json_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.hook(root, "codex exec --ephemeral --json 'bounded task'")
            self.assertEqual(result.returncode, 0, result.stderr)
            identifier = self.identifier(result)
            request = json.loads((root / "state" / identifier / "request.json").read_text())
            self.assertEqual(request["command"], "codex exec --ephemeral --json 'bounded task'")

    def test_codex_exec_receipt_preserves_json_events_and_folds_exact_adjacent_warnings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = (
                ": codex exec; "
                "printf '%s\\n' "
                "'WARN codex_rollout state discrepancy falling_back' "
                "'WARN codex_rollout state discrepancy falling_back' "
                "'{\"type\":\"item.completed\",\"item\":{\"aggregated_output\":\"SECRET RAW OUTPUT\"}}' "
                "'{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":1234}}'"
            )
            identifier, result = self.run_stored(root, command)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.startswith("OK · Command"), result.stdout)
            self.assertIn("SECRET RAW OUTPUT", result.stdout)
            self.assertIn('"input_tokens":1234', result.stdout)
            self.assertEqual(result.stdout.count("state discrepancy falling_back"), 1)
            self.assertIn("[repeat ×2]", result.stdout)
            raw = (root / "state" / identifier / "raw.log").read_text()
            self.assertIn("SECRET RAW OUTPUT", raw)
            self.assertEqual(raw.count("state discrepancy falling_back"), 2)
            receipt = json.loads((root / "state" / identifier / "receipt.json").read_text())
            self.assertEqual(receipt["omitted"], {"repeat": 1})

    def test_rewritten_command_runs_original_once_when_receipt_store_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hook = self.hook(root, "git status --short >/dev/null; printf 'FALLBACK_OK\\n'")
            identifier = self.identifier(hook)
            run_dir = root / "state" / identifier
            run_dir.chmod(0o500)
            try:
                result = subprocess.run(
                    ["/bin/bash", "-c", self.rewritten(hook)],
                    text=True,
                    capture_output=True,
                    env={**os.environ, "LAZY_COMMAND_STATE_ROOT": str(root / "state")},
                    check=False,
                )
            finally:
                run_dir.chmod(0o700)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "FALLBACK_OK\n")
            self.assertFalse((run_dir / "receipt.json").exists())

    def test_broad_search_receipt_reversibly_factors_repeated_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = (
                ": rg /home/alice/.codex; printf '%s\\n' "
                "'/home/alice/.codex/state/runs/alpha' "
                "'/home/alice/.codex/state/runs/beta' "
                "'/home/alice/.codex/state/runs/gamma'"
            )
            _identifier, result = self.run_stored(root, command)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK · Command", result.stdout)
            self.assertIn('prefix "/home/alice/.codex/state/runs/"', result.stdout)
            self.assertIn('+ "alpha"', result.stdout)
            self.assertIn('+ "beta"', result.stdout)
            self.assertIn('+ "gamma"', result.stdout)

    def test_project_root_search_receipt_factors_repeated_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = (
                ": rg --files /Users/alice/Projects; printf '%s\\n' "
                "'/Users/alice/Projects/proj-a/AGENTS.md' "
                "'/Users/alice/Projects/proj-b/README.md' "
                "'/Users/alice/Projects/proj-c/pyproject.toml'"
            )
            _identifier, result = self.run_stored(root, command)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK · Command", result.stdout)
            self.assertIn('prefix "/Users/alice/Projects/"', result.stdout)
            self.assertIn('+ "proj-a/AGENTS.md"', result.stdout)
            self.assertIn('+ "proj-b/README.md"', result.stdout)
            self.assertIn('+ "proj-c/pyproject.toml"', result.stdout)

    def test_project_child_search_is_wrapped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.hook(root, "rg needle /Users/alice/Projects/proj-a")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("updatedInput", result.stdout)

    def test_compound_search_preserves_sections_bodies_and_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = (
                ": rg needle repo-a; printf '%s\\n' 'PROJ-C' "
                "'/Users/alice/Projects/proj-c/.github/workflows/a.yml:1:runs-on: self-hosted' "
                "'/Users/alice/Projects/proj-c/.github/workflows/b.yml:2:runs-on: self-hosted' "
                "'/Users/alice/Projects/proj-c/.github/workflows/c.yml:3:labels: example-ci'; "
                ": rg needle repo-b; printf '%s\\n' 'STARSECTOR' "
                "'/Users/alice/Projects/starsector/.github/workflows/run.yml:4:runs-on: self-hosted'"
            )
            _identifier, result = self.run_stored(root, command)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PROJ-C", result.stdout)
            self.assertIn('prefix "/Users/alice/Projects/proj-c/.github/workflows/"', result.stdout)
            self.assertIn('+ "a.yml:1:runs-on: self-hosted"', result.stdout)
            self.assertIn('+ "b.yml:2:runs-on: self-hosted"', result.stdout)
            self.assertIn('+ "c.yml:3:labels: example-ci"', result.stdout)
            self.assertIn("STARSECTOR", result.stdout)
            self.assertIn(
                "/Users/alice/Projects/starsector/.github/workflows/run.yml:4:runs-on: self-hosted",
                result.stdout,
            )

    def test_runner_preserves_complete_git_and_github_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = (
                ": gh pr view 980 --json state git status; printf '%s\\n' "
                "'{\"state\":\"MERGED\",\"mergeCommit\":{\"oid\":\"bbdcd44e7f76623b\"}}' "
                "'hint: noob advice' 'Switched to branch main' 'Fast-forward' "
                "' create mode 100644 noisy/file' '14 files changed, 2634 insertions(+)'"
            )
            identifier, result = self.run_stored(root, command)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK · Command", result.stdout)
            self.assertIn('"oid":"bbdcd44e7f76623b"', result.stdout)
            self.assertIn("14 files changed", result.stdout)
            self.assertIn("noob advice", result.stdout)
            self.assertIn("create mode 100644 noisy/file", result.stdout)
            raw = (root / "state" / identifier / "raw.log").read_text()
            self.assertIn("noob advice", raw)
            receipt = json.loads((root / "state" / identifier / "receipt.json").read_text())
            self.assertEqual(receipt["omitted"], {})

    def test_successful_pr_check_watch_preserves_each_observed_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = (
                ": gh pr checks 40 --watch; printf '%s\\n' "
                "'Refreshing checks status every 10 seconds. Press Ctrl+C to quit.' "
                "'GitGuardian Security Checks\tpending\t0\thttps://dashboard.gitguardian.com' "
                "'Refreshing checks status every 10 seconds. Press Ctrl+C to quit.' "
                "'GitGuardian Security Checks\tpass\t22s\thttps://dashboard.gitguardian.com'"
            )
            _identifier, result = self.run_stored(root, command)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK · Command", result.stdout)
            self.assertEqual(result.stdout.count("Refreshing checks status every 10 seconds"), 2)
            self.assertIn("GitGuardian Security Checks\tpending", result.stdout)
            self.assertIn("GitGuardian Security Checks\tpass", result.stdout)

    def test_failed_pr_check_watch_retains_pending_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = (
                ": gh pr checks 40 --watch; printf '%s\\n' "
                "'GitGuardian Security Checks\tpending\t0\thttps://dashboard.gitguardian.com'; "
                "exit 2"
            )
            _identifier, result = self.run_stored(root, command)
            self.assertEqual(result.returncode, 2)
            self.assertIn("FAIL 2 · Command", result.stdout)
            self.assertIn("GitGuardian Security Checks", result.stdout)
            self.assertIn("pending", result.stdout)

    def test_failure_preserves_error_and_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _identifier, result = self.run_stored(
                root, "pytest --version; printf 'ERROR boom\\n'; exit 7"
            )
            self.assertEqual(result.returncode, 7)
            self.assertIn("FAIL 7 · Command", result.stdout)
            self.assertIn("ERROR boom", result.stdout)

    def test_unittest_success_preserves_passing_test_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = (
                ": python3 -m unittest discover -v; printf '%s\\n' "
                "'test_alpha (suite.Case.test_alpha) ... ok' "
                "'test_beta (suite.Case.test_beta) ... ok' "
                "'----------------------------------------' "
                "'Ran 2 tests in 0.001s' 'OK'"
            )
            _identifier, result = self.run_stored(root, command)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK · Command", result.stdout)
            self.assertIn("Ran 2 tests in 0.001s", result.stdout)
            self.assertIn("test_alpha (suite.Case.test_alpha) ... ok", result.stdout)
            self.assertIn("test_beta (suite.Case.test_beta) ... ok", result.stdout)
            self.assertRegex(result.stdout, r"(?m)^OK$")

    def test_unittest_failure_retains_failing_test_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = (
                ": python3 -m unittest discover -v; printf '%s\\n' "
                "'test_bad (suite.Case.test_bad) ... FAIL' "
                "'AssertionError: expected true' 'FAILED (failures=1)'; exit 1"
            )
            _identifier, result = self.run_stored(root, command)
            self.assertEqual(result.returncode, 1)
            self.assertIn("FAIL 1 · Command", result.stdout)
            self.assertIn("test_bad", result.stdout)
            self.assertIn("AssertionError", result.stdout)

    def test_direct_shell_fallback_uses_receipts_without_reusing_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = (
                "printf '%s\\n' 'test_alpha (suite.Case.test_alpha) ... ok' "
                "'Ran 1 test in 0.001s' 'OK'"
            )
            invocation = [
                sys.executable,
                str(RUNNER),
                "shell",
                "--cwd",
                str(root),
                command,
            ]
            environment = {**os.environ, "LAZY_COMMAND_STATE_ROOT": str(root / "state")}
            first = subprocess.run(
                invocation,
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            second = subprocess.run(
                invocation,
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("test_alpha", first.stdout)
            self.assertIn("Ran 1 test in 0.001s", first.stdout)
            identifiers = re.findall(r"\bid ([0-9a-f]{16})\b", first.stdout + second.stdout)
            self.assertEqual(len(identifiers), 2)
            self.assertNotEqual(identifiers[0], identifiers[1])
            self.assertEqual(len(list((root / "state").glob("*/receipt.json"))), 2)

    def test_other_test_success_detail_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = (
                ": pytest -v; printf '%s\\n' "
                "'tests/test_api.py::test_get PASSED [ 25%]' "
                "'test parser::accepts_input ... ok' "
                "'PASS src/widget.test.ts' "
                "'✓ renders the widget 2ms' "
                "'--- PASS: TestRoute (0.00s)' "
                "'4 passed in 0.12s'"
            )
            _identifier, result = self.run_stored(root, command)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("4 passed in 0.12s", result.stdout)
            self.assertIn("tests/test_api.py::test_get PASSED [ 25%]", result.stdout)
            self.assertIn("test parser::accepts_input ... ok", result.stdout)
            self.assertIn("PASS src/widget.test.ts", result.stdout)
            self.assertIn("✓ renders the widget 2ms", result.stdout)
            self.assertIn("--- PASS: TestRoute (0.00s)", result.stdout)

    def test_many_json_rows_remain_distinct_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = (
                ": gh run list; printf '%s\\n' "
                "'{\"conclusion\":\"success\"}' "
                "'{\"conclusion\":\"failure\"}' "
                "'{\"conclusion\":\"success\"}'"
            )
            _identifier, result = self.run_stored(root, command)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.count('{"conclusion":"success"}'), 2)
            self.assertEqual(result.stdout.count('{"conclusion":"failure"}'), 1)

    def test_zero_error_summary_does_not_turn_success_into_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _identifier, result = self.run_stored(root, ": git status; printf '0 errors, 12 passed\\n'")
            self.assertTrue(result.stdout.startswith("OK · Command"), result.stdout)
            self.assertIn("0 errors, 12 passed", result.stdout)

    def test_short_shell_defaults_to_process_cwd_and_returns_small_raw(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run(
                [sys.executable, str(RUNNER), "sh", "-r", "pwd"],
                cwd=root,
                text=True,
                capture_output=True,
                env={**os.environ, "LAZY_COMMAND_STATE_ROOT": str(root / "state")},
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(Path(result.stdout.strip()).resolve(), root.resolve())
            self.assertEqual(len(list((root / "state").glob("*/receipt.json"))), 1)

    def test_root_shortcut_routes_directly_to_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run(
                [sys.executable, str(RUNNER), "-r", "printf 'direct\\n'"],
                text=True,
                capture_output=True,
                env={**os.environ, "LAZY_COMMAND_STATE_ROOT": str(root / "state")},
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "direct\n")

    def test_root_shortcut_accepts_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            result = subprocess.run(
                [sys.executable, str(RUNNER), "-C", str(target), "-r", "pwd"],
                cwd=root,
                text=True,
                capture_output=True,
                env={**os.environ, "LAZY_COMMAND_STATE_ROOT": str(root / "state")},
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(Path(result.stdout.strip()).resolve(), target.resolve())
            self.assertEqual(len(list((root / "state").glob("*/receipt.json"))), 1)

    def test_root_help_foregrounds_shortcut_invocation(self):
        result = subprocess.run(
            [sys.executable, str(RUNNER), "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[-C PATH] [-r | --since ID] COMMAND", result.stdout)
        self.assertIn("shortcut: run a command", result.stdout)

    def test_short_shell_accepts_argv_after_separator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "sh",
                    "-r",
                    "--",
                    "printf",
                    "%s\\n",
                    "hello world",
                ],
                text=True,
                capture_output=True,
                env={**os.environ, "LAZY_COMMAND_STATE_ROOT": str(root / "state")},
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "hello world\n")

    def test_inline_raw_keeps_semantic_view_when_output_exceeds_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "sh",
                    "-r",
                    "--raw-limit",
                    "16",
                    "--",
                    sys.executable,
                    "-c",
                    "print('x' * 64)",
                ],
                text=True,
                capture_output=True,
                env={**os.environ, "LAZY_COMMAND_STATE_ROOT": str(root / "state")},
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("output withheld", result.stdout)
            self.assertRegex(result.stdout, r"id [0-9a-f]{16}")

    def test_receipt_factors_adjacent_package_lines_without_losing_suffixes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {**os.environ, "LAZY_COMMAND_STATE_ROOT": str(root / "state")}
            command = (
                "printf '%s\\n' "
                "'Setting up libsubid0:amd64 (1:4.17.4-2ubuntu3)' "
                "'Setting up uidmap (1:4.17.4-2ubuntu3)' "
                "'Setting up virtiofsd (1.13.2-6ubuntu0)' "
                "'Processing triggers for man-db (2.13.1-1)' "
                "'Processing triggers for libc-bin (2.41-6ubuntu1)'"
            )
            result = subprocess.run(
                [sys.executable, str(RUNNER), "sh", command],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('prefix "Setting up "', result.stdout)
            self.assertIn('+ "libsubid0:amd64 (1:4.17.4-2ubuntu3)"', result.stdout)
            self.assertIn('+ "uidmap (1:4.17.4-2ubuntu3)"', result.stdout)
            self.assertIn('+ "virtiofsd (1.13.2-6ubuntu0)"', result.stdout)
            self.assertIn('prefix "Processing triggers for "', result.stdout)
            self.assertIn('+ "man-db (2.13.1-1)"', result.stdout)
            self.assertIn('+ "libc-bin (2.41-6ubuntu1)"', result.stdout)
            self.assertRegex(result.stdout, r"id [0-9a-f]{16}")

    def test_short_show_alias_can_recover_raw_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {**os.environ, "LAZY_COMMAND_STATE_ROOT": str(root / "state")}
            run = subprocess.run(
                [sys.executable, str(RUNNER), "sh", "printf 'kept raw\\n'"],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            identifier = re.search(r"id ([0-9a-f]{16})", run.stdout)
            self.assertIsNotNone(identifier)
            shown = subprocess.run(
                [sys.executable, str(RUNNER), "s", "-r", identifier.group(1)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertEqual(shown.stdout, "kept raw\n")

    def test_installer_is_workspace_local_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {**os.environ, "PATH": f"{Path.home() / '.local/bin'}:{os.environ.get('PATH', '')}"}
            first = subprocess.run(
                [sys.executable, str(RUNNER), "install", "--workspace", str(root)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            second = subprocess.run(
                [sys.executable, str(RUNNER), "install", "--workspace", str(root)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            document = json.loads((root / ".codex" / "hooks.json").read_text())
            self.assertEqual(len(document["hooks"]["PreToolUse"]), 1)
            self.assertEqual(document["hooks"]["PreToolUse"][0]["matcher"], "Bash")
            self.assertIn("already installed", second.stdout)

    def test_installer_supports_one_user_level_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            env = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{home / '.local/bin'}:{os.environ.get('PATH', '')}",
            }
            first = subprocess.run(
                [sys.executable, str(RUNNER), "install", "--user"],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            second = subprocess.run(
                [sys.executable, str(RUNNER), "install", "--user"],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            path = home / ".codex" / "hooks.json"
            document = json.loads(path.read_text())
            self.assertEqual(len(document["hooks"]["PreToolUse"]), 1)
            self.assertEqual(document["hooks"]["PreToolUse"][0]["matcher"], "Bash")
            self.assertIn("user-level", first.stdout)
            self.assertIn("already installed", second.stdout)
            self.assertNotIn("lc off PATH", first.stdout)
            alias = home / ".local" / "bin" / "lc"
            self.assertTrue(alias.is_symlink())
            self.assertEqual(alias.resolve(), RUNNER.resolve())

    def test_user_installer_repairs_a_moved_clone(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            env = {**os.environ, "HOME": str(home), "PATH": f"{home / '.local/bin'}:{os.environ.get('PATH', '')}"}
            # An earlier install from a clone that has since moved away.
            alias = home / ".local" / "bin" / "lc"
            alias.parent.mkdir(parents=True)
            alias.symlink_to(home / "old-clone" / "scripts" / "semantic_command.py")
            other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/bin/true"}]}
            stale = {"matcher": "Bash", "hooks": [{"type": "command", "command": f"{home}/old-clone/scripts/semantic_command.py hook"}]}
            (home / ".codex").mkdir()
            (home / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [other, stale]}}))
            result = subprocess.run([sys.executable, str(RUNNER), "install", "--user"], text=True, capture_output=True, env=env, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(alias.resolve(), RUNNER.resolve())
            pre = json.loads((home / ".codex" / "hooks.json").read_text())["hooks"]["PreToolUse"]
            commands = [handler["command"] for group in pre for handler in group["hooks"]]
            self.assertIn("/usr/bin/true", commands)
            self.assertEqual(sum(command.endswith(" hook") for command in commands), 1)
            self.assertNotIn(f"{home}/old-clone/scripts/semantic_command.py hook", commands)

    def test_user_installer_reports_alias_off_path(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            result = subprocess.run(
                [sys.executable, str(RUNNER), "install", "--user"],
                text=True,
                capture_output=True,
                env={**os.environ, "HOME": str(home), "PATH": "/usr/bin:/bin"},
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"lc off PATH: {home / '.local' / 'bin'}", result.stdout)

    def test_user_installer_merges_hooks_json_when_config_only_has_trust_state(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".codex"
            config.mkdir()
            (config / "config.toml").write_text(
                '[hooks.state]\n\n[hooks.state."/tmp/hooks.json:stop:0:0"]\ntrusted_hash = "sha256:abc"\n'
            )
            (config / "hooks.json").write_text(json.dumps({
                "hooks": {
                    "Stop": [{
                        "hooks": [{"type": "command", "command": "/tmp/checkpoint.py"}]
                    }]
                }
            }))
            result = subprocess.run(
                [sys.executable, str(RUNNER), "install", "--user"],
                text=True,
                capture_output=True,
                env={**os.environ, "HOME": str(home)},
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads((config / "hooks.json").read_text())
            self.assertEqual(document["hooks"]["Stop"][0]["hooks"][0]["command"], "/tmp/checkpoint.py")
            self.assertEqual(len(document["hooks"]["PreToolUse"]), 1)

    def test_installer_still_rejects_actual_inline_toml_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ".codex"
            config.mkdir()
            (config / "config.toml").write_text(
                '[[hooks.PreToolUse]]\nmatcher = "^Bash$"\n'
            )
            result = subprocess.run(
                [sys.executable, str(RUNNER), "install", "--workspace", str(root)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already declares TOML hooks", result.stderr)

    def test_feedback_is_bounded_content_free_and_workspace_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            workspace = root / "wanted"
            other = root / "other"
            workspace.mkdir()
            other.mkdir()

            def receipt(identifier, cwd, raw_bytes, view, kind, exit_code=0):
                run = state / identifier
                run.mkdir(parents=True)
                (run / "request.json").write_text(
                    json.dumps({"cwd": str(cwd), "command": "SECRET COMMAND BODY"}), encoding="utf-8"
                )
                (run / "receipt.json").write_text(
                    json.dumps(
                        {
                            "schema": "lazy-semantic-command-receipt/v1",
                            "id": identifier,
                            "output_bytes": raw_bytes,
                            "exit_code": exit_code,
                            "output_limit_reached": False,
                            "kind": kind,
                            "omitted": {"duplicate": 2},
                            "completed_at": "2099-01-01T00:00:00+00:00",
                        }
                    ),
                    encoding="utf-8",
                )
                (run / "view.txt").write_text(view, encoding="utf-8")

            receipt("a" * 16, workspace, 1000, "short\n", "Search")
            receipt("b" * 16, workspace, 100, "failure\n", "Tests", 7)
            receipt("c" * 16, other, 9000, "must not count\n", "Git")
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "feedback",
                    "--workspace",
                    str(workspace),
                    "--since-hours",
                    "24",
                    "--top",
                    "1",
                ],
                text=True,
                capture_output=True,
                env={**os.environ, "LAZY_COMMAND_STATE_ROOT": str(state)},
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("SECRET", result.stdout)
            self.assertNotIn(str(workspace), result.stdout)
            view = json.loads(result.stdout)
            self.assertEqual(view["runs"], 2)
            self.assertEqual(view["rawBytes"], 1100)
            self.assertEqual(view["failures"], 1)
            self.assertEqual(view["byKind"], {"Search": 1, "Tests": 1})
            self.assertEqual(view["omissions"], {"duplicate": 4})
            self.assertEqual([row["id"] for row in view["largestCandidates"]], ["a" * 16])
            self.assertFalse(view["commandBodiesEmitted"])


if __name__ == "__main__":
    unittest.main()
