from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "safe_search.py"


class SafeSearchTests(unittest.TestCase):
    def run_search(self, root: Path, pattern: str, *options: str):
        return subprocess.run(
            [sys.executable, str(SCRIPT), pattern, str(root), *options],
            check=False,
            capture_output=True,
            text=True,
        )

    def decode_matches(self, view):
        encoding = view["matchEncoding"]
        result = []
        for group in encoding["groups"]:
            path = encoding["pathPrefix"] + group["pathSuffix"]
            for line, text_suffix, omitted_before, omitted_after in group["rows"]:
                result.append(
                    {
                        "path": path,
                        "line": line,
                        "text": group["textPrefix"] + text_suffix,
                        "charactersOmittedBefore": omitted_before,
                        "charactersOmittedAfter": omitted_after,
                    }
                )
        return result

    def test_minified_line_is_truncated_inside_explicit_consumer_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tail = "SECRET_TAIL_SENTINEL"
            (root / "bundle.js").write_text("needle=" + "x" * 200_000 + tail, encoding="utf-8")
            result = self.run_search(
                root, "needle", "--max-matches", "5", "--max-line-chars", "80", "--max-view-chars", "1800"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLessEqual(len(result.stdout), 1800)
            self.assertNotIn(tail, result.stdout)
            view = json.loads(result.stdout)
            self.assertGreater(view["omissions"]["lineCharacters"], 199_000)
            self.assertEqual(view["budgetsApplied"]["maxLineCharacters"], 80)
            self.assertFalse(view["rawDocumentEmitted"])

    def test_minified_match_neighborhood_is_retained_not_line_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bundle.js").write_text(
                "界" * 10_000 + "before-thread/compact/start-after" + "z" * 10_000,
                encoding="utf-8",
            )
            result = self.run_search(
                root,
                "thread/compact/start",
                "--max-matches",
                "2",
                "--max-line-chars",
                "80",
                "--max-view-chars",
                "2000",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            row = self.decode_matches(json.loads(result.stdout))[0]
            self.assertIn("thread/compact/start", row["text"])
            self.assertGreater(row["charactersOmittedBefore"], 9_000)
            self.assertGreater(row["charactersOmittedAfter"], 9_000)

    def test_match_limit_stops_search_and_query_body_is_hashed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "many.txt").write_text("\n".join(f"private-pattern row {i}" for i in range(30)), encoding="utf-8")
            result = self.run_search(
                root, "private-pattern", "--max-matches", "3", "--max-line-chars", "100", "--max-view-chars", "3000"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            view = json.loads(result.stdout)
            self.assertEqual(view["selectedMatches"], 3)
            self.assertEqual(len(self.decode_matches(view)), 3)
            self.assertTrue(view["searchStoppedEarly"])
            self.assertNotIn("private-pattern", view["querySha256"])
            self.assertFalse(view["authorizesWork"])

    def test_no_match_is_a_valid_empty_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.txt").write_text("nothing here", encoding="utf-8")
            result = self.run_search(
                root, "absent", "--max-matches", "2", "--max-line-chars", "80", "--max-view-chars", "2000"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            view = json.loads(result.stdout)
            self.assertEqual(view["selectedMatches"], 0)
            self.assertEqual(self.decode_matches(view), [])

    def test_repeated_path_and_text_prefixes_are_lossless_and_factored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "state" / "runs" / "first.jsonl"
            second = root / "state" / "runs" / "second.jsonl"
            first.parent.mkdir(parents=True)
            common = '{"timestamp":"2026-08-31T03:56:03Z","cwd":"/home/alice/Documents/Codex/'
            first.write_text(common + 'alpha"}\n' + common + 'beta"}\n', encoding="utf-8")
            second.write_text(common + 'gamma"}\n', encoding="utf-8")
            result = self.run_search(
                root, "timestamp", "--max-matches", "5", "--max-line-chars", "200", "--max-view-chars", "4000"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            view = json.loads(result.stdout)
            decoded = self.decode_matches(view)
            self.assertEqual([row["text"] for row in decoded], [common + 'alpha"}', common + 'beta"}', common + 'gamma"}'])
            self.assertTrue(view["matchEncoding"]["pathPrefix"])
            self.assertTrue(view["matchEncoding"]["groups"][0]["textPrefix"])
            self.assertEqual(view["schema"], "lazy-safe-search-view/v3")
            self.assertTrue(view["selectedMatchesLosslesslyEncoded"])
            flat = json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            compact = json.dumps(view["matchEncoding"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            self.assertLess(len(compact), len(flat))

    def test_near_duplicates_preserve_differing_suffixes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rows.txt").write_text("needle same A\nneedle same B\nneedle unlike C\n", encoding="utf-8")
            result = self.run_search(
                root, "needle", "--max-matches", "5", "--max-line-chars", "100", "--max-view-chars", "3000"
            )
            decoded = self.decode_matches(json.loads(result.stdout))
            self.assertEqual([row["text"] for row in decoded], ["needle same A", "needle same B", "needle unlike C"])


if __name__ == "__main__":
    unittest.main()
