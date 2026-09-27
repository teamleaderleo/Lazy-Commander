from __future__ import annotations

import re
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from semantic_command import compact_rows, project, render, repeated_units


class CompactLanguageTests(unittest.TestCase):
    def reconstruct(self, units):
        result = []
        for _start, _end, text in units:
            block = re.match(r"repeat ×(\d+) · (\d+) lines\n", text)
            single = re.search(r"\n\[repeat ×(\d+)\]$", text)
            if block:
                rows = text[block.end():].split("\n")
                self.assertEqual(len(rows), int(block[2]))
                result.extend(rows * int(block[1]))
            elif single:
                result.extend([text[:single.start()]] * int(single[1]))
            else:
                result.append(text)
        return result

    def test_repeated_terminal_snapshot_is_local_and_lossless(self):
        snapshot = ["run 12345 · branch codex/example", "  ✓ build (job 123)", "  * verify (job 456)", ""]
        final = ["run 12345 · branch codex/example", "  ✓ build (job 123)", "  X verify (job 456)"]
        rows = snapshot * 8 + final
        units = repeated_units(rows)
        self.assertEqual(self.reconstruct(units), rows)
        self.assertTrue(units[0][2].startswith("repeat ×8 · 4 lines\n"))
        payload, omitted, _, _ = project("gh run watch 12345", "\n".join(rows).encode(), 1)
        self.assertIn("  X verify (job 456)", payload)
        self.assertEqual(omitted, {"repeat": 28})

    def test_repeated_diagnostic_block_retains_frames_and_total(self):
        block = ["Traceback (most recent call last):", '  File "/app/worker.py", line 81, in run', "    execute(job)", "RuntimeError: backend unavailable"]
        rows = ["before"] + block * 3 + ["after"]
        units = repeated_units(rows)
        self.assertEqual(self.reconstruct(units), rows)
        self.assertIn("repeat ×3 · 4 lines", "\n".join(unit[2] for unit in units))

    def test_json_records_and_nonadjacent_repeats_remain_distinct(self):
        for rows in (["header", '{"event":"same"}'] * 4,
                     ["same long message", "different", "same long message"],
                     ["a", "b"] * 2):
            units = repeated_units(rows)
            self.assertEqual(self.reconstruct(units), rows)
            self.assertEqual("\n".join(unit[2] for unit in units), "\n".join(rows))

    def test_bounded_selection_never_separates_repeat_count_and_block(self):
        block = ["poll status stable run/123 " + "x" * 130, "  verify pending job/456"]
        rows = block * 5 + [f"ordinary {n} " + "y" * 90 for n in range(150)]
        payload, _, _, _ = project("gh run watch 123", "\n".join(rows).encode(), 0)
        self.assertLessEqual(len(payload), 7000)
        if "repeat ×5" in payload:
            self.assertIn("repeat ×5 · 2 lines\n" + "\n".join(block), payload)

    def test_complete_receipt_has_one_identity_and_no_routine_footer(self):
        receipt = {"exit_code": 0, "detected_error_lines": 3, "kind": "Command",
                   "id": "0123456789abcdef", "omitted": {}, "output_bytes": 64_000_000,
                   "output_lines": 10000, "output_limit_reached": False}
        output = render(receipt, "requested value")
        self.assertEqual(output, "OK · Command · id 0123456789abcdef\nrequested value\n")
        self.assertIn("OK · Command", render(receipt, "Documentation explains a failure mode"))
        receipt["exit_code"] = 7
        self.assertTrue(render(receipt, "").startswith("FAIL 7"))
        receipt["omitted"] = {"lines": 9990}
        receipt["output_limit_reached"] = True
        output = render(receipt, "[… lines 1–9990 omitted …]")
        self.assertIn("--find TEXT (or --lines START:END)", output)
        self.assertNotIn("lc s -r", output)
        self.assertIn("raw output is incomplete", output)

    def test_literal_notation_is_not_mistaken_for_a_generated_block(self):
        rows = ['repeat ×3 · 2 lines', 'a', 'b', 'prefix "/repo/"', '+ "a"', '+ "b"',
                '[repeat ×9]', 'literal "already literal"']
        units = compact_rows(rows)
        decoded = self.reconstruct(units)
        decoded = [json.loads(row[8:]) if row.startswith("literal ") else row for row in decoded]
        self.assertEqual(decoded, rows)
        self.assertTrue(units[0][2].startswith('literal "repeat ×3'))

    def test_repeated_literal_markers_decode_one_layer_only(self):
        row = '[repeat ×3]'
        units = compact_rows([row] * 8)
        decoded = self.reconstruct(units)
        self.assertEqual([json.loads(item[8:]) for item in decoded], [row] * 8)


if __name__ == "__main__":
    unittest.main()
