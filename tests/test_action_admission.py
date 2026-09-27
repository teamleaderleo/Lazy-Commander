from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "action_admission.py"
OWNER_WRAPPER = Path(__file__).parents[1] / "scripts" / "owner_profiles.py"
JSON_VIEW = Path(__file__).parents[1] / "scripts" / "json_view.py"
SAFE_SEARCH = Path(__file__).parents[1] / "scripts" / "safe_search.py"
BROWSER_RESULT_VIEW = Path(__file__).parents[1] / "scripts" / "browser_result_view.py"


def request(kind: str, **values):
    return {"schema": "lazy-action-request/v1", "kind": kind, **values}


class ActionAdmissionTests(unittest.TestCase):
    def test_option_named_base_head_is_not_misclassified_as_head_command(self):
        decision = self.decide(
            request(
                "command",
                argv=["python3", "owner_projection.py", "--base-head", "abc123"],
                cwd="/tmp",
                scope={},
            )
        )
        self.assertEqual(decision["route"], "direct")
        self.assertTrue(decision["original_action_executable"])

    def test_exact_owner_profile_wrapper_is_mechanically_observation_only(self):
        decision = self.decide(
            request(
                "command",
                argv=[str(OWNER_WRAPPER), "run", "--profiles", "/private/profile.json"],
                cwd="/tmp",
                scope={"observation_only": True},
            )
        )
        self.assertEqual(decision["route"], "direct")
        self.assertTrue(decision["original_action_executable"])
        lookalike = self.decide(
            request(
                "command",
                argv=["/tmp/owner_profiles.py", "run", "--profiles", "/private/profile.json"],
                cwd="/tmp",
                scope={"observation_only": True},
            )
        )
        self.assertEqual(lookalike["route"], "reconcile-effect")

    def test_exact_json_projector_is_mechanically_observation_only(self):
        for argv in ([str(JSON_VIEW), "/private/input.json"], [sys.executable, str(JSON_VIEW), "/private/input.json"]):
            with self.subTest(argv=argv):
                decision = self.decide(
                    request("command", argv=argv, cwd="/tmp", scope={"observation_only": True})
                )
                self.assertNotEqual(decision["route"], "reconcile-effect")
                self.assertTrue(decision["original_action_executable"])
        lookalike = self.decide(
            request(
                "command",
                argv=[sys.executable, "/tmp/json_view.py", "/private/input.json"],
                cwd="/tmp",
                scope={"observation_only": True},
            )
        )
        self.assertEqual(lookalike["route"], "reconcile-effect")

    def test_exact_safe_search_is_mechanically_observation_only(self):
        argv = [
            sys.executable, str(SAFE_SEARCH), "needle", "/private/src",
            "--max-matches", "20", "--max-line-chars", "200",
            "--max-view-chars", "8000",
        ]
        decision = self.decide(
            request("command", argv=argv, cwd="/tmp", scope={"observation_only": True})
        )
        self.assertNotEqual(decision["route"], "reconcile-effect")
        self.assertTrue(decision["original_action_executable"])
        lookalike = self.decide(
            request(
                "command",
                argv=[sys.executable, "/tmp/safe_search.py", "needle", "/private/src"],
                cwd="/tmp",
                scope={"observation_only": True},
            )
        )
        self.assertEqual(lookalike["route"], "reconcile-effect")

    def test_exact_browser_result_projector_is_mechanically_observation_only(self):
        argv = [sys.executable, str(BROWSER_RESULT_VIEW), "/private/input.json", "--output-dir", "/private/out"]
        decision = self.decide(
            request("command", argv=argv, cwd="/tmp", scope={"observation_only": True})
        )
        self.assertNotEqual(decision["route"], "reconcile-effect")
        self.assertTrue(decision["original_action_executable"])
        lookalike = self.decide(
            request(
                "command",
                argv=[sys.executable, "/tmp/browser_result_view.py", "/private/input.json"],
                cwd="/tmp", scope={"observation_only": True},
            )
        )
        self.assertEqual(lookalike["route"], "reconcile-effect")

    def decide(self, value):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "request.json"
            output = root / "decision.json"
            source.write_text(json.dumps(value), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "decide", "--request", str(source), "--output", str(output)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(output.read_text(encoding="utf-8"))

    def test_routes_waits_reads_verification_and_browser_semantics(self):
        wait = self.decide(request("command", argv=["bash", "-lc", "sleep 25; date -u"], scope={}))
        self.assertEqual(wait["route"], "exact-wake")
        self.assertFalse(wait["original_action_executable"])
        self.assertEqual(wait["next"]["delay_seconds"], 25.0)
        read = self.decide(request("command", argv=["sed", "-n", "1,900p", "SECRET_FILE"], scope={}))
        self.assertEqual(read["route"], "bounded-read")
        bounded_read = self.decide(
            request("command", argv=["bash", "-lc", "rg -n pattern src | head -n 120"], scope={})
        )
        self.assertEqual(bounded_read["route"], "bounded-read")
        unsafe_read = self.decide(
            request("command", argv=["bash", "-lc", "sed -n '1,20p' file; touch changed"], scope={})
        )
        self.assertEqual(unsafe_read["route"], "scoped-observation")
        deferred_effect = self.decide(
            request(
                "command",
                argv=["touch", "changed"],
                scope={"observation_only": True},
            )
        )
        self.assertEqual(deferred_effect["route"], "reconcile-effect")
        verify = self.decide(request("command", argv=[sys.executable, "-m", "unittest", "discover"], scope={}))
        self.assertEqual(verify["route"], "bounded-command")
        git_status = self.decide(request("command", argv=["git", "status", "--short"], scope={}))
        self.assertEqual(git_status["route"], "bounded-command")
        browser = self.decide(
            request(
                "browser-observation",
                operation="domSnapshot",
                scope={"semantic_delta_available": True},
            )
        )
        self.assertEqual(browser["route"], "semantic-delta")
        self.assertNotIn("SECRET_FILE", json.dumps(read))

    def test_run_command_executes_supported_route_through_private_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "request.json"
            output = root / "out"
            source.write_text(
                json.dumps(
                    request(
                        "command",
                        argv=[
                            sys.executable,
                            "-c",
                            "print('noise\\n' * 200, end=''); print('Tests 3 passed'); print('OK')",
                        ],
                        cwd=str(root),
                        scope={},
                    )
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "run-command", "--request", str(source), "--output-dir", str(output)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads((output / "admission-receipt.json").read_text(encoding="utf-8"))
            view = (output / "run" / "view.txt").read_text(encoding="utf-8")
            self.assertTrue(receipt["executed"])
            self.assertIn(receipt["route"], {"direct", "bounded-command"})
            self.assertIn("Tests 3 passed", view)
            self.assertLess(len(view), 10_000)
            for path in (
                output / "decision.json",
                output / "admission-receipt.json",
                output / "run" / "raw.log",
                output / "run" / "view.txt",
                output / "run" / "receipt.json",
            ):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_blocked_wait_is_not_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "request.json"
            output = root / "out"
            source.write_text(
                json.dumps(request("command", argv=["sleep", "2"], cwd=str(root), scope={})),
                encoding="utf-8",
            )
            started = time.monotonic()
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "run-command", "--request", str(source), "--output-dir", str(output)],
                check=False,
                capture_output=True,
                text=True,
            )
            elapsed = time.monotonic() - started
            receipt = json.loads((output / "admission-receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(result.returncode, 0)
            self.assertFalse(receipt["executed"])
            self.assertEqual(receipt["route"], "exact-wake")
            self.assertLess(elapsed, 1.0)
            decision = json.loads((output / "decision.json").read_text(encoding="utf-8"))
            self.assertIn("wake_at", decision["next"])

    def test_run_bounded_read_preserves_prefix_and_honors_request_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_file = root / "source.txt"
            source_file.write_text(
                "".join(f"semantic source line {index}\n" for index in range(500)),
                encoding="utf-8",
            )
            source = root / "request.json"
            output = root / "out"
            source.write_text(
                json.dumps(
                    request(
                        "command",
                        argv=["sed", "-n", "1,500p", "source.txt"],
                        cwd=str(root),
                        scope={},
                    )
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "run-command", "--request", str(source), "--output-dir", str(output)],
                check=False,
                capture_output=True,
                text=True,
                cwd="/",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            admission = json.loads((output / "admission-receipt.json").read_text(encoding="utf-8"))
            bounded = json.loads((output / "run" / "receipt.json").read_text(encoding="utf-8"))
            view = (output / "run" / "view.txt").read_text(encoding="utf-8")
            self.assertEqual(admission["route"], "bounded-read")
            self.assertEqual(bounded["view_mode"], "prefix")
            self.assertEqual(bounded["selected_lines"], 40)
            self.assertEqual(bounded["omitted_lines"], 460)
            self.assertEqual(bounded["view_character_limit"], 8000)
            self.assertLessEqual(len(view), 8000)
            self.assertIn("semantic source line 0", view)
            self.assertNotIn("semantic source line 300", view)

    def test_run_requires_explicit_cwd_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "request.json"
            source.write_text(
                json.dumps(request("command", argv=["true"], scope={})),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "run-command",
                    "--request",
                    str(source),
                    "--output-dir",
                    str(root / "out"),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("explicit absolute cwd", result.stderr)

    def build_history(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        connection.execute(
            """CREATE TABLE thread_items (
                thread_id TEXT, turn_id TEXT, item_id TEXT,
                rollout_ordinal INTEGER, created_at_ms INTEGER,
                item_json TEXT, item_type TEXT
            )"""
        )
        now_ms = int(time.time() * 1000)
        items = [
            (
                "commandExecution",
                {
                    "command": "/bin/bash -lc 'sed -n 1,900p SECRET_FILE'",
                    "aggregatedOutput": "x" * 40_000,
                    "status": "completed",
                },
            ),
            (
                "commandExecution",
                {"command": "/bin/bash -lc 'sleep 25; date -u'", "aggregatedOutput": "done", "status": "completed"},
            ),
            (
                "commandExecution",
                {
                    "command": "/bin/bash -lc 'python3 -m unittest discover'",
                    "aggregatedOutput": "t" * 50_000,
                    "status": "completed",
                },
            ),
            (
                "mcpToolCall",
                {
                    "server": "node_repl",
                    "tool": "js",
                    "arguments": {"code": "await tab.playwright.domSnapshot()"},
                    "result": "d" * 60_000,
                    "status": "completed",
                },
            ),
            (
                "commandExecution",
                {"command": "/usr/bin/custom SECRET_DIRECT", "aggregatedOutput": "m" * 70_000, "status": "completed"},
            ),
        ]
        for ordinal, (item_type, value) in enumerate(items):
            connection.execute(
                "INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("thread", "turn", str(ordinal), ordinal, now_ms + ordinal, json.dumps(value), item_type),
            )
        connection.commit()
        connection.close()

    def test_replay_measures_catches_without_emitting_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.sqlite"
            output = root / "out"
            self.build_history(db)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "replay",
                    "--db",
                    str(db),
                    "--since-days",
                    "1",
                    "--output-dir",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report_text = (output / "replay.json").read_text(encoding="utf-8")
            report = json.loads(report_text)
            self.assertEqual(report["payload"]["large_items"], 4)
            self.assertEqual(report["payload"]["large_items_caught"], 3)
            self.assertEqual(report["payload"]["large_items_missed"], 1)
            self.assertEqual(report["attention"]["exact_wait_routes"], 1)
            self.assertNotIn("SECRET_FILE", report_text)
            self.assertNotIn("SECRET_DIRECT", report_text)
            for name in ("replay.json", "view.md", "receipt.json"):
                self.assertEqual(stat.S_IMODE((output / name).stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
