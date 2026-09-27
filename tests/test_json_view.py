from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "json_view.py"


def run_view(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path), *args],
        check=False,
        capture_output=True,
        text=True,
    )


class JsonViewTests(unittest.TestCase):
    def test_summary_emits_structure_but_no_scalar_or_array_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = "never-emit-this-secret"
            source = Path(directory) / "source.json"
            source.write_text(
                json.dumps(
                    {
                        "name": secret,
                        "enabled": True,
                        "items": [{"token": secret}, secret, 17, None],
                        "nested": {"answer": 42},
                    }
                ),
                encoding="utf-8",
            )
            result = run_view(source)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(secret, result.stdout)
            view = json.loads(result.stdout)
            self.assertFalse(view["rawDocumentEmitted"])
            self.assertFalse(view["scalarBodiesEmitted"])
            items = next(item for item in view["projection"]["keys"] if item["key"] == "items")
            self.assertEqual(
                items["node"],
                {
                    "type": "array",
                    "count": 4,
                    "itemTypes": {"object": 1, "string": 1, "number": 1, "null": 1},
                    "valuesEmitted": 0,
                },
            )

    def test_exact_pointer_never_emits_scalar_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            source.write_text('{"private":{"token":"swordfish"}}', encoding="utf-8")
            result = run_view(source, "--pointer", "/private/token")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("swordfish", result.stdout)
            view = json.loads(result.stdout)
            self.assertEqual(view["mode"], "pointer")
            self.assertEqual(view["projection"], {"type": "string"})

    def test_invalid_json_reports_bounded_metadata_without_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "broken.json"
            source.write_text('{"secret":"do-not-repeat",}', encoding="utf-8")
            result = run_view(source)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("do-not-repeat", result.stdout)
            view = json.loads(result.stdout)
            self.assertFalse(view["parsed"])
            self.assertEqual(view["error"]["type"], "invalid_json")
            self.assertEqual(set(view["error"]), {"type", "line", "column", "offset"})

    def test_limits_report_key_and_depth_omissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            source.write_text(
                json.dumps({f"key-{index}": {"deep": index} for index in range(5)}),
                encoding="utf-8",
            )
            result = run_view(source, "--max-keys", "2", "--max-depth", "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            view = json.loads(result.stdout)
            self.assertEqual(view["projection"]["omittedKeys"], 3)
            self.assertTrue(
                all(item["node"]["depthTruncated"] for item in view["projection"]["keys"])
            )

    def test_invalid_pointer_does_not_echo_pointer_or_source_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            source.write_text('{"secret":"private-value"}', encoding="utf-8")
            result = run_view(source, "--pointer", "/missing-private-name")
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("private-value", result.stdout)
            self.assertNotIn("missing-private-name", result.stdout)
            view = json.loads(result.stdout)
            self.assertEqual(view["error"]["type"], "invalid_pointer")


if __name__ == "__main__":
    unittest.main()
