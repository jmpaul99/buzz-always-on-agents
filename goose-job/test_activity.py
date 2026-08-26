from __future__ import annotations

import json
import unittest

from activity import GooseActivityParser


def collect(feed: str):
    events = []
    replied = []

    def emit(_kind, data):
        update = None
        if isinstance(data, dict):
            update = data.get("params", {}).get("update", {})
        events.append(update)

    parser = GooseActivityParser(emit, on_reply=lambda: replied.append(1))
    parser.feed(feed)
    parser.close()
    return events, replied


def tools(events):
    return [
        e
        for e in events
        if isinstance(e, dict) and e.get("sessionUpdate") in {"tool_call", "tool_call_update"}
    ]


class GooseActivityParserTest(unittest.TestCase):
    def test_tui_shell_and_send(self):
        events, replied = collect(
            "thinking about it\n"
            "▸ shell\ncommand: buzz messages send --channel x hello\n"
            '{"accepted":true,"id":"e1"}\n'
        )
        calls = tools(events)
        self.assertTrue(replied)
        self.assertTrue(any(e.get("sessionUpdate") == "tool_call" for e in calls))
        last = calls[-1]
        self.assertEqual(last.get("toolName"), "shell")
        self.assertIn("buzz messages send", last.get("rawInput", {}).get("command", ""))
        self.assertIn("accepted", last.get("rawOutput", ""))

    def test_goose_stream_json_unwraps_tool_name_and_args(self):
        req = {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolRequest",
                        "id": "tool-a",
                        "toolCall": {
                            "status": "success",
                            "value": {
                                "name": "developer__shell",
                                "arguments": {"command": "which buzz"},
                            },
                        },
                    },
                    {
                        "type": "toolRequest",
                        "id": "tool-b",
                        "toolCall": {
                            "status": "success",
                            "value": {
                                "name": "developer__shell",
                                "arguments": {
                                    "command": "buzz messages send --channel x hello"
                                },
                            },
                        },
                    },
                ],
            },
        }
        resp = {
            "type": "message",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "toolResponse",
                        "id": "tool-a",
                        "toolResult": {
                            "status": "success",
                            "value": {
                                "content": [{"type": "text", "text": "/opt/sprig/buzz"}]
                            },
                        },
                    },
                    {
                        "type": "toolResponse",
                        "id": "tool-b",
                        "toolResult": {
                            "status": "success",
                            "value": {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": '{"accepted":true,"id":"e1"}',
                                    }
                                ]
                            },
                        },
                    },
                ],
            },
        }
        events, replied = collect(json.dumps(req) + "\n" + json.dumps(resp) + "\n")
        calls = tools(events)
        names = [e.get("toolName") for e in calls if e.get("sessionUpdate") == "tool_call"]
        self.assertEqual(names, ["developer__shell", "developer__shell"])
        first = next(e for e in calls if e.get("toolCallId") == "tool-a")
        self.assertEqual(first.get("rawInput", {}).get("command"), "which buzz")
        self.assertNotEqual(first.get("toolName"), "tool")
        done_a = [
            e
            for e in calls
            if e.get("toolCallId") == "tool-a" and e.get("status") == "completed"
        ]
        self.assertTrue(done_a)
        self.assertIn("/opt/sprig/buzz", done_a[-1].get("rawOutput", ""))
        done_b = [
            e
            for e in calls
            if e.get("toolCallId") == "tool-b" and e.get("status") == "completed"
        ]
        self.assertTrue(done_b)
        self.assertIn("accepted", done_b[-1].get("rawOutput", ""))
        self.assertTrue(replied)

    def test_wrapped_json_line(self):
        line = json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolRequest",
                            "id": "t1",
                            "toolCall": {
                                "status": "success",
                                "value": {
                                    "name": "developer__read_file",
                                    "arguments": {"path": "/tmp/foo.txt"},
                                },
                            },
                        }
                    ],
                },
            }
        )
        mid = max(20, len(line) // 2)
        events, _ = collect(line[:mid] + "\n" + line[mid:] + "\n")
        calls = tools(events)
        start = next(e for e in calls if e.get("sessionUpdate") == "tool_call")
        self.assertEqual(start.get("toolName"), "developer__read_file")
        self.assertEqual(start.get("rawInput", {}).get("path"), "/tmp/foo.txt")

    def test_json_string_and_cmd_alias(self):
        req = {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolRequest",
                        "id": "t-cmd",
                        "toolCall": {
                            "status": "success",
                            "value": {
                                "name": "developer__shell",
                                "arguments": '{"cmd": "echo aliased"}',
                            },
                        },
                    }
                ],
            },
        }
        events, _ = collect(json.dumps(req) + "\n")
        start = next(e for e in tools(events) if e.get("sessionUpdate") == "tool_call")
        self.assertEqual(start.get("rawInput", {}).get("command"), "echo aliased")

    def test_raw_accepted_json_marks_replied(self):
        _events, replied = collect(
            json.dumps({"accepted": True, "event_id": "abc"}) + "\n"
        )
        self.assertTrue(replied)

    def test_notification_log_does_not_hide_send(self):
        events, replied = collect(
            json.dumps(
                {
                    "type": "notification",
                    "extension_id": "developer",
                    "log": {"message": '{"accepted":true,"event_id":"e1"}'},
                }
            )
            + "\n"
        )
        self.assertTrue(replied)

    def test_thinking_before_tool_then_skip_reply_text(self):
        thinking = {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "I'll run date first."},
                    {
                        "type": "toolRequest",
                        "id": "t-date",
                        "toolCall": {
                            "status": "success",
                            "value": {
                                "name": "developer__shell",
                                "arguments": {"command": 'date && echo "Thinking..."'},
                            },
                        },
                    },
                ],
            },
        }
        reply = {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "I have thought for a second, successfully called a tool, and am now reporting back!",
                    }
                ],
            },
        }
        events, _ = collect(json.dumps(thinking) + "\n" + json.dumps(reply) + "\n")
        thoughts = [
            e.get("content", {}).get("text")
            for e in events
            if isinstance(e, dict) and e.get("sessionUpdate") == "agent_thought_chunk"
        ]
        self.assertEqual(thoughts, ["I'll run date first."])
        self.assertTrue(
            any(
                e.get("sessionUpdate") == "tool_call"
                and "date" in (e.get("rawInput") or {}).get("command", "")
                for e in events
                if isinstance(e, dict)
            )
        )

    def test_list_functions_is_visible(self):
        req = {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolRequest",
                        "id": "t-list",
                        "toolCall": {
                            "status": "success",
                            "value": {
                                "name": "developer__list_functions",
                                "arguments": {},
                            },
                        },
                    }
                ],
            },
        }
        events, _ = collect(json.dumps(req) + "\n")
        start = next(e for e in tools(events) if e.get("sessionUpdate") == "tool_call")
        self.assertEqual(start.get("toolName"), "developer__list_functions")
        self.assertEqual(start.get("title"), "developer__list_functions")

    def test_namespaced_search_title(self):
        req = {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolRequest",
                        "id": "t-search",
                        "toolCall": {
                            "status": "success",
                            "value": {
                                "name": "github__search_repositories",
                                "arguments": {"query": "user:example"},
                            },
                        },
                    }
                ],
            },
        }
        events, _ = collect(json.dumps(req) + "\n")
        start = next(e for e in tools(events) if e.get("sessionUpdate") == "tool_call")
        self.assertEqual(start.get("title"), "github__search_repositories")

    def test_reactions_do_not_mark_replied(self):
        events, replied = collect(
            "▸ shell\ncommand: buzz reactions add --event e1 --content '🎉'\n"
            '{"accepted":true,"id":"r1"}\n'
        )
        self.assertFalse(replied)
        calls = tools(events)
        self.assertTrue(calls)

    def test_messages_send_still_marks_replied(self):
        _events, replied = collect(
            "▸ shell\ncommand: buzz messages send --channel x hello\n"
            '{"accepted":true,"id":"e1"}\n'
        )
        self.assertTrue(replied)


if __name__ == "__main__":
    unittest.main()
