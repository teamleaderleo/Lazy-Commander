from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from semantic_command import compact_rows, project  # noqa: E402


def reconstruct(units: list[tuple[int, int, str]]) -> list[str]:
    rows = []
    for _start, _end, text in units:
        lines = text.splitlines()
        if lines and lines[0].startswith("prefix "):
            prefix = json.loads(lines[0].removeprefix("prefix "))
            rows.extend(prefix + json.loads(line.removeprefix("+ ")) for line in lines[1:])
        else:
            rows.append(text)
    return rows


class SemanticCommandLocalPrefixTests(unittest.TestCase):
    def test_same_file_rg_rows_factor_the_complete_filename(self):
        rows = [
            "src/worker.py:12:first match",
            "src/worker.py:48:second match",
            "src/worker.py:103:third match",
        ]

        units = compact_rows(rows)

        self.assertEqual(len(units), 1)
        self.assertTrue(units[0][2].startswith('prefix "src/worker.py:"\n'))
        self.assertIn('+ "12:first match"', units[0][2])
        self.assertEqual(reconstruct(units), rows)

    def test_adjacent_path_families_form_separate_local_groups(self):
        rows = [
            "src/alpha.py:1:first",
            "src/alpha.py:2:second",
            "src/alpha.py:3:third",
            "tests/beta.py:7:fourth",
            "tests/beta.py:8:fifth",
            "tests/beta.py:9:sixth",
        ]

        units = compact_rows(rows)
        rendered = "\n".join(text for _start, _end, text in units)

        self.assertIn('prefix "src/alpha.py:"', rendered)
        self.assertIn('prefix "tests/beta.py:"', rendered)
        self.assertEqual(reconstruct(units), rows)

    def test_plain_rows_split_groups_without_distant_prefix_state(self):
        rows = [
            "src/alpha.py:1:first",
            "src/alpha.py:2:second",
            "src/alpha.py:3:third",
            "RESULTS FROM SECOND ROOT",
            "src/alpha.py:4:fourth",
            "src/alpha.py:5:fifth",
            "src/alpha.py:6:sixth",
        ]

        units = compact_rows(rows)
        rendered = "\n".join(text for _start, _end, text in units)

        self.assertEqual(rendered.count('prefix "src/alpha.py:"'), 2)
        self.assertIn("RESULTS FROM SECOND ROOT", rendered)
        self.assertEqual(reconstruct(units), rows)

    def test_whitespace_and_colons_in_paths_remain_exact(self):
        rows = [
            "dir with spaces/a:b.py:4:  leading content",
            "dir with spaces/a:b.py:20:content:with:colons ",
            "dir with spaces/a:b.py:300:\tindented",
        ]

        units = compact_rows(rows)

        self.assertEqual(len(units), 1)
        self.assertTrue(units[0][2].startswith('prefix "dir with spaces/a:b.py:"\n'))
        self.assertEqual(reconstruct(units), rows)

    def test_bounded_projection_keeps_prefix_header_with_all_group_rows(self):
        grouped = [
            f"src/large.py:{number}:match-" + "x" * 100
            for number in range(1, 26)
        ]
        rows = grouped + [
            f"ordinary-{number:03d}-" + "z" * 100
            for number in range(1, 101)
        ]

        payload, omitted, _line_count, _errors = project(
            "rg match src", ("\n".join(rows) + "\n").encode(), 0
        )

        self.assertLessEqual(len(payload), 7_000)
        self.assertIn("lines", omitted)
        self.assertIn('prefix "src/large.py:"', payload)
        for number in range(1, 26):
            self.assertIn(f'+ "{number}:match-', payload)


if __name__ == "__main__":
    unittest.main()
