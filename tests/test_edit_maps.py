from __future__ import annotations

import ast
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).parents[1] / "scripts"


class EditMapTests(unittest.TestCase):
    def test_edit_map_anchors_are_current_top_level_symbols(self):
        mapped = 0
        for path in SCRIPTS.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstring = ast.get_docstring(tree) or ""
            if "Edit map:" not in docstring:
                continue
            mapped += 1
            rows = [row for row in docstring.splitlines() if "->" in row]
            self.assertGreaterEqual(len(rows), 3, f"edit map too small in {path.name}")
            self.assertLessEqual(len(rows), 7, f"edit map too large in {path.name}")
            symbols = {
                node.name
                for node in tree.body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            }
            seen = set()
            for row in rows:
                anchors = [value.strip() for value in row.split("->", 1)[1].split(",")]
                self.assertTrue(anchors, f"empty edit-map row in {path.name}")
                for anchor in anchors:
                    self.assertIn(anchor, symbols, f"stale edit-map anchor {path.name}:{anchor}")
                    self.assertNotIn(anchor, seen, f"duplicate edit-map anchor {path.name}:{anchor}")
                    seen.add(anchor)
        self.assertGreater(mapped, 0)


if __name__ == "__main__":
    unittest.main()
