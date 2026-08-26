from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "goose-job"))

import memory  # noqa: E402

FAKE_BUZZ = r"""
import json
import os
import sys

cmd = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "")
if cmd[:3] == ["mem", "get", "core"]:
    if "--format" in cmd:
        raise SystemExit(1)
    if mode == "found":
        print("I am Fizz, a helpful bee.")
        raise SystemExit(0)
    if mode == "absent":
        print("core not found", file=sys.stderr)
        raise SystemExit(1)
    if mode == "error":
        print("decrypt failed", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(1)
if cmd[:2] == ["--format", "compact"] and cmd[2:4] == ["canvas", "get"]:
    if "--format" in cmd[4:]:
        raise SystemExit(1)
    if mode == "canvas":
        print(json.dumps({"event_id": "evt-1", "mtime": "2026-08-25"}))
        raise SystemExit(0)
    raise SystemExit(1)
if cmd[:2] == ["huddle", "get"]:
    if "--format" in cmd:
        raise SystemExit(1)
    if mode == "huddle":
        print("Stay on topic.")
        raise SystemExit(0)
    raise SystemExit(1)
if cmd[:2] == ["--format", "compact"] and cmd[2:4] == ["messages", "get"]:
    rest = cmd[4:]
    if "--channel" not in rest or "--limit" not in rest or "--kinds" not in rest:
        raise SystemExit(1)
    if "--format" in rest:
        raise SystemExit(1)
    if mode == "get":
        print(
            json.dumps(
                [
                    {"id": "evt-old", "pubkey": "aa" * 32, "content": "update the agent", "created_at": 1},
                    {"id": "evt-now", "pubkey": "bb" * 32, "content": "Yes", "created_at": 2},
                ]
            )
        )
        raise SystemExit(0)
    if mode == "get-empty":
        print("[]")
        raise SystemExit(0)
    if mode == "get-many":
        print(
            json.dumps(
                [
                    {
                        "id": f"evt-{i}",
                        "content": f"line-{i}",
                        "created_at": i,
                    }
                    for i in range(20)
                ]
            )
        )
        raise SystemExit(0)
    raise SystemExit(1)
if cmd[:2] == ["--format", "compact"] and cmd[2:4] == ["messages", "thread"]:
    rest = cmd[4:]
    if "--channel" not in rest or "--event" not in rest or "--id" in rest:
        raise SystemExit(1)
    if "--format" in rest:
        raise SystemExit(1)
    if mode == "thread":
        print(
            json.dumps(
                [
                    {"id": "evt-root", "pubkey": "aa" * 32, "content": "alice hi", "created_at": 1},
                    {"id": "evt-9", "pubkey": "bb" * 32, "content": "bob hello", "created_at": 2},
                ]
            )
        )
        raise SystemExit(0)
    raise SystemExit(1)
raise SystemExit(1)
"""


def _env(tmp: Path, **extra: str) -> dict[str, str]:
    fake = tmp / "fake_buzz.py"
    fake.write_text(FAKE_BUZZ, encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {
            "BUZZ_BIN": str(fake),
            "BUZZ_PRIVATE_KEY": "nsec1notareal",
            "BUZZ_AUTH_TAG": "[]",
            "BUZZ_RELAY_URL": "https://example.invalid",
            "BUZZ_OWNER_PUBKEY": "aa" * 32,
            "BUZZ_CHANNEL_ID": "chan-1",
            "AGENT_NAME": "fizz",
        }
    )
    env.update(extra)
    return env


class CoreFetchTest(unittest.TestCase):
    def test_found(self):
        with tempfile.TemporaryDirectory() as raw:
            env = _env(Path(raw), FAKE_MODE="found")
            with self.assertLogs("goose-memory", level="INFO") as cm:
                logging.getLogger("goose-memory").info("start")
                section = memory.fetch_core(env)
            self.assertIn("[Agent Memory — core]", section)
            self.assertIn("I am Fizz, a helpful bee.", section)
            blob = "\n".join(cm.output)
            self.assertNotIn("I am Fizz, a helpful bee.", blob)

    def test_absent_nudge(self):
        with tempfile.TemporaryDirectory() as raw:
            section = memory.fetch_core(_env(Path(raw), FAKE_MODE="absent"))
            self.assertIn("[Agent Memory — core]", section)
            self.assertIn("buzz mem set core", section)
            self.assertNotIn("I am Fizz", section)

    def test_error_omits(self):
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(memory.fetch_core(_env(Path(raw), FAKE_MODE="error")), "")

    def test_missing_owner_omits(self):
        with tempfile.TemporaryDirectory() as raw:
            env = _env(Path(raw), FAKE_MODE="found")
            env.pop("BUZZ_OWNER_PUBKEY")
            self.assertEqual(memory.fetch_core(env), "")


