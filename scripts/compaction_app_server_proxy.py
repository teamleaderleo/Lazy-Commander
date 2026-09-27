#!/usr/bin/env python3
"""Proxy Codex app-server stdio and attach one passive listener per root thread."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Callable

from compaction_advisor import (
    advance_pending,
    advise,
    resolve_classification,
    validate_classification,
)
from compaction_classifier import build_card, run_codex_classifier
from compaction_owner_checkpoint import load_checkpoint, thread_key
from compaction_thread_listener import ThreadListener


INTERNAL_PREFIX = "lazy-compaction:"


class ProxyTermination(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(f"proxy received signal {signum}")
        self.signum = signum


def private_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    body = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != body:
            raise ValueError("same receipt identity changed content")
        return
    with os.fdopen(fd, "wb") as target:
        target.write(body)


def _codex_version(real_codex: str) -> str:
    try:
        result = subprocess.run(
            [real_codex, "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    return (result.stdout or result.stderr).strip().splitlines()[0][:120] or "unavailable"


def record_runtime_start(
    state_root: Path,
    real_codex: str,
    child_pid: int,
    *,
    apply: bool,
    now_ns: int | None = None,
) -> tuple[str, Path]:
    started_ns = time.time_ns() if now_ns is None else now_ns
    runtime_id = f"{os.getpid()}-{started_ns}"
    path = state_root / "runtimes" / f"{runtime_id}-started.json"
    private_write(
        path,
        {
            "schema": "lazy-compaction-proxy-runtime/v1",
            "runtimeId": runtime_id,
            "state": "started",
            "startedAtUnixNs": started_ns,
            "proxyPid": os.getpid(),
            "childPid": child_pid,
            "realCodex": str(Path(real_codex).resolve()),
            "realCodexVersion": _codex_version(real_codex),
            "apply": apply,
        },
    )
    return runtime_id, path


def record_runtime_stop(
    state_root: Path,
    runtime_id: str,
    exit_code: int,
) -> None:
    private_write(
        state_root / "runtimes" / f"{runtime_id}-stopped.json",
        {
            "schema": "lazy-compaction-proxy-runtime-stop/v1",
            "runtimeId": runtime_id,
            "state": "stopped",
            "exitCode": exit_code,
            "stoppedAtUnixNs": time.time_ns(),
        },
    )


def record_runtime_observation(
    state_root: Path,
    runtime_id: str,
    event: str,
    thread_ref: str,
) -> None:
    if event not in {"root-thread", "root-idle"}:
        raise ValueError("unknown runtime observation")
    path = state_root / "runtimes" / f"{runtime_id}-{event}.json"
    if path.exists():
        return
    private_write(
        path,
        {
            "schema": "lazy-compaction-proxy-observation/v1",
            "runtimeId": runtime_id,
            "event": event,
            "threadKey": thread_key(thread_ref),
            "observedAtUnixNs": time.time_ns(),
            "rawConversationRetained": False,
        },
    )


class DecisionController:
    def __init__(self, state_root: Path, *, apply: bool) -> None:
        self.state_root = state_root
        self.apply = apply
        self.inflight: dict[str, dict[str, Any]] = {}

    def _last_compacted_generation(self, thread_ref: str) -> int:
        prefix = f"{thread_key(thread_ref)}-"
        generations = []
        for path in (self.state_root / "effects").glob(f"{prefix}*-committed.json"):
            raw = json.loads(path.read_text(encoding="utf-8"))
            generation = raw.get("checkpointGeneration")
            if isinstance(generation, int) and generation >= 0:
                generations.append(generation)
        return max(generations, default=0)

    def _effect_path(self, thread_ref: str, generation: int, state: str) -> Path:
        return self.state_root / "effects" / f"{thread_key(thread_ref)}-{generation}-{state}.json"

    def _effect_unsettled(self, thread_ref: str, generation: int) -> bool:
        issued = self._effect_path(thread_ref, generation, "issued").exists()
        committed = self._effect_path(thread_ref, generation, "committed").exists()
        return issued and not committed

    def _pending_path(self, thread_ref: str, after_generation: int) -> Path:
        return self.state_root / "pending" / f"{thread_key(thread_ref)}-{after_generation}.json"

    def _record_pending(self, pending: dict[str, Any]) -> None:
        private_write(
            self._pending_path(pending["threadRef"], pending["afterCheckpointGeneration"]),
            pending,
        )

    def _load_pending(self, thread_ref: str) -> dict[str, Any] | None:
        prefix = f"{thread_key(thread_ref)}-"
        candidates = []
        for path in (self.state_root / "pending").glob(f"{prefix}*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            generation = value.get("afterCheckpointGeneration")
            if isinstance(generation, int):
                candidates.append((generation, value))
        if not candidates:
            return None
        after_generation, pending = max(candidates, key=lambda item: item[0])
        if self._last_compacted_generation(thread_ref) > after_generation:
            return None
        return pending

    def _classification_path(self, thread_ref: str, generation: int) -> Path:
        return self.state_root / "classifications" / f"{thread_key(thread_ref)}-{generation}.json"

    def _record_classification(
        self,
        thread_ref: str,
        generation: int,
        classification: dict[str, str],
        metrics: dict[str, Any],
    ) -> None:
        private_write(
            self._classification_path(thread_ref, generation),
            {
                "schema": "lazy-compaction-classification-receipt/v1",
                "threadRef": thread_ref,
                "checkpointGeneration": generation,
                "classification": validate_classification(classification),
                "metrics": metrics,
            },
        )

    def _load_classification(
        self,
        thread_ref: str,
        generation: int,
    ) -> tuple[dict[str, str], dict[str, Any]] | None:
        path = self._classification_path(thread_ref, generation)
        if not path.exists():
            return None
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if (
            receipt.get("schema") != "lazy-compaction-classification-receipt/v1"
            or receipt.get("threadRef") != thread_ref
            or receipt.get("checkpointGeneration") != generation
            or not isinstance(receipt.get("metrics"), dict)
        ):
            raise ValueError("classification receipt does not match its identity")
        return validate_classification(receipt.get("classification")), receipt["metrics"]

    def _record_effect(self, thread_ref: str, generation: int, state: str, **extra: Any) -> None:
        private_write(
            self._effect_path(thread_ref, generation, state),
            {
                "schema": "lazy-compaction-effect/v1",
                "threadRef": thread_ref,
                "checkpointGeneration": generation,
                "state": state,
                "retryAuthorized": False,
                **extra,
            },
        )

    def _advisor_state(
        self,
        wake: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        used = wake.get("usedTokens")
        window = wake.get("contextWindow")
        token_state_complete = isinstance(used, int) and isinstance(window, int) and 0 <= used <= window
        if not token_state_complete:
            used, window = 0, 1
        boundary = checkpoint["semanticBoundary"]
        semantic_evidence = {
            "new-task-family",
            "phase-changed",
            "phase-complete",
        }
        evidence = set(checkpoint.get("boundaryEvidence", [])) | set(
            wake.get("boundaryEvidence", [])
        )
        if boundary == "no" and semantic_evidence.intersection(evidence):
            boundary = "uncertain"
        return (
            {
                "schema": "lazy-compaction-checkpoint/v1",
                "threadRef": wake["threadRef"],
                "usedTokens": used,
                "contextWindow": window,
                "checkpointGeneration": checkpoint["checkpointGeneration"],
                "lastCompactedGeneration": self._last_compacted_generation(wake["threadRef"]),
                "durableCheckpointReady": checkpoint["durableCheckpointReady"]
                and checkpoint["nextActionPresent"],
                "semanticBoundary": boundary,
                "effectState": checkpoint["effectState"],
            },
            token_state_complete,
        )

    def _issue_compaction(
        self,
        wake: dict[str, Any],
        generation: int,
    ) -> dict[str, Any] | None:
        thread_ref = wake["threadRef"]
        turn_ref = wake["turnRef"]
        if thread_ref in self.inflight:
            return None
        request_id = f"{INTERNAL_PREFIX}{hashlib.sha256(f'{thread_ref}\0{turn_ref}\0{generation}'.encode()).hexdigest()}"
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "thread/compact/start",
            "params": {"threadId": thread_ref},
        }
        self.inflight[thread_ref] = {
            "requestId": request_id,
            "turnRef": turn_ref,
            "checkpointGeneration": generation,
            "state": "issued",
        }
        self._record_effect(
            thread_ref,
            generation,
            "issued",
            turnRef=turn_ref,
            requestIdSha256=hashlib.sha256(request_id.encode()).hexdigest(),
        )
        return request

    def evaluate(self, wake: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        thread_ref = wake["threadRef"]
        turn_ref = wake["turnRef"]
        checkpoint = load_checkpoint(self.state_root, thread_ref)
        if checkpoint is None:
            decision = {
                "schema": "lazy-compaction-proxy-decision/v1",
                "threadRef": thread_ref,
                "turnRef": turn_ref,
                "action": "continue",
                "reasonCodes": ["no-owner-checkpoint"],
                "mode": "apply" if self.apply else "observe",
                "authorizesCompaction": False,
                "stage": "advisor",
            }
            self._persist_decision(decision, 0)
            return decision, None
        generation = checkpoint["checkpointGeneration"]
        if thread_ref in self.inflight:
            return (
                {
                    "schema": "lazy-compaction-proxy-decision/v1",
                    "threadRef": thread_ref,
                    "turnRef": turn_ref,
                    "checkpointGeneration": generation,
                    "action": "continue",
                    "reasonCodes": ["compaction-single-flight"],
                    "mode": "apply" if self.apply else "observe",
                    "authorizesCompaction": False,
                    "stage": "advisor",
                },
                None,
            )
        if self._effect_unsettled(thread_ref, generation):
            decision = {
                "schema": "lazy-compaction-proxy-decision/v1",
                "threadRef": thread_ref,
                "turnRef": turn_ref,
                "checkpointGeneration": generation,
                "action": "reconcile-effect",
                "reasonCodes": ["prior-issued-compaction-has-no-settlement"],
                "mode": "apply" if self.apply else "observe",
                "authorizesCompaction": False,
                "stage": "advisor",
            }
            self._persist_decision(decision, generation)
            return decision, None
        advisor_state, token_state_complete = self._advisor_state(wake, checkpoint)
        pending = self._load_pending(thread_ref)
        if pending is not None:
            advanced = advance_pending(advisor_state, pending)
            decision = {
                "schema": "lazy-compaction-proxy-decision/v1",
                "threadRef": thread_ref,
                "turnRef": turn_ref,
                "checkpointGeneration": generation,
                "action": advanced["action"],
                "reasonCodes": ["pending-checkpoint-first"],
                "mode": "apply" if self.apply else "observe",
                "authorizesCompaction": bool(advanced["authorizesCompaction"] and self.apply),
                "stage": "pending",
            }
            self._persist_decision(decision, generation)
            request = (
                self._issue_compaction(wake, generation)
                if decision["authorizesCompaction"]
                else None
            )
            return decision, request
        advised = advise(advisor_state)
        if not token_state_complete and advised["action"] == "compact" and checkpoint["semanticBoundary"] != "yes":
            advised["action"] = "continue"
            advised["reasonCodes"] = ["pressure-state-unavailable"]
            advised["authorizesCompaction"] = False
        decision = {
            "schema": "lazy-compaction-proxy-decision/v1",
            "threadRef": thread_ref,
            "turnRef": turn_ref,
            "checkpointGeneration": generation,
            "action": advised["action"],
            "reasonCodes": advised["reasonCodes"],
            "usedWindowPct": advised["usedWindowPct"],
            "mode": "apply" if self.apply else "observe",
            "authorizesCompaction": bool(advised["authorizesCompaction"] and self.apply),
            "stage": "advisor",
        }
        self._persist_decision(decision, generation)
        if decision["action"] == "classify-boundary":
            cached = self._load_classification(thread_ref, generation)
            if cached is not None:
                classification, metrics = cached
                return self.resolve_classifier(
                    wake,
                    classification,
                    metrics,
                    expected_generation=generation,
                    still_idle=True,
                )
        if not decision["authorizesCompaction"] or thread_ref in self.inflight:
            return decision, None
        return decision, self._issue_compaction(wake, generation)

    def classification_card(self, wake: dict[str, Any]) -> tuple[dict[str, Any], int]:
        checkpoint = load_checkpoint(self.state_root, wake["threadRef"])
        if checkpoint is None:
            raise ValueError("classification candidate is no longer uncertain")
        state, _ = self._advisor_state(wake, checkpoint)
        if state["semanticBoundary"] != "uncertain":
            raise ValueError("classification candidate is no longer uncertain")
        merged = dict(checkpoint)
        merged["boundaryEvidence"] = sorted(
            set(checkpoint.get("boundaryEvidence", []))
            | set(wake.get("boundaryEvidence", []))
        )[:8]
        return (
            build_card(
                merged,
                checkpoint_fresh=state["checkpointGeneration"] > state["lastCompactedGeneration"],
                goal_status=wake.get("goalStatus"),
            ),
            checkpoint["checkpointGeneration"],
        )

    def resolve_classifier(
        self,
        wake: dict[str, Any],
        classification: dict[str, str],
        metrics: dict[str, Any],
        *,
        expected_generation: int,
        still_idle: bool,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        checkpoint = load_checkpoint(self.state_root, wake["threadRef"])
        generation = checkpoint["checkpointGeneration"] if checkpoint else expected_generation
        state = self._advisor_state(wake, checkpoint)[0] if checkpoint else None
        stale = (
            not still_idle
            or checkpoint is None
            or generation != expected_generation
            or state is None
            or state["semanticBoundary"] != "uncertain"
        )
        if stale:
            decision = {
                "schema": "lazy-compaction-proxy-decision/v1",
                "threadRef": wake["threadRef"],
                "turnRef": wake["turnRef"],
                "checkpointGeneration": generation,
                "action": "continue",
                "reasonCodes": ["classifier-result-stale"],
                "mode": "apply" if self.apply else "observe",
                "authorizesCompaction": False,
                "stage": "classifier",
                "classifierMetrics": metrics,
            }
            self._persist_decision(decision, generation)
            return decision, None
        resolved = resolve_classification(state, classification)
        self._record_classification(
            wake["threadRef"],
            generation,
            classification,
            metrics,
        )
        if resolved["pending"] is not None:
            self._record_pending(resolved["pending"])
        decision = {
            "schema": "lazy-compaction-proxy-decision/v1",
            "threadRef": wake["threadRef"],
            "turnRef": wake["turnRef"],
            "checkpointGeneration": generation,
            "action": resolved["action"],
            "reasonCodes": [resolved["classifierReason"]],
            "mode": "apply" if self.apply else "observe",
            "authorizesCompaction": bool(resolved["authorizesCompaction"] and self.apply),
            "stage": "classifier",
            "classifierDecision": resolved["classifierDecision"],
            "classifierMetrics": metrics,
        }
        self._persist_decision(decision, generation)
        request = (
            self._issue_compaction(wake, generation)
            if decision["authorizesCompaction"]
            else None
        )
        return decision, request

    def record_classifier_failure(self, wake: dict[str, Any], reason: str) -> None:
        checkpoint = load_checkpoint(self.state_root, wake["threadRef"])
        generation = checkpoint["checkpointGeneration"] if checkpoint else 0
        decision = {
            "schema": "lazy-compaction-proxy-decision/v1",
            "threadRef": wake["threadRef"],
            "turnRef": wake["turnRef"],
            "checkpointGeneration": generation,
            "action": "continue",
            "reasonCodes": [reason],
            "mode": "apply" if self.apply else "observe",
            "authorizesCompaction": False,
            "stage": "classifier",
        }
        self._persist_decision(decision, generation)

    def _persist_decision(self, decision: dict[str, Any], generation: int) -> None:
        identity = hashlib.sha256(
            f'{decision["threadRef"]}\0{decision["turnRef"]}\0{generation}\0{decision["mode"]}\0{decision.get("stage", "advisor")}'.encode()
        ).hexdigest()
        private_write(self.state_root / "decisions" / f"{identity}.json", decision)

    def observe_response(self, message: dict[str, Any]) -> bool:
        request_id = message.get("id")
        if not isinstance(request_id, str) or not request_id.startswith(INTERNAL_PREFIX):
            return False
        for thread_ref, pending in list(self.inflight.items()):
            if pending["requestId"] != request_id:
                continue
            if "error" in message:
                pending["state"] = "ambiguous"
                self._record_effect(
                    thread_ref,
                    pending["checkpointGeneration"],
                    "ambiguous",
                    reason="app-server-request-error",
                )
                del self.inflight[thread_ref]
            else:
                pending["state"] = "accepted"
                self._settle_if_ready(thread_ref, pending)
            return True
        return True

    def _settle_if_ready(self, thread_ref: str, pending: dict[str, Any]) -> None:
        if (
            pending.get("state") == "accepted"
            and pending.get("itemCompleted")
            and pending.get("turnCompleted")
            and pending.get("idleObserved")
        ):
            self._record_effect(
                thread_ref,
                pending["checkpointGeneration"],
                "committed",
                compactionTurn=pending["compactionTurn"],
            )
            del self.inflight[thread_ref]

    def observe_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        thread_ref = params.get("threadId")
        if not isinstance(thread_ref, str) or thread_ref not in self.inflight:
            return
        pending = self.inflight[thread_ref]
        if method == "item/started":
            item = params.get("item")
            turn_ref = params.get("turnId")
            if (
                isinstance(item, dict)
                and item.get("type") == "contextCompaction"
                and isinstance(turn_ref, str)
            ):
                pending["compactionTurn"] = turn_ref
                pending["compactionItem"] = item.get("id")
            return
        if method == "item/completed":
            item = params.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "contextCompaction"
                and item.get("id") == pending.get("compactionItem")
            ):
                pending["itemCompleted"] = True
                self._settle_if_ready(thread_ref, pending)
            return
        if method == "turn/completed":
            turn = params.get("turn")
            if (
                isinstance(turn, dict)
                and turn.get("id") == pending.get("compactionTurn")
                and turn.get("status") == "completed"
            ):
                pending["turnCompleted"] = True
                self._settle_if_ready(thread_ref, pending)
            return
        if method != "thread/status/changed":
            return
        status = params.get("status")
        if (
            isinstance(status, dict)
            and status.get("type") == "idle"
            and pending.get("compactionTurn")
        ):
            pending["idleObserved"] = True
            self._settle_if_ready(thread_ref, pending)


class AsyncClassifier:
    """Classify uncertain wakes off the app-server read loop, then revalidate."""

    def __init__(
        self,
        controller: DecisionController,
        listener: ThreadListener,
        runtime_lock: threading.RLock,
        send_request: Callable[[dict[str, Any]], None],
        real_codex: str,
        state_root: Path,
        *,
        model: str,
        timeout_seconds: float,
        runner: Callable[..., tuple[dict[str, str], dict[str, Any]]] = run_codex_classifier,
    ) -> None:
        self.controller = controller
        self.listener = listener
        self.runtime_lock = runtime_lock
        self.send_request = send_request
        self.real_codex = real_codex
        self.state_root = state_root
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        self.pending: set[tuple[str, str]] = set()

    def submit(self, wake: dict[str, Any]) -> bool:
        identity = (wake["threadRef"], wake["turnRef"])
        with self.runtime_lock:
            if identity in self.pending:
                return False
            try:
                card, generation = self.controller.classification_card(wake)
            except (ValueError, OSError, json.JSONDecodeError):
                self.controller.record_classifier_failure(wake, "classifier-candidate-invalid")
                return False
            self.pending.add(identity)
        threading.Thread(
            target=self._work,
            args=(dict(wake), card, generation, identity),
            daemon=True,
            name=f"compaction-classifier-{identity[0][:8]}",
        ).start()
        return True

    def _work(
        self,
        wake: dict[str, Any],
        card: dict[str, Any],
        generation: int,
        identity: tuple[str, str],
    ) -> None:
        try:
            try:
                classification, metrics = self.runner(
                    self.real_codex,
                    self.state_root,
                    card,
                    model=self.model,
                    timeout_seconds=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                with self.runtime_lock:
                    self.controller.record_classifier_failure(wake, "classifier-timeout")
                return
            except (RuntimeError, ValueError, OSError, json.JSONDecodeError):
                with self.runtime_lock:
                    self.controller.record_classifier_failure(wake, "classifier-failed")
                return
            with self.runtime_lock:
                still_idle = self.listener.is_idle_candidate(*identity)
                _, request = self.controller.resolve_classifier(
                    wake,
                    classification,
                    metrics,
                    expected_generation=generation,
                    still_idle=still_idle,
                )
                if request is not None:
                    try:
                        self.send_request(request)
                    except OSError:
                        self.controller.observe_response(
                            {
                                "id": request["id"],
                                "error": {"message": "proxy-write-failed"},
                            }
                        )
        finally:
            with self.runtime_lock:
                self.pending.discard(identity)


def _forward_input(source: BinaryIO, target: BinaryIO, lock: threading.Lock) -> None:
    try:
        while chunk := os.read(source.fileno(), 64 * 1024):
            with lock:
                target.write(chunk)
                target.flush()
    finally:
        try:
            target.close()
        except OSError:
            pass


def _stop_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def run_proxy(real_codex: str, argv: list[str], state_root: Path, *, apply: bool) -> int:
    child = subprocess.Popen(
        [real_codex, *argv],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
    )
    try:
        runtime_id, _ = record_runtime_start(
            state_root,
            real_codex,
            child.pid,
            apply=apply,
        )
    except Exception:
        child.terminate()
        child.wait()
        raise
    assert child.stdin is not None and child.stdout is not None
    lock = threading.Lock()
    feeder = threading.Thread(
        target=_forward_input,
        args=(sys.stdin.buffer, child.stdin, lock),
        daemon=True,
    )
    feeder.start()
    listener = ThreadListener()
    controller = DecisionController(state_root, apply=apply)
    runtime_lock = threading.RLock()

    def send_request(request: dict[str, Any]) -> None:
        encoded = (json.dumps(request, separators=(",", ":")) + "\n").encode()
        with lock:
            child.stdin.write(encoded)
            child.stdin.flush()

    classifier = AsyncClassifier(
        controller,
        listener,
        runtime_lock,
        send_request,
        real_codex,
        state_root,
        model=os.environ.get("LAZY_COMPACTION_CLASSIFIER_MODEL", "gpt-5.6-luna"),
        timeout_seconds=float(os.environ.get("LAZY_COMPACTION_CLASSIFIER_TIMEOUT", "60")),
    )

    def request_termination(signum: int, _frame: Any) -> None:
        raise ProxyTermination(signum)

    signal.signal(signal.SIGTERM, request_termination)
    try:
        for raw in child.stdout:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                sys.stdout.buffer.write(raw)
                sys.stdout.buffer.flush()
                continue
            with runtime_lock:
                if controller.observe_response(message):
                    continue
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()
            with runtime_lock:
                controller.observe_notification(message)
                if message.get("method") == "thread/started":
                    thread = (message.get("params") or {}).get("thread")
                    if (
                        isinstance(thread, dict)
                        and isinstance(thread.get("id"), str)
                        and thread.get("parentThreadId") is None
                    ):
                        record_runtime_observation(
                            state_root,
                            runtime_id,
                            "root-thread",
                            thread["id"],
                        )
                for wake in listener.observe(message):
                    record_runtime_observation(
                        state_root,
                        runtime_id,
                        "root-idle",
                        wake["threadRef"],
                    )
                    decision, request = controller.evaluate(wake)
                    if request is not None:
                        send_request(request)
                    elif decision["action"] == "classify-boundary":
                        classifier.submit(wake)
        exit_code = child.wait()
    except KeyboardInterrupt:
        exit_code = 130
        _stop_child(child)
    except ProxyTermination as termination:
        exit_code = 128 + termination.signum
        _stop_child(child)
    except BaseException:
        _stop_child(child)
        record_runtime_stop(state_root, runtime_id, 1)
        raise
    record_runtime_stop(state_root, runtime_id, exit_code)
    return exit_code


def main() -> int:
    real_codex = os.environ.get("LAZY_REAL_CODEX_CLI", "").strip()
    if not real_codex:
        raise SystemExit("LAZY_REAL_CODEX_CLI must name the real Codex executable")
    argv = sys.argv[1:]
    if "app-server" not in argv:
        os.execv(real_codex, [real_codex, *argv])
    state_root = Path(
        os.environ.get(
            "LAZY_COMPACTION_STATE_ROOT",
            str(Path.home() / ".codex" / "state" / "lazy-commander" / "compaction-proxy"),
        )
    )
    apply = os.environ.get("LAZY_COMPACTION_APPLY") == "1"
    return run_proxy(real_codex, argv, state_root, apply=apply)


if __name__ == "__main__":
    raise SystemExit(main())
