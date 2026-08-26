from __future__ import annotations

import json
import os
import unittest

os.environ.setdefault("LITELLM_URL", "http://127.0.0.1:4000")

from litellm_proxy import (
    _disable_stream,
    apply_tool_name_rewrite,
    offered_tool_names,
    unique_prefixed_name,
)


def _req(*names: str) -> dict:
    return {
        "model": "goose",
        "tools": [
            {"type": "function", "function": {"name": name, "parameters": {}}}
            for name in names
        ],
    }


def _resp(name: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": name, "arguments": "{}"},
                        }
                    ],
                }
            }
        ]
    }


class DisableStreamTest(unittest.TestCase):
    def test_forces_stream_false(self):
        raw = json.dumps({"model": "goose", "stream": True, "messages": []}).encode()
        out = json.loads(_disable_stream(raw, "application/json"))
        self.assertFalse(out["stream"])

    def test_leaves_non_stream_payload(self):
        raw = json.dumps({"model": "goose", "messages": []}).encode()
        self.assertEqual(_disable_stream(raw, "application/json"), raw)


class PrefixRewriteTest(unittest.TestCase):
    def test_offered_names(self):
        names = offered_tool_names(_req("github__get_me", "developer__shell"))
        self.assertEqual(names, {"github__get_me", "developer__shell"})

    def test_unique_suffix(self):
        offered = {"github__get_me", "developer__shell"}
        self.assertEqual(unique_prefixed_name(offered, "get_me"), "github__get_me")

    def test_already_namespaced(self):
        offered = {"github__get_me"}
        self.assertIsNone(unique_prefixed_name(offered, "github__get_me"))

    def test_ambiguous_suffix(self):
        offered = {"github__get_me", "gitlab__get_me"}
        self.assertIsNone(unique_prefixed_name(offered, "get_me"))

    def test_missing_tool(self):
        offered = {"github__search_repositories"}
        self.assertIsNone(unique_prefixed_name(offered, "list_repositories"))

    def test_rewrites_openai_tool_call(self):
        raw = apply_tool_name_rewrite(
            json.dumps(_req("github__get_me", "developer__shell")).encode(),
            json.dumps(_resp("get_me")).encode(),
            "application/json",
        )
        payload = json.loads(raw)
        name = payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
        self.assertEqual(name, "github__get_me")

    def test_leaves_invented_name(self):
        raw = apply_tool_name_rewrite(
            json.dumps(_req("github__search_repositories")).encode(),
            json.dumps(_resp("list_repositories")).encode(),
            "application/json",
        )
        payload = json.loads(raw)
        name = payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
        self.assertEqual(name, "list_repositories")

    def test_leaves_ambiguous(self):
        raw = apply_tool_name_rewrite(
            json.dumps(_req("github__get_me", "gitlab__get_me")).encode(),
            json.dumps(_resp("get_me")).encode(),
            "application/json",
        )
        payload = json.loads(raw)
        name = payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
        self.assertEqual(name, "get_me")


if __name__ == "__main__":
    unittest.main()
