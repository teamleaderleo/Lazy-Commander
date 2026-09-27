from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from compaction_app_server_proxy import AsyncClassifier, DecisionController
from compaction_owner_checkpoint import record
from compaction_thread_listener import ThreadListener


def checkpoint(thread_ref="root-one", **overrides):
    value = {
        "schema": "lazy-compaction-owner-checkpoint/v1",
        "threadRef": thread_ref,
        "checkpointGeneration": 1,
        "durableCheckpointReady": True,
        "nextActionPresent": True,
        "semanticBoundary": "yes",
        "effectState": "settled",
    }
    value.update(overrides)
    return value


def wake(thread_ref="root-one", turn_ref="turn-one", **overrides):
    value = {
        "schema": "lazy-compaction-thread-wake/v1",
        "threadRef": thread_ref,
        "turnRef": turn_ref,
        "event": "root-turn-idle",
        "status": "idle",
        "usedTokens": 50_000,
        "contextWindow": 272_000,
        "goalStatus": "active",
        "authorizesCompaction": False,
        "rawConversationRequired": False,
    }
    value.update(overrides)
    return value


def note(method, **params):
    return {"jsonrpc": "2.0", "method": method, "params": params}


class CompactionAppServerProxyTests(unittest.TestCase):
    def idle_listener(self):
        listener = ThreadListener()
        listener.observe(
            note(
                "thread/started",
                thread={"id": "root-one", "parentThreadId": None, "status": {"type": "active"}},
            )
        )
        listener.observe(note("turn/started", threadId="root-one", turn={"id": "turn-one"}))
        listener.observe(
            note(
                "turn/completed",
                threadId="root-one",
                turn={"id": "turn-one", "status": "completed"},
            )
        )
        listener.observe(note("thread/status/changed", threadId="root-one", status={"type": "idle"}))
        return listener

    def wait_classifier(self, classifier):
        deadline = time.monotonic() + 2
        while classifier.pending and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(classifier.pending)

    def test_observe_mode_records_but_never_injects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record(checkpoint(), root)
            decision, request = DecisionController(root, apply=False).evaluate(wake())
            self.assertEqual(decision["action"], "compact")
            self.assertFalse(decision["authorizesCompaction"])
            self.assertIsNone(request)

    def test_apply_mode_issues_once_and_settles_full_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record(checkpoint(), root)
            controller = DecisionController(root, apply=True)
            decision, request = controller.evaluate(wake())
            self.assertTrue(decision["authorizesCompaction"])
            self.assertEqual(request["method"], "thread/compact/start")
            self.assertIsNotNone(request)
            self.assertIsNone(controller.evaluate(wake())[1])

            self.assertTrue(
                controller.observe_response(
                    {"jsonrpc": "2.0", "id": request["id"], "result": {}}
                )
            )
            controller.observe_notification(
                note(
                    "item/started",
                    threadId="root-one",
                    turnId="compact-turn",
                    item={"id": "compact-item", "type": "contextCompaction"},
                )
            )
            controller.observe_notification(
                note(
                    "item/completed",
                    threadId="root-one",
                    turnId="compact-turn",
                    item={"id": "compact-item", "type": "contextCompaction"},
                )
            )
            controller.observe_notification(
                note(
                    "turn/completed",
                    threadId="root-one",
                    turn={"id": "compact-turn", "status": "completed"},
                )
            )
            controller.observe_notification(
                note("thread/status/changed", threadId="root-one", status={"type": "idle"})
            )
            self.assertNotIn("root-one", controller.inflight)
            committed = list((root / "effects").glob("*-committed.json"))
            self.assertEqual(len(committed), 1)

            decision, request = controller.evaluate(wake(turn_ref="turn-two"))
            self.assertEqual(decision["action"], "checkpoint-now")
            self.assertIsNone(request)

    def test_restart_with_issued_effect_reconciles_and_never_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record(checkpoint(), root)
            _, request = DecisionController(root, apply=True).evaluate(wake())
            self.assertIsNotNone(request)
            restarted = DecisionController(root, apply=True)
            decision, replay = restarted.evaluate(wake(turn_ref="turn-two"))
            self.assertEqual(decision["action"], "reconcile-effect")
            self.assertIsNone(replay)

    def test_effect_settles_when_idle_precedes_compaction_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record(checkpoint(), root)
            controller = DecisionController(root, apply=True)
            _, request = controller.evaluate(wake())
            controller.observe_response({"id": request["id"], "result": {}})
            controller.observe_notification(
                note(
                    "item/started",
                    threadId="root-one",
                    turnId="compact-turn",
                    item={"id": "compact-item", "type": "contextCompaction"},
                )
            )
            controller.observe_notification(
                note("thread/status/changed", threadId="root-one", status={"type": "idle"})
            )
            controller.observe_notification(
                note(
                    "item/completed",
                    threadId="root-one",
                    turnId="compact-turn",
                    item={"id": "compact-item", "type": "contextCompaction"},
                )
            )
            self.assertIn("root-one", controller.inflight)
            controller.observe_notification(
                note(
                    "turn/completed",
                    threadId="root-one",
                    turn={"id": "compact-turn", "status": "completed"},
                )
            )
            self.assertNotIn("root-one", controller.inflight)
            self.assertEqual(len(list((root / "effects").glob("*-committed.json"))), 1)

    def test_request_error_is_ambiguous_and_never_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record(checkpoint(), root)
            controller = DecisionController(root, apply=True)
            _, request = controller.evaluate(wake())
            controller.observe_response(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "error": {"code": -32000, "message": "active"},
                }
            )
            self.assertEqual(len(list((root / "effects").glob("*-ambiguous.json"))), 1)
            self.assertIsNone(controller.evaluate(wake(turn_ref="turn-two"))[1])

    def test_no_checkpoint_and_unsettled_owner_effect_are_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision, request = DecisionController(root, apply=True).evaluate(wake())
            self.assertEqual(decision["reasonCodes"], ["no-owner-checkpoint"])
            self.assertIsNone(request)
            record(checkpoint(effectState="ambiguous"), root)
            decision, request = DecisionController(root, apply=True).evaluate(wake(turn_ref="t2"))
            self.assertEqual(decision["action"], "reconcile-effect")
            self.assertIsNone(request)

    def test_stop_checkpoint_is_quiet_until_structured_phase_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record(
                checkpoint(
                    semanticBoundary="no",
                    boundaryEvidence=["new-user-request", "unknown"],
                ),
                root,
            )
            controller = DecisionController(root, apply=True)
            quiet, request = controller.evaluate(
                wake(boundaryEvidence=["new-user-request", "unknown"])
            )
            self.assertEqual(quiet["action"], "continue")
            self.assertIsNone(request)

            changed, request = controller.evaluate(
                wake(turn_ref="turn-two", boundaryEvidence=["new-user-request", "phase-changed"])
            )
            self.assertEqual(changed["action"], "classify-boundary")
            self.assertIsNone(request)
            card, generation = controller.classification_card(
                wake(turn_ref="turn-two", boundaryEvidence=["phase-changed"])
            )
            self.assertEqual(generation, 1)
            self.assertIn("phase-changed", card["boundaryEvidence"])

    def test_async_classifier_rechecks_idle_and_discards_stale_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record(
                checkpoint(
                    semanticBoundary="uncertain",
                    boundaryEvidence=["phase-complete"],
                ),
                root,
            )
            controller = DecisionController(root, apply=True)
            listener = self.idle_listener()
            lock = threading.RLock()
            entered = threading.Event()
            release = threading.Event()
            sent = []

            def runner(*args, **kwargs):
                entered.set()
                release.wait(2)
                return {"decision": "now", "reason": "stable-boundary"}, {"elapsedMs": 1}

            classifier = AsyncClassifier(
                controller,
                listener,
                lock,
                sent.append,
                "codex",
                root,
                model="luna",
                timeout_seconds=2,
                runner=runner,
            )
            self.assertTrue(classifier.submit(wake()))
            self.assertTrue(entered.wait(1))
            listener.observe(note("turn/started", threadId="root-one", turn={"id": "turn-two"}))
            release.set()
            self.wait_classifier(classifier)
            self.assertEqual(sent, [])
            decisions = [json.loads(path.read_text()) for path in (root / "decisions").glob("*.json")]
            self.assertIn(["classifier-result-stale"], [item["reasonCodes"] for item in decisions])

    def test_async_classifier_issues_once_when_candidate_remains_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record(
                checkpoint(
                    semanticBoundary="uncertain",
                    boundaryEvidence=["phase-complete"],
                ),
                root,
            )
            controller = DecisionController(root, apply=True)
            listener = self.idle_listener()
            sent = []

            def runner(*args, **kwargs):
                return {"decision": "now", "reason": "stable-boundary"}, {"elapsedMs": 1}

            classifier = AsyncClassifier(
                controller,
                listener,
                threading.RLock(),
                sent.append,
                "codex",
                root,
                model="luna",
                timeout_seconds=2,
                runner=runner,
            )
            self.assertTrue(classifier.submit(wake()))
            self.wait_classifier(classifier)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["method"], "thread/compact/start")
            self.assertIn("root-one", controller.inflight)

    def test_soon_latches_checkpoint_first_and_new_generation_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record(
                checkpoint(
                    durableCheckpointReady=False,
                    nextActionPresent=False,
                    semanticBoundary="uncertain",
                    boundaryEvidence=["phase-complete"],
                ),
                root,
            )
            controller = DecisionController(root, apply=True)
            decision, request = controller.resolve_classifier(
                wake(),
                {"decision": "soon", "reason": "checkpoint-first"},
                {"elapsedMs": 1},
                expected_generation=1,
                still_idle=True,
            )
            self.assertEqual(decision["action"], "checkpoint-now")
            self.assertIsNone(request)
            self.assertEqual(len(list((root / "pending").glob("*.json"))), 1)

            record(
                checkpoint(
                    checkpointGeneration=2,
                    semanticBoundary="no",
                    boundaryEvidence=["active-phase"],
                ),
                root,
            )
            decision, request = controller.evaluate(wake(turn_ref="turn-two"))
            self.assertEqual(decision["action"], "compact")
            self.assertEqual(decision["stage"], "pending")
            self.assertIsNotNone(request)

    def test_same_generation_reuses_closed_classification_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record(
                checkpoint(
                    semanticBoundary="uncertain",
                    boundaryEvidence=["phase-complete"],
                ),
                root,
            )
            controller = DecisionController(root, apply=False)
            first, _ = controller.resolve_classifier(
                wake(),
                {"decision": "now", "reason": "stable-boundary"},
                {"elapsedMs": 1},
                expected_generation=1,
                still_idle=True,
            )
            second, request = controller.evaluate(wake(turn_ref="turn-two"))
            self.assertEqual(first["action"], "compact")
            self.assertEqual(second["action"], "compact")
            self.assertEqual(second["classifierDecision"], "now")
            self.assertIsNone(request)
            self.assertEqual(len(list((root / "classifications").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
