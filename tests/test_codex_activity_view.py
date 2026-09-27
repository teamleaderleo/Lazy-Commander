from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "codex_activity_view.py"


def item(item_type: str, **values):
    return item_type, json.dumps({"type": item_type, **values})


class CodexActivityViewTests(unittest.TestCase):
    def build_db(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        connection.execute(
            """CREATE TABLE thread_items (
                thread_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                rollout_ordinal INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL,
                item_json TEXT NOT NULL,
                item_type TEXT NOT NULL
            )"""
        )
        rows = [
            item(
                "commandExecution",
                command="/bin/bash -lc 'sleep 25; date -u SECRET_ARGUMENT'",
                status="completed",
                aggregatedOutput="timestamp\n",
                durationMs=25_000,
                commandActions=[{"type": "unknown", "command": "secret"}],
            ),
            item(
                "commandExecution",
                command="/bin/bash -lc 'sleep 25; date -u SECRET_ARGUMENT'",
                status="completed",
                aggregatedOutput="timestamp\n",
                durationMs=25_000,
                commandActions=[],
            ),
            item(
                "commandExecution",
                command="/bin/bash -lc 'sed -n 1,999p SECRET_FILE'",
                status="completed",
                aggregatedOutput="x" * 40_000,
                durationMs=20,
                commandActions=[{"type": "read", "path": "SECRET_FILE"}],
            ),
            item(
                "commandExecution",
                command="/bin/bash -lc 'false SECRET_FAILURE'",
                status="failed",
                aggregatedOutput="",
                durationMs=60_000,
                commandActions=[],
            ),
            item(
                "mcpToolCall",
                server="node_repl",
                tool="js",
                status="completed",
                result="y" * 50_000,
                durationMs=1_000,
            ),
            item(
                "mcpToolCall",
                server="node_repl",
                tool="js",
                status="completed",
                result={
                    "content": [{"type": "text", "text": "z" * 100}],
                    "structuredContent": None,
                    "_meta": {"uiEnvelope": "m" * 40_000},
                },
                durationMs=10,
            ),
            item(
                "fileChange",
                status="completed",
                changes=[{
                    "path": "SECRET_ARTIFACT_PATH",
                    "kind": {"type": "add"},
                    "diff": "SECRET_ARTIFACT_BODY" + "q" * 40_000,
                }],
            ),
            item("reasoning", content="SECRET_REASONING"),
        ]
        for ordinal, (item_type, item_json) in enumerate(rows):
            connection.execute(
                "INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("thread-1", "turn-1", f"item-{ordinal}", ordinal, 1000 + ordinal, item_json, item_type),
            )
        connection.commit()
        connection.close()

    def test_projects_real_activity_without_raw_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.sqlite"
            output = root / "out"
            self.build_db(db)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--db",
                    str(db),
                    "--thread-id",
                    "thread-1",
                    "--output-dir",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report_text = (output / "activity.json").read_text(encoding="utf-8")
            report = json.loads(report_text)
            view = (output / "view.md").read_text(encoding="utf-8")
            self.assertEqual(report["commands"]["runs"], 4)
            self.assertEqual(report["commands"]["exact_repeat_runs"], 1)
            self.assertEqual(report["commands"]["wait_runs"], 2)
            self.assertEqual(report["commands"]["failed_runs"], 1)
            self.assertEqual(report["commands"]["large_payload_runs"], 1)
            self.assertEqual(report["tools"]["large_payload_runs"], 1)
            self.assertEqual(report["tools"]["large_envelope_runs"], 1)
            self.assertGreater(report["attention"]["tool_envelope_chars"], 40_000)
            self.assertEqual(report["file_changes"]["runs"], 1)
            self.assertGreater(report["attention"]["file_change_payload_chars"], 40_000)
            self.assertGreater(
                report["attention"]["semantic_payload_chars"],
                report["attention"]["tool_and_command_payload_chars"],
            )
            self.assertIn("large-file-change", view)
            self.assertIn("large-command-output", view)
            self.assertIn("large-tool-output", view)
            self.assertNotIn("SECRET_ARGUMENT", report_text)
            self.assertNotIn("SECRET_FILE", report_text)
            self.assertNotIn("SECRET_REASONING", report_text)
            self.assertNotIn("SECRET_ARTIFACT_PATH", report_text)
            self.assertNotIn("SECRET_ARTIFACT_BODY", report_text)
            for name in ("activity.json", "view.md", "receipt.json"):
                self.assertEqual(stat.S_IMODE((output / name).stat().st_mode), 0o600)

    def test_refuses_to_overwrite_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.sqlite"
            output = root / "out"
            self.build_db(db)
            command = [
                sys.executable,
                str(SCRIPT),
                "--db",
                str(db),
                "--output-dir",
                str(output),
            ]
            first = subprocess.run(command, check=False, capture_output=True, text=True)
            second = subprocess.run(command, check=False, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("refusing to overwrite", second.stderr)

    def test_exact_time_window_is_lower_inclusive_and_upper_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.sqlite"
            output = root / "out"
            self.build_db(db)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--db",
                    str(db),
                    "--thread-id",
                    "thread-1",
                    "--since-at",
                    "1970-01-01T00:00:01.002Z",
                    "--until-at",
                    "1970-01-01T00:00:01.005Z",
                    "--output-dir",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((output / "activity.json").read_text(encoding="utf-8"))
            self.assertEqual(report["window"]["items"], 3)
            self.assertEqual(report["commands"]["runs"], 2)
            self.assertEqual(report["tools"]["runs"], 1)
            self.assertEqual(report["selector"]["since_at"], "1970-01-01T00:00:01.002Z")
            self.assertEqual(report["selector"]["until_at"], "1970-01-01T00:00:01.005Z")


if __name__ == "__main__":
    unittest.main()
