from __future__ import annotations

from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from compaction_thread_listener import ThreadListener, called_tools


def notification(method, **params):
    return {"jsonrpc": "2.0", "method": method, "params": params}


def start(listener, thread_id="root", *, parent=None, status="idle"):
    return listener.observe(
        notification(
            "thread/started",
            thread={
                "id": thread_id,
                "parentThreadId": parent,
                "status": {"type": status},
            },
        )
    )


class CompactionThreadListenerTests(unittest.TestCase):
    def test_code_scanner_ignores_quoted_and_commented_tool_names(self):
        source = '''
        const result = await tools.update_plan({plan: []});
        await tools.exec_command({cmd: "rg 'tools.update_goal(' ."});
        // await tools.create_goal({objective: "fake"});
        '''
        self.assertEqual(called_tools({"input": source}), {"update_plan"})

    def test_root_turn_emits_once_only_after_fresh_idle_event(self):
        listener = ThreadListener()
        self.assertEqual(start(listener), [])
        listener.observe(notification("turn/started", threadId="root", turn={"id": "t1"}))
        listener.observe(
            notification(
                "thread/tokenUsage/updated",
                threadId="root",
                turnId="t1",
                tokenUsage={"last": {"inputTokens": 50_000}, "modelContextWindow": 272_000},
            )
        )
        self.assertEqual(
            listener.observe(
                notification(
                    "turn/completed",
                    threadId="root",
                    turn={"id": "t1", "status": "completed"},
                )
            ),
            [],
        )
        wakes = listener.observe(
            notification("thread/status/changed", threadId="root", status={"type": "idle"})
        )
        self.assertEqual(len(wakes), 1)
        self.assertEqual(wakes[0]["turnRef"], "t1")
        self.assertEqual(wakes[0]["usedTokens"], 50_000)
        self.assertFalse(wakes[0]["authorizesCompaction"])
        self.assertEqual(
            listener.observe(
                notification("thread/status/changed", threadId="root", status={"type": "idle"})
            ),
            [],
        )

    def test_subagent_never_emits(self):
        listener = ThreadListener()
        start(listener, "child", parent="root", status="active")
        listener.observe(
            notification(
                "turn/completed",
                threadId="child",
                turn={"id": "ct1", "status": "completed"},
            )
        )
        self.assertEqual(
            listener.observe(
                notification("thread/status/changed", threadId="child", status={"type": "idle"})
            ),
            [],
        )

    def test_idle_before_turn_completed_emits_on_completion(self):
        listener = ThreadListener()
        start(listener, status="active")
        listener.observe(notification("turn/started", threadId="root", turn={"id": "t1"}))
        self.assertEqual(
            listener.observe(
                notification("thread/status/changed", threadId="root", status={"type": "idle"})
            ),
            [],
        )
        wakes = listener.observe(
            notification(
                "turn/completed",
                threadId="root",
                turn={"id": "t1", "status": "completed"},
            )
        )
        self.assertEqual(len(wakes), 1)
        self.assertEqual(wakes[0]["turnRef"], "t1")

    def test_context_compaction_turn_does_not_recurse(self):
        listener = ThreadListener()
        start(listener, status="active")
        listener.observe(notification("turn/started", threadId="root", turn={"id": "compact-1"}))
        listener.observe(
            notification(
                "item/started",
                threadId="root",
                turnId="compact-1",
                item={"id": "item-1", "type": "contextCompaction"},
            )
        )
        listener.observe(
            notification(
                "turn/completed",
                threadId="root",
                turn={"id": "compact-1", "status": "completed"},
            )
        )
        self.assertEqual(
            listener.observe(
                notification("thread/status/changed", threadId="root", status={"type": "idle"})
            ),
            [],
        )

    def test_failed_turn_and_response_messages_are_quiet(self):
        listener = ThreadListener()
        start(listener, status="active")
        self.assertEqual(listener.observe({"jsonrpc": "2.0", "id": 1, "result": {}}), [])
        listener.observe(
            notification(
                "turn/completed",
                threadId="root",
                turn={"id": "bad", "status": "failed"},
            )
        )
        self.assertEqual(
            listener.observe(
                notification("thread/status/changed", threadId="root", status={"type": "idle"})
            ),
            [],
        )

    def test_closed_thread_forgets_state(self):
        listener = ThreadListener()
        start(listener)
        listener.observe(notification("thread/closed", threadId="root"))
        self.assertNotIn("root", listener.threads)

    def test_plan_tool_projects_phase_enum_only(self):
        listener = ThreadListener()
        start(listener, status="active")
        listener.observe(notification("turn/started", threadId="root", turn={"id": "t1"}))
        listener.observe(
            notification(
                "item/completed",
                threadId="root",
                turnId="t1",
                item={
                    "id": "tool-1",
                    "type": "mcpToolCall",
                    "tool": "exec",
                    "arguments": {"input": "const r = await tools.update_plan({plan: []});"},
                },
            )
        )
        listener.observe(
            notification(
                "turn/completed",
                threadId="root",
                turn={"id": "t1", "status": "completed"},
            )
        )
        wakes = listener.observe(
            notification("thread/status/changed", threadId="root", status={"type": "idle"})
        )
        self.assertEqual(wakes[0]["boundaryEvidence"], ["new-user-request", "phase-changed"])
        self.assertNotIn("arguments", str(wakes[0]))


if __name__ == "__main__":
    unittest.main()
