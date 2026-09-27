from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from semantic_command_hook import command_argvs, should_wrap  # noqa: E402


def payload() -> dict[str, object]:
    return {
        "tool_name": "Bash",
        "permission_mode": "dontAsk",
    }


class SemanticCommandHookFidelityTests(unittest.TestCase):
    def test_explicit_terminal_projections_are_left_exact(self):
        commands = (
            "rg -n needle src | head -40",
            "find src -name '*.py' | sed -n '1,40p'",
            "git status --short | tail -n 20",
            "gh pr checks 52 | head -c 7000",
            "gh pr view 52 --json body",
            "gh run list --limit 5 --json databaseId,status",
            "rg --count-matches needle src",
            "rg --max-count 5 needle src",
            "rg -m 5 needle src",
            "rg -l needle src",
            "rg -c needle src",
            "rg -n needle src | jq -R 'split(\":\")'",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(should_wrap(payload(), command))

    def test_noisy_executables_are_still_admitted(self):
        commands = (
            "git status --short && gh pr checks 52 --watch",
            "rg -n needle src; find tests -name '*.py' -print",
            "LC_ALL=C env COLOR=0 python3.14 -m unittest discover -v",
            "codex exec --json 'review this bounded task'",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(should_wrap(payload(), command))

    def test_quoted_labels_comments_and_explicit_runner_are_not_commands(self):
        commands = (
            "printf '%s\\n' ': gh pr view 52 --json body' 'git status'",
            "echo complete # rg -n needle /Users/alice/Projects",
            "lc -r 'git status --short && gh pr checks 52'",
            "lazy-command shell --cwd /tmp 'rg needle src'",
            "bash -lc 'git status --short'",
            "cd /tmp && lc git status && git status --short",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(should_wrap(payload(), command))

    def test_quoted_and_escaped_separators_are_declined(self):
        commands = (
            "printf '%s\\n' ';' git status",
            "printf '%s\\n' '|' rg needle src",
            "printf '%s\\n' \\; git status",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(command_argvs(command))
                self.assertFalse(should_wrap(payload(), command))

    def test_shell_substitutions_are_declined(self):
        commands = (
            "printf '%s\\n' $(git status)",
            "printf '%s\\n' `rg needle src`",
            "diff <(git status) expected.txt",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(command_argvs(command))
                self.assertFalse(should_wrap(payload(), command))

    def test_comment_ends_at_newline_before_real_command(self):
        command = "echo complete # git status\nrg needle src"
        self.assertEqual(
            command_argvs(command),
            [("echo", "complete"), ("rg", "needle", "src")],
        )
        self.assertTrue(should_wrap(payload(), command))

    def test_unsafe_compounds_are_declined_as_a_whole(self):
        commands = (
            "git status --short && git diff -- src/main.py",
            "rg -n needle src; gh pr view 52",
            "git status --short | less",
            "git status --short && tail -f build.log",
            "git status --short && printf 'unterminated",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(should_wrap(payload(), command))

    def test_parser_returns_only_actual_executable_positions(self):
        command = "printf '%s\\n' 'gh pr view 52'; git status --short # rg needle repo"
        self.assertEqual(
            command_argvs(command),
            [
                ("printf", "%s\\n", "gh pr view 52"),
                ("git", "status", "--short"),
            ],
        )

    def test_parser_declines_heredoc_payloads(self):
        command = "python3 - <<'PY'\nprint('git status')\nPY"
        self.assertIsNone(command_argvs(command))
        self.assertFalse(should_wrap(payload(), command))


if __name__ == "__main__":
    unittest.main()