class StandingFetchTest(unittest.TestCase):
    def test_canvas_huddle_thread_on_success(self):
        with tempfile.TemporaryDirectory() as raw:
            env = _env(Path(raw), FAKE_MODE="canvas", REPLY_TO="evt-9")
            canvas = memory.fetch_canvas(env)
            self.assertIn("[Channel Canvas]", canvas)
            self.assertIn("evt-1", canvas)
            self.assertIn("buzz canvas get --channel chan-1", canvas)
            self.assertNotIn("Stay on topic", canvas)
            huddle = memory.fetch_huddle(_env(Path(raw), FAKE_MODE="huddle"))
            self.assertIn("[Huddle Instructions]", huddle)
            self.assertIn("Stay on topic.", huddle)
            thread = memory.fetch_thread(_env(Path(raw), FAKE_MODE="thread", REPLY_TO="evt-9"))
            self.assertIn("[Thread Context]", thread)
            self.assertIn("alice hi", thread)
            self.assertIn("bob hello", thread)
            self.assertNotIn("[Context]", thread)

    def test_unthreaded_get_includes_stream(self):
        with tempfile.TemporaryDirectory() as raw:
            env = _env(Path(raw), FAKE_MODE="get", BUZZ_EVENT_ID="evt-now")
            with self.assertLogs("goose-memory", level="INFO") as cm:
                logging.getLogger("goose-memory").info("start")
                section = memory.fetch_thread(env)
            self.assertIn("[Conversation Context]", section)
            self.assertIn("update the agent", section)
            self.assertNotIn("Yes", section)
            blob = "\n".join(cm.output)
            self.assertNotIn("update the agent", blob)

    def test_unthreaded_omits_without_channel(self):
        with tempfile.TemporaryDirectory() as raw:
            env = _env(Path(raw), FAKE_MODE="get")
            env["BUZZ_CHANNEL_ID"] = ""
            self.assertEqual(memory.fetch_thread(env), "")

    def test_get_empty_omits(self):
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(memory.fetch_thread(_env(Path(raw), FAKE_MODE="get-empty")), "")

    def test_get_caps_at_twelve(self):
        with tempfile.TemporaryDirectory() as raw:
            section = memory.fetch_thread(_env(Path(raw), FAKE_MODE="get-many"))
            self.assertIn("[Conversation Context]", section)
            self.assertNotIn("line-0", section)
            self.assertIn("line-19", section)
            self.assertIn("line-8", section)

    def test_thread_requires_channel_and_event(self):
        with tempfile.TemporaryDirectory() as raw:
            section = memory.fetch_thread(
                _env(Path(raw), FAKE_MODE="thread", REPLY_TO="evt-9")
            )
            self.assertIn("[Thread Context]", section)
            self.assertIn("alice hi", section)

    def test_misplaced_format_is_usage_error(self):
        """Regression: `buzz messages get … --format compact` is clap exit 1."""
        with tempfile.TemporaryDirectory() as raw:
            env = _env(Path(raw), FAKE_MODE="get")
            proc = memory.run_buzz(
                ["messages", "get", "--channel", "chan-1", "--limit", "12", "--format", "compact"],
                env,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("update the agent", memory.fetch_thread(env))

    def test_cli_error_category_skips_message_body(self):
        proc = memory.subprocess.CompletedProcess(
            ["buzz"],
            1,
            "",
            '{"error":"usage","message":"secret chat body"}',
        )
        self.assertEqual(memory._cli_error_category(proc), "usage")

    def test_omit_on_cli_error(self):
        with tempfile.TemporaryDirectory() as raw:
            env = _env(Path(raw), FAKE_MODE="error", REPLY_TO="evt-9")
            self.assertEqual(memory.fetch_canvas(env), "")
            self.assertEqual(memory.fetch_huddle(env), "")
            self.assertEqual(memory.fetch_thread(env), "")
            self.assertEqual(memory.fetch_thread(_env(Path(raw), FAKE_MODE="error")), "")

    def test_team_section(self):
        self.assertIn("[Team Instructions]", memory.team_section({"BUZZ_TEAM_INSTRUCTIONS": "Be kind."}))
        self.assertEqual(memory.team_section({"BUZZ_TEAM_INSTRUCTIONS": "  "}), "")


class TomAndWorkspaceTest(unittest.TestCase):
    def test_write_tom_concatenates(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            path = memory.write_tom_md(
                home,
                "Stay within 5 enabled extensions.",
                ["[Agent Memory — core]\nI am Fizz.", "[Team Instructions]\nBe kind."],
            )
            text = path.read_text(encoding="utf-8")
            self.assertEqual(path, home / ".config" / "goose" / "tom.md")
            self.assertIn("5 enabled extensions", text)
            self.assertIn("[Agent Memory — core]", text)
            self.assertIn("[Team Instructions]", text)

    def test_workspace_layout(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mnt"
            root.mkdir()
            env = {"BUZZ_WORKSPACE": str(root), "BUZZ_CHANNEL_ID": "9b6babd2-4046-4a91-beea-aa3d9ab190d8"}
            cwd = memory.ensure_workspace("Fizz Bee!", env)
            self.assertIsNotNone(cwd)
            assert cwd is not None
            self.assertEqual(cwd, root / "agents" / "fizz-bee")
            self.assertTrue((root / "shared").is_dir())
            self.assertTrue((cwd / "RESEARCH").is_dir())
            self.assertTrue((cwd / ".scratch").is_dir())
            channel_dir = root / "channels" / "9b6babd2-4046-4a91-beea-aa3d9ab190d8"
            self.assertTrue(channel_dir.is_dir())
            self.assertTrue((channel_dir / "RESEARCH").is_dir())
            section = memory.workspace_section("Fizz Bee!", env)
            self.assertIn("[Workspace]", section)
            self.assertIn("shared", section)
            self.assertIn("agents/fizz-bee", section.replace("\\", "/"))
            self.assertIn("channels/9b6babd2-4046-4a91-beea-aa3d9ab190d8", section.replace("\\", "/"))

    def test_workspace_skips_channel_without_id(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mnt"
            root.mkdir()
            env = {"BUZZ_WORKSPACE": str(root)}
            memory.ensure_workspace("fizz", env)
            self.assertFalse((root / "channels").exists())


if __name__ == "__main__":
    unittest.main()
