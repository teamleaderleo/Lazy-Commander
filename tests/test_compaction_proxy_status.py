from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from compaction_proxy_status import latest_status
from compaction_app_server_proxy import (
    record_runtime_observation,
    record_runtime_start,
    record_runtime_stop,
)


class CompactionProxyStatusTests(unittest.TestCase):
    def write_start(self, root: Path, *, started: int, proxy_pid: int, child_pid: int):
        runtime_id = f"runtime-{started}"
        path = root / "runtimes" / f"{runtime_id}-started.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": "lazy-compaction-proxy-runtime/v1",
                    "runtimeId": runtime_id,
                    "state": "started",
                    "startedAtUnixNs": started,
                    "proxyPid": proxy_pid,
                    "childPid": child_pid,
                    "realCodexVersion": "codex-cli 0.151.0",
                    "apply": True,
                }
            ),
            encoding="utf-8",
        )
        return runtime_id

    def test_missing_runtime_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(latest_status(Path(tmp))["state"], "not-started")

    def test_single_live_runtime_is_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_id = self.write_start(
                root, started=1, proxy_pid=os.getpid(), child_pid=os.getpid()
            )
            status = latest_status(root)
            self.assertEqual(status["state"], "active")
            self.assertEqual(status["runtimeId"], runtime_id)
            self.assertTrue(status["apply"])
            self.assertEqual(status["realCodexVersion"], "codex-cli 0.151.0")
            self.assertFalse(status["rootThreadObserved"])
            self.assertFalse(status["rootIdleObserved"])

    def test_root_runtime_wins_over_newer_active_rootless_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root_runtime = self.write_start(
                root, started=1, proxy_pid=os.getpid(), child_pid=os.getpid()
            )
            self.write_start(root, started=2, proxy_pid=os.getpid(), child_pid=os.getpid())
            record_runtime_observation(root, root_runtime, "root-thread", "private-root")

            status = latest_status(root)
            self.assertEqual(status["state"], "active")
            self.assertEqual(status["runtimeId"], root_runtime)
            self.assertTrue(status["rootThreadObserved"])
            self.assertFalse(status["rootIdleObserved"])

    def test_active_rootless_runtime_wins_over_stopped_root_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stopped_root = self.write_start(
                root, started=1, proxy_pid=os.getpid(), child_pid=os.getpid()
            )
            record_runtime_observation(root, stopped_root, "root-thread", "private-root")
            record_runtime_observation(root, stopped_root, "root-idle", "private-root")
            record_runtime_stop(root, stopped_root, 0)
            active_rootless = self.write_start(
                root, started=2, proxy_pid=os.getpid(), child_pid=os.getpid()
            )

            status = latest_status(root)
            self.assertEqual(status["state"], "active")
            self.assertEqual(status["runtimeId"], active_rootless)
            self.assertFalse(status["rootThreadObserved"])
            self.assertFalse(status["rootIdleObserved"])

    def test_active_rootless_runtime_wins_over_dead_root_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dead_root = self.write_start(
                root, started=1, proxy_pid=999_999_999, child_pid=999_999_999
            )
            record_runtime_observation(root, dead_root, "root-thread", "private-root")
            record_runtime_observation(root, dead_root, "root-idle", "private-root")
            active_rootless = self.write_start(
                root, started=2, proxy_pid=os.getpid(), child_pid=os.getpid()
            )

            status = latest_status(root)
            self.assertEqual(status["state"], "active")
            self.assertEqual(status["runtimeId"], active_rootless)
            self.assertFalse(status["rootThreadObserved"])
            self.assertFalse(status["rootIdleObserved"])

    def test_latest_live_runtime_is_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_start(root, started=1, proxy_pid=999_999_999, child_pid=999_999_999)
            self.write_start(root, started=2, proxy_pid=os.getpid(), child_pid=os.getpid())
            status = latest_status(root)
            self.assertEqual(status["state"], "active")
            self.assertTrue(status["apply"])
            self.assertEqual(status["realCodexVersion"], "codex-cli 0.151.0")
            self.assertFalse(status["rootThreadObserved"])
            self.assertFalse(status["rootIdleObserved"])

    def test_stop_receipt_wins_over_live_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_id = self.write_start(
                root, started=1, proxy_pid=os.getpid(), child_pid=os.getpid()
            )
            (root / "runtimes" / f"{runtime_id}-stopped.json").write_text("{}", encoding="utf-8")
            self.assertEqual(latest_status(root)["state"], "stopped")

    def test_producer_receipts_round_trip_through_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("compaction_app_server_proxy._codex_version", return_value="codex-cli 0.151.0"):
                runtime_id, _ = record_runtime_start(
                    root,
                    "/opt/codex",
                    os.getpid(),
                    apply=True,
                    now_ns=42,
                )
            self.assertEqual(latest_status(root)["state"], "active")
            record_runtime_observation(root, runtime_id, "root-thread", "private-root")
            record_runtime_observation(root, runtime_id, "root-idle", "private-root")
            status = latest_status(root)
            self.assertTrue(status["rootThreadObserved"])
            self.assertTrue(status["rootIdleObserved"])
            record_runtime_stop(root, runtime_id, 0)
            self.assertEqual(latest_status(root)["state"], "stopped")

    def test_corrupt_observation_does_not_prove_root_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_id = self.write_start(
                root, started=1, proxy_pid=os.getpid(), child_pid=os.getpid()
            )
            (root / "runtimes" / f"{runtime_id}-root-idle.json").write_text(
                "{}", encoding="utf-8"
            )
            self.assertFalse(latest_status(root)["rootIdleObserved"])


if __name__ == "__main__":
    unittest.main()
