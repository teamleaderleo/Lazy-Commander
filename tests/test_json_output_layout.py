from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from json_output_layout import compact_json_layout  # noqa: E402


class JsonOutputLayoutTests(unittest.TestCase):
    def test_preserves_duplicate_keys_numbers_and_token_order(self) -> None:
        source = """{
          "duplicate": -0,
          "duplicate": 1.2300e+04,
          "values": [true, false, null, 0E-0]
        }"""
        expected = (
            '{"duplicate":-0,\n'
            '"duplicate":1.2300e+04,\n'
            '"values":[true,false,null,0E-0]}'
        )
        self.assertEqual(compact_json_layout(source, len(expected)), expected)

    def test_prefers_top_level_array_lines_and_compacts_nested_values(self) -> None:
        source = "[\n  { \"a\": [ 1, 2 ] },\n  [ 3, 4 ],\n  null\n]"
        expected = '[{"a":[1,2]},\n[3,4],\nnull]'
        self.assertEqual(compact_json_layout(source, len(expected)), expected)

    def test_falls_back_to_minified_layout_at_tighter_boundary(self) -> None:
        source = "[  1,  2,  3  ]"
        readable = "[1,\n2,\n3]"
        minified = "[1,2,3]"
        self.assertEqual(compact_json_layout(source, len(readable)), readable)
        self.assertEqual(compact_json_layout(source, len(readable) - 1), minified)
        self.assertEqual(compact_json_layout(source, len(minified)), minified)
        self.assertIsNone(compact_json_layout(source, len(minified) - 1))

    def test_does_not_change_already_small_json(self) -> None:
        source = '{\n  "a": 1,\n  "b": 2\n}'
        self.assertIsNone(compact_json_layout(source, len(source)))
        self.assertIsNone(compact_json_layout(source, len(source) + 100))

    def test_preserves_string_whitespace_and_escape_spelling(self) -> None:
        source = r'''  { "text": "  a\tb  ", "escapes": "\u0061\/\"\\" }  '''
        expected = r'''{"text":"  a\tb  ",
"escapes":"\u0061\/\"\\"}'''
        self.assertEqual(compact_json_layout(source, len(expected)), expected)

    def test_rejects_non_json_even_when_whitespace_removal_would_fit(self) -> None:
        invalid_documents = (
            "  {'single': 1}  ",
            "  {\"trailing\": 1,}  ",
            "  [01]  ",
            "  +1  ",
            "  .1  ",
            "  1.  ",
            "  1e  ",
            "  NaN  ",
            "  Infinity  ",
            "  -Infinity  ",
            "  true false  ",
            '  "bad\\xescape"  ',
        )
        for source in invalid_documents:
            with self.subTest(source=source):
                self.assertIsNone(compact_json_layout(source, len(source) - 1))

    def test_compacts_a_primitive_only_by_removing_outer_whitespace(self) -> None:
        source = ' \n\t "value with spaces" \r\n '
        expected = '"value with spaces"'
        self.assertEqual(compact_json_layout(source, len(expected)), expected)
        self.assertIsNone(compact_json_layout(source, len(expected) - 1))

    def test_empty_containers_are_complete_at_exact_limit(self) -> None:
        self.assertEqual(compact_json_layout(" \n [ ] \n ", 2), "[]")
        self.assertEqual(compact_json_layout(" \n { } \n ", 2), "{}")

    def test_exact_seven_thousand_character_boundary(self) -> None:
        value = '"' + "x" * 6_998 + '"'
        source = " " + value
        self.assertEqual(compact_json_layout(source, 7_000), value)
        self.assertIsNone(compact_json_layout(source, 6_999))


if __name__ == "__main__":
    unittest.main()
