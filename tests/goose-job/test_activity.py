from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "goose-job"))

from activity import GooseActivityParser, MAX_THOUGHT


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
            "▸ shell\ncommand: buzz messages send --channel x --content -\n"
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
                                    "command": "buzz messages send --channel x --content -"
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

    def test_complete_event_keeps_assistant_text(self):
        events, replied = collect(
            json.dumps(
                {
                    "type": "complete",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "All systems go."}],
                    },
                }
            )
            + "\n"
        )
        thoughts = [
            e.get("content", {}).get("text")
            for e in events
            if isinstance(e, dict) and e.get("sessionUpdate") == "agent_thought_chunk"
        ]
        self.assertEqual(thoughts, ["All systems go."])
        self.assertFalse(replied)

    def test_last_reply_survives_skipped_post_tool_text(self):
        req = {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolRequest",
                        "id": "t-date",
                        "toolCall": {
                            "status": "success",
                            "value": {
                                "name": "developer__shell",
                                "arguments": {"command": "date"},
                            },
                        },
                    }
                ],
            },
        }
        reply = {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Ready on the channel."}],
            },
        }
        events = []
        parser = GooseActivityParser(lambda _k, data: events.append(data))
        parser.feed(json.dumps(req) + "\n" + json.dumps(reply) + "\n")
        parser.close()
        self.assertEqual(parser.last_reply, "Ready on the channel.")
        self.assertFalse(parser.replied)

    def test_record_external_send_marks_replied(self):
        events = []
        replied = []
        parser = GooseActivityParser(
            lambda _k, data: events.append(data),
            on_reply=lambda: replied.append(1),
        )
        parser.record_external_send(
            "buzz messages send --channel x --content -",
            '{"accepted":true,"id":"e1"}',
            ok=True,
        )
        self.assertTrue(replied)
        self.assertTrue(parser.replied)

    def test_recipe_load_banner_is_not_a_thought(self):
        events, _ = collect(
            "Loading recipe: Buzz reply\n"
            "Description: Default Buzz mention: do the work, then send on the channel\n"
            "Parameters used to load this recipe:\n"
            "channel: 9b6babd2-4046-4a91-beea-aa3d9ab190d8\n"
            "I'll send a short reply.\n"
        )
        thoughts = [
            e.get("content", {}).get("text")
            for e in events
            if isinstance(e, dict) and e.get("sessionUpdate") == "agent_thought_chunk"
        ]
        self.assertEqual(thoughts, ["I'll send a short reply."])

    def test_long_thought_is_emitted_in_full_chunks(self):
        prefix = (
            "Yes, I can see the chat history from the conversation context. "
            "Here is what happened so far: "
        )
        tail = 'You said it "Didnt seem to take" and I asked for clarification.'
        text = prefix + ("x" * 700) + " " + tail
        self.assertGreater(len(text), 800)
        events, _ = collect(text + "\n")
        thoughts = [
            e.get("content", {}).get("text")
            for e in events
            if isinstance(e, dict) and e.get("sessionUpdate") == "agent_thought_chunk"
        ]
        joined = "".join(thoughts)
        self.assertEqual(joined, text)
        self.assertGreater(len(thoughts), 1)
        self.assertIn('"Didnt seem to take"', joined)
        self.assertTrue(all(len(chunk) <= MAX_THOUGHT for chunk in thoughts))

    def test_help_command_is_visible(self):
        events, _ = collect(
            "▸ shell\ncommand: buzz reactions --help\n"
            '{"accepted":true}\n'
        )
        self.assertTrue(tools(events))

    def test_printenv_is_still_hidden(self):
        events, _ = collect(
            "▸ shell\ncommand: printenv\n"
            "BUZZ_PRIVATE_KEY=nsec1hidden\n"
        )
        self.assertEqual(tools(events), [])

    def test_second_send_increments_count(self):
        events = []
        seconds = []
        parser = GooseActivityParser(
            lambda kind, payload: events.append((kind, payload)),
            on_second_send=lambda: seconds.append(True),
        )
        parser.feed(
            "▸ shell\ncommand: buzz messages send --channel x --content -\n"
            '{"accepted":true,"id":"e1"}\n'
        )
        parser.feed(
            "▸ shell\ncommand: buzz messages send --channel x --content -\n"
            '{"accepted":true,"id":"e2"}\n'
        )
        parser.close()
        self.assertEqual(parser.send_count, 2)
        self.assertTrue(seconds)

    def test_messages_send_still_marks_replied(self):
        _events, replied = collect(
            "▸ shell\ncommand: buzz messages send --channel x --content -\n"
            '{"accepted":true,"id":"e1"}\n'
        )
        self.assertTrue(replied)

    def test_argv_content_does_not_mark_replied(self):
        _, quoted = collect(
            "▸ shell\ncommand: buzz messages send --channel x --content \"hello\"\n"
            '{"accepted":true,"id":"e1"}\n'
        )
        self.assertFalse(quoted)
        _, truncated = collect(
            "▸ shell\ncommand: buzz messages send --channel x --content \"? C\n"
            '{"accepted":true,"id":"e1"}\n'
        )
        self.assertFalse(truncated)
        _, positional = collect(
            "▸ shell\ncommand: buzz messages send --channel x hello\n"
            '{"accepted":true,"id":"e1"}\n'
        )
        self.assertFalse(positional)


if __name__ == "__main__":
    unittest.main()
