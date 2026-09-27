from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from git_diff_projection import project_git_diff  # noqa: E402
from semantic_command import project, render  # noqa: E402


def text_file(path: str, hunks: list[tuple[str, list[str]]]) -> list[str]:
    rows = [
        f"diff --git a/{path} b/{path}",
        "index 1111111..2222222 100644",
        f"--- a/{path}",
        f"+++ b/{path}",
    ]
    for header, body in hunks:
        rows.extend((header, *body))
    return rows


class GitDiffProjectionTests(unittest.TestCase):
    def test_complete_small_diff_is_preserved_without_compaction(self):
        rows = text_file(
            "src/worker.py",
            [("@@ -8,2 +8,2 @@ fn run() {", [" unchanged", "-old", "+new"])],
        )
        raw = ("\n".join(rows) + "\n").encode()

        payload, omitted, line_count, errors = project("git diff -- src/worker.py", raw, 0)

        self.assertEqual(payload, "\n".join(rows))
        self.assertEqual(omitted, {})
        self.assertEqual(line_count, len(rows))
        self.assertEqual(errors, 0)
        self.assertNotIn("prefix ", payload)
        self.assertNotIn(' + "+new"', payload)

    def test_large_diff_shows_only_self_contained_whole_hunks(self):
        hunks = []
        for number in range(1, 31):
            body = [
                " context-" + "c" * 90,
                f"-old-{number:02d}-" + "o" * 90,
                f"+new-{number:02d}-" + "n" * 90,
            ]
            hunks.append((f"@@ -{number * 10},2 +{number * 10},2 @@ function_{number}", body))
        rows = text_file("src/large.py", hunks)

        payload, omitted, _line_count, errors = project(
            "git diff -- src/large.py", ("\n".join(rows) + "\n").encode(), 0
        )

        self.assertLessEqual(len(payload), 7_000)
        self.assertGreater(omitted["hunks"], 0)
        self.assertGreater(omitted["lines"], 0)
        self.assertEqual(errors, 0)
        self.assertIn("hunks skipped; use stored expansion", payload)
        blocks = ["diff --git " + block for block in payload.split("diff --git ")[1:]]
        self.assertGreater(len(blocks), 1)
        for block in blocks:
            self.assertIn("--- a/src/large.py", block)
            self.assertIn("+++ b/src/large.py", block)
            self.assertIn("@@ -", block)
        self.assertIn("+new-01-", payload)
        self.assertNotIn('prefix "', payload)

    def test_single_oversized_hunk_is_an_original_contiguous_prefix(self):
        body = [" line-" + str(number).zfill(3) + "-" + "x" * 100 for number in range(80)]
        rows = text_file("src/huge.py", [("@@ -1,80 +1,80 @@ huge", body)])

        payload, omitted, _line_count, _errors = project(
            "git diff", ("\n".join(rows) + "\n").encode(), 0
        )

        self.assertLessEqual(len(payload), 7_000)
        self.assertEqual(omitted["hunks"], 1)
        self.assertIn("partial hunk at the @@ range above", payload)
        shown = payload.split("\n[… partial hunk", 1)[0].splitlines()
        self.assertEqual(shown, rows[:len(shown)])
        self.assertNotIn("line-079", payload)

    def test_oversized_body_line_is_omitted_whole(self):
        body_line = "+identity-start-" + "x" * 8_000 + "-identity-end"
        rows = text_file("src/identity.py", [("@@ -0,0 +1 @@ identity", [body_line])])

        payload, omitted, _line_count, _errors = project(
            "git diff", ("\n".join(rows) + "\n").encode(), 0
        )

        self.assertIn("@@ -0,0 +1 @@ identity", payload)
        self.assertIn("partial hunk", payload)
        self.assertNotIn("identity-start", payload)
        self.assertNotIn("identity-end", payload)
        self.assertEqual(omitted["hunks"], 1)

    def test_selected_rename_mode_and_binary_metadata_remain_verbatim(self):
        metadata = [
            'diff --git "a/old name.txt" "b/new name.txt"',
            "similarity index 100%",
            "rename from old name.txt",
            "rename to new name.txt",
            "diff --git a/script.sh b/script.sh",
            "old mode 100644",
            "new mode 100755",
            "diff --git a/image.bin b/image.bin",
            "new file mode 100644",
            "index 0000000..3333333",
            "Binary files /dev/null and b/image.bin differ",
        ]
        large = text_file(
            "src/large.py",
            [("@@ -1,70 +1,70 @@ large", [" " + "q" * 110 for _ in range(70)])],
        )
        rows = metadata + large

        payload, omitted, _line_count, _errors = project(
            "git diff --find-renames", ("\n".join(rows) + "\n").encode(), 0
        )

        self.assertIn("rename from old name.txt", payload)
        self.assertIn("rename to new name.txt", payload)
        self.assertIn("old mode 100644", payload)
        self.assertIn("new mode 100755", payload)
        self.assertIn("Binary files /dev/null and b/image.bin differ", payload)
        self.assertGreater(omitted["lines"], 0)

    def test_ambiguous_commands_and_unsupported_patch_grammar_decline(self):
        valid = text_file("src/a.py", [("@@ -1 +1 @@", ["-old", "+new"])])
        malformed = text_file("src/a.py", [("@@ -1,2 +1,2 @@", ["-old", "+new"])])
        combined = ["diff --cc src/a.py", "index 111,222..333", "@@@ -1 -1 +1 @@@"]

        self.assertIsNone(project_git_diff("git diff; cat patch", valid, 0, 7_000))
        self.assertIsNone(project_git_diff("git diff", malformed, 0, 7_000))
        self.assertIsNone(project_git_diff("git diff --cc", combined, 0, 7_000))
        self.assertIsNone(project_git_diff("git diff --stat", ["1 file changed"], 0, 7_000))
        self.assertIsNone(project_git_diff("git diff --exit-code", valid, 1, 7_000))
        self.assertIsNotNone(
            project_git_diff("/usr/bin/git -C repo --no-pager diff", valid, 0, 7_000)
        )
        self.assertIsNone(
            project_git_diff(
                "git diff", ["diff --git a/a b/a", "old mode 100644"], 0, 7_000
            )
        )

    def test_omission_receipt_retains_stored_expansion_recovery(self):
        body = [" line-" + str(number) + "-" + "r" * 100 for number in range(80)]
        rows = text_file("src/recover.py", [("@@ -1,80 +1,80 @@ recover", body)])
        payload, omitted, line_count, errors = project(
            "git diff", ("\n".join(rows) + "\n").encode(), 0
        )
        receipt = {
            "exit_code": 0,
            "detected_error_lines": errors,
            "kind": "Git",
            "output_bytes": 12_345,
            "output_lines": line_count,
            "omitted": omitted,
            "id": "0123456789abcdef",
            "output_limit_reached": False,
        }

        view = render(receipt, payload)

        self.assertIn("expand: lc s 0123456789abcdef --find TEXT", view)


if __name__ == "__main__":
    unittest.main()
