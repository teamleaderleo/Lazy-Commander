from __future__ import annotations

import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "browser_result_view.py"


def packet(**overrides):
    value = {
        "schema": "lazy-browser-result-input/v1",
        "operationId": "op-1",
        "attemptId": "attempt-1",
        "actionClass": "observation",
        "outcome": "success",
        "settlement": "not-applicable",
        "result": {
            "content": [{"type": "text", "text": "semantic sentinel"}],
            "prompt": "private prompt",
            "url": "https://private.example/path",
            "screenshotData": "x" * 100_000,
            "headers": {"x-private": "transport sentinel"},
        },
    }
    value.update(overrides)
    return value


class BrowserResultViewTests(unittest.TestCase):
    def run_view(self, value, *extra, root=None):
        root_path = Path(root) if root else Path(tempfile.mkdtemp())
        source = root_path / "input.json"
        output = root_path / "out"
        source.write_text(json.dumps(value), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(source), "--output-dir", str(output), *extra],
            check=False, capture_output=True, text=True,
        )
        return result, output

    def test_default_view_omits_all_bodies_and_counts_large_classes(self):
        value = packet(result={
            "text": "s" * 200_001,
            "prompt": "p" * 30_001,
            "url": "https://private.example/secret",
            "screenshotData": "b" * 100_001,
            "headers": {"authorization": "transport-secret"},
        })
        with tempfile.TemporaryDirectory() as directory:
            result, output = self.run_view(value, root=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            view_text = (output / "view.json").read_text(encoding="utf-8")
            view = json.loads(view_text)
            self.assertFalse(view["resultBodiesEmitted"])
            self.assertIsNone(view["contentBudget"])
            self.assertGreater(view["stringClasses"]["semantic"]["chars"], 200_000)
            self.assertGreater(view["stringClasses"]["binary"]["chars"], 100_000)
            for sentinel in ("transport-secret", "private.example", "ssssssss", "pppppppp", "bbbbbbbb"):
                self.assertNotIn(sentinel, view_text)
            self.assertIn("transport-secret", (output / "raw.json").read_text(encoding="utf-8"))
            for path in (output, output / "raw.json", output / "view.json"):
                expected = 0o700 if path.is_dir() else 0o600
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected)

    def test_timed_out_effect_stays_ambiguous_and_never_authorizes_retry(self):
        value = packet(actionClass="effect", outcome="timeout", settlement="ambiguous")
        with tempfile.TemporaryDirectory() as directory:
            result, output = self.run_view(value, root=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            effect = json.loads((output / "view.json").read_text())["effect"]
            self.assertTrue(effect["reconciliationRequired"])
            self.assertFalse(effect["authoritativelySettled"])
            self.assertFalse(effect["retryAuthorized"])

    def test_authoritative_effect_settlement_requires_and_projects_receipt(self):
        value = packet(
            actionClass="effect", settlement="committed",
            receipt={"authority": "visible-chat", "receiptId": "message-42"},
        )
        with tempfile.TemporaryDirectory() as directory:
            result, output = self.run_view(value, root=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            effect = json.loads((output / "view.json").read_text())["effect"]
            self.assertTrue(effect["authoritativelySettled"])
            self.assertEqual(effect["receipt"]["receiptId"], "message-42")
        invalid = packet(actionClass="effect", settlement="committed")
        with tempfile.TemporaryDirectory() as directory:
            result, _ = self.run_view(invalid, root=directory)
            self.assertNotEqual(result.returncode, 0)

    def test_consumer_budget_is_explicit_and_replay_is_stable(self):
        value = packet(result={"answer": "abcdefghij"})
        args = ("--consumer-id", "unit-test", "--content-pointer", "/answer", "--max-content-chars", "4")
        with tempfile.TemporaryDirectory() as directory:
            result, output = self.run_view(value, *args, root=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            first = (output / "view.json").read_bytes()
            result, _ = self.run_view(value, *args, root=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(first, (output / "view.json").read_bytes())
            view = json.loads(first)
            self.assertEqual(view["content"], "abcd")
            self.assertEqual(view["contentBudget"]["consumerId"], "unit-test")
            self.assertEqual(view["contentBudget"]["maxChars"], 4)


if __name__ == "__main__":
    unittest.main()
