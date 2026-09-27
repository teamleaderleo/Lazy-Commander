#!/usr/bin/env python3
"""Reduce app-server JSON-RPC events into one bounded wake per idle root turn."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ThreadState:
    root: bool
    status: str
    candidate_turn: str | None = None
    last_woken_turn: str | None = None
    current_turn: str | None = None
    compaction_turns: set[str] = field(default_factory=set)
    used_tokens: int | None = None
    context_window: int | None = None
    goal_status: str | None = None
    boundary_evidence: set[str] = field(default_factory=set)


TOOL_CALL = re.compile(r"\btools\.(update_plan|create_goal|update_goal)\s*\(")


def _code_without_literals(source: str, limit: int = 32_768) -> str:
    """Blank JS strings/comments so quoted tool names cannot become evidence."""
    text = source[:limit]
    result = list(text)
    state = "normal"
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if state == "normal":
            if char in {"'", '"', "`"}:
                state, quote, result[index] = "string", char, " "
            elif char == "/" and following == "/":
                state = "line-comment"
                result[index] = result[index + 1] = " "
                index += 1
            elif char == "/" and following == "*":
                state = "block-comment"
                result[index] = result[index + 1] = " "
                index += 1
        elif state == "string":
            result[index] = " "
            if char == "\\":
                if index + 1 < len(text):
                    result[index + 1] = " "
                    index += 1
            elif char == quote:
                state = "normal"
        elif state == "line-comment":
            result[index] = " "
            if char == "\n":
                state = "normal"
        else:
            result[index] = " "
            if char == "*" and following == "/":
                result[index + 1] = " "
                index += 1
                state = "normal"
        index += 1
    return "".join(result)


def called_tools(arguments: Any) -> set[str]:
    strings: list[str] = []
    pending = [arguments]
    while pending and len(strings) < 8:
        value = pending.pop()
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, dict):
            pending.extend(list(value.values())[:16])
        elif isinstance(value, list):
            pending.extend(value[:16])
    return {
        match.group(1)
        for source in strings
        for match in TOOL_CALL.finditer(_code_without_literals(source))
    }


class ThreadListener:
    """Passive event reducer. It never sends a request or reads message bodies."""

    def __init__(self) -> None:
        self.threads: dict[str, ThreadState] = {}

    def is_idle_candidate(self, thread_ref: str, turn_ref: str) -> bool:
        state = self.threads.get(thread_ref)
        return bool(
            state
            and state.root
            and state.status == "idle"
            and state.current_turn is None
            and state.candidate_turn == turn_ref
        )

    def _wake_if_ready(self, thread_id: str, state: ThreadState) -> list[dict[str, Any]]:
        if (
            state.status != "idle"
            or not state.root
            or state.candidate_turn is None
            or state.candidate_turn == state.last_woken_turn
        ):
            return []
        state.last_woken_turn = state.candidate_turn
        return [
            {
                "schema": "lazy-compaction-thread-wake/v1",
                "threadRef": thread_id,
                "turnRef": state.candidate_turn,
                "event": "root-turn-idle",
                "status": "idle",
                "usedTokens": state.used_tokens,
                "contextWindow": state.context_window,
                "goalStatus": state.goal_status,
                "boundaryEvidence": sorted(state.boundary_evidence or {"unknown"}),
                "authorizesCompaction": False,
                "rawConversationRequired": False,
            }
        ]

    @staticmethod
    def _method(message: Any) -> tuple[str, dict[str, Any]] | None:
        if not isinstance(message, dict) or "id" in message:
            return None
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return None
        return method, params

    @staticmethod
    def _thread_id(params: dict[str, Any]) -> str | None:
        value = params.get("threadId")
        return value if isinstance(value, str) and value else None

    def observe(self, message: Any) -> list[dict[str, Any]]:
        parsed = self._method(message)
        if parsed is None:
            return []
        method, params = parsed
        if method == "thread/started":
            thread = params.get("thread")
            if not isinstance(thread, dict):
                return []
            thread_id = thread.get("id")
            status = thread.get("status")
            if not isinstance(thread_id, str) or not isinstance(status, dict):
                return []
            status_type = status.get("type")
            if not isinstance(status_type, str):
                return []
            self.threads[thread_id] = ThreadState(
                root=thread.get("parentThreadId") is None,
                status=status_type,
            )
            return []
        thread_id = self._thread_id(params)
        if thread_id is None or thread_id not in self.threads:
            return []
        state = self.threads[thread_id]
        if method in {"thread/closed", "thread/deleted", "thread/archived"}:
            del self.threads[thread_id]
            return []
        if method == "turn/started":
            turn = params.get("turn")
            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                state.current_turn = turn["id"]
                state.status = "active"
                state.boundary_evidence = {"new-user-request"}
            return []
        if method == "item/started":
            item = params.get("item")
            turn_id = params.get("turnId")
            if (
                isinstance(item, dict)
                and item.get("type") == "contextCompaction"
                and isinstance(turn_id, str)
            ):
                state.compaction_turns.add(turn_id)
                state.candidate_turn = None
            return []
        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage")
            if isinstance(usage, dict):
                last = usage.get("last")
                window = usage.get("modelContextWindow")
                if isinstance(last, dict) and isinstance(last.get("inputTokens"), int):
                    state.used_tokens = last["inputTokens"]
                if isinstance(window, int) and window > 0:
                    state.context_window = window
            return []
        if method == "thread/goal/updated":
            goal = params.get("goal")
            if isinstance(goal, dict) and isinstance(goal.get("status"), str):
                state.goal_status = goal["status"]
                if goal["status"] in {"complete", "blocked"}:
                    state.boundary_evidence.add("phase-complete")
            return []
        if method == "thread/goal/cleared":
            state.goal_status = None
            return []
        if method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                return []
            turn_id = turn["id"]
            state.current_turn = None
            if turn_id in state.compaction_turns:
                state.compaction_turns.discard(turn_id)
                return []
            if turn.get("status") == "completed":
                state.candidate_turn = turn_id
                return self._wake_if_ready(thread_id, state)
            return []
        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict):
                direct_name = item.get("name") or item.get("tool")
                names = {direct_name} if direct_name in {"update_plan", "create_goal", "update_goal"} else set()
                if item.get("type") == "mcpToolCall" and item.get("tool") == "exec":
                    names |= called_tools(item.get("arguments"))
                if "update_plan" in names:
                    state.boundary_evidence.add("phase-changed")
                if "create_goal" in names:
                    state.boundary_evidence.add("new-task-family")
                if "update_goal" in names:
                    state.boundary_evidence.add("phase-changed")
            return []
        if method != "thread/status/changed":
            return []
        status = params.get("status")
        if not isinstance(status, dict) or not isinstance(status.get("type"), str):
            return []
        state.status = status["type"]
        return self._wake_if_ready(thread_id, state)


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    listener = ThreadListener()
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        for wake in listener.observe(message):
            print(json.dumps(wake, sort_keys=True, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
