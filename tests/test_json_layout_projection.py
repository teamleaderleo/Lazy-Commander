from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from semantic_command import project


class JsonLayoutProjectionTests(unittest.TestCase):
    def test_layout_compaction_recovers_every_record_without_omissions(self):
        records = [{"id": f"job-{i:03d}", "result": {"state": "passed", "attempt": i},
                    "url": f"https://example.test/jobs/{i}"} for i in range(48)]
        raw = json.dumps(records, indent=4).encode()
        self.assertGreater(len(raw), 7000)
        view, omitted, lines, errors = project("gh api jobs", raw, 0)
        self.assertLessEqual(len(view), 7000)
        self.assertEqual(json.loads(view), records)
        self.assertEqual(omitted, {})
        self.assertEqual(lines, len(raw.splitlines()))
        self.assertEqual(errors, 0)

    def test_small_json_layout_remains_exact(self):
        raw = b'{\n  "body": "Requested  text\\nnext line",\n  "id": 9007199254740993\n}'
        view, omitted, _, _ = project("gh pr view --json body,id", raw, 0)
        self.assertEqual(view, raw.decode())
        self.assertEqual(omitted, {})

    def test_json_unicode_separators_are_values_not_terminal_formatting(self):
        for padding in (0, 7100):
            text = '{"body":"a\u0085b\u2028c\u2029d"' + ' ' * padding + '}'
            view, omitted, _, _ = project("gh api body", text.encode(), 0)
            self.assertEqual(json.loads(view), json.loads(text))
            self.assertIn("a\u0085b\u2028c\u2029d", view)
            self.assertEqual(omitted, {})

    def test_colored_json_keeps_duplicate_records_and_structure(self):
        text = json.dumps([{"name": "same", "value": [1, 2]}] * 4, indent=2)
        colored = "\x1b[32m" + text.replace("\n", "\x1b[0m\n\x1b[32m") + "\x1b[0m"
        view, omitted, _, _ = project("jq -C .", colored.encode(), 0)
        self.assertEqual(view, text)
        self.assertEqual(omitted, {})

    def test_too_large_document_keeps_existing_bounded_selection(self):
        raw = json.dumps([{"id": i, "body": "x" * 1000} for i in range(20)], indent=2).encode()
        view, omitted, _, _ = project("gh api items", raw, 0)
        self.assertLessEqual(len(view), 7000)
        self.assertIn("lines", omitted)
        self.assertIn('"id": 19', view)


if __name__ == "__main__":
    unittest.main()
