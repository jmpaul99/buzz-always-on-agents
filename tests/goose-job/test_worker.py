from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "listener"))
sys.path.insert(0, str(_ROOT / "goose-job"))

from agenthome import sync_agent_home
from worker import (
    build_goose_cmd,
    build_send_argv,
    one_line,
    prepare_turn,
    recipe_params,
)


class SyncAgentHomeTest(unittest.TestCase):
    def test_copies_config_hints_and_guardrails(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw) / "base"
            home = Path(raw) / "home"
            cfg = base / ".config" / "goose"
            cfg.mkdir(parents=True)
            (cfg / "config.yaml").write_text("extensions: {}\n", encoding="utf-8")
            (cfg / ".goosehints").write_text("This is a Buzz cloud agent.\n", encoding="utf-8")
            (cfg / "guardrails.md").write_text("Stay within 5 enabled extensions.\n", encoding="utf-8")
            sync_agent_home(base, home)
            dest_cfg = home / ".config" / "goose"
            self.assertTrue((dest_cfg / "config.yaml").is_file())
            hints = dest_cfg / ".goosehints"
            self.assertTrue(hints.is_file())
            self.assertIn("Buzz cloud", hints.read_text(encoding="utf-8"))
            guardrails = dest_cfg / "guardrails.md"
            self.assertTrue(guardrails.is_file())
            self.assertIn("5 enabled extensions", guardrails.read_text(encoding="utf-8"))
            self.assertFalse((home / ".goosehints").exists())


def _params(cmd: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    idx = 0
    while idx < len(cmd):
        if cmd[idx] == "--params" and idx + 1 < len(cmd):
            key, _, val = cmd[idx + 1].partition("=")
            out[key] = val
            idx += 2
            continue
        idx += 1
    return out


class GooseCmdTest(unittest.TestCase):
    def test_recipe_when_file_exists(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            recipe = root / "github" / "recipe.yaml"
            recipe.parent.mkdir()
            recipe.write_text("title: GitHub\n", encoding="utf-8")
            cmd = build_goose_cmd("list my repos", "github", recipe_root=root)
            self.assertIn("--recipe", cmd)
            self.assertEqual(cmd[cmd.index("--recipe") + 1], str(recipe))
            self.assertEqual(_params(cmd)["message"], "list my repos")
            self.assertNotIn("-t", cmd)

    def test_defaults_to_reply_recipe(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            dest = root / "reply"
            dest.mkdir()
            (dest / "recipe.yaml").write_text("title: Reply\n", encoding="utf-8")
            cmd = build_goose_cmd("hello", "", recipe_root=root)
            self.assertIn("--recipe", cmd)
            self.assertEqual(cmd[cmd.index("--recipe") + 1], str(dest / "recipe.yaml"))
            self.assertNotIn("-t", cmd)
            self.assertIn("--quiet", cmd)

    def test_text_when_no_recipe_file(self):
        cmd = build_goose_cmd("hello", "", recipe_root=Path("/missing"))
        self.assertIn("-t", cmd)
        self.assertEqual(cmd[cmd.index("-t") + 1], "hello")
        self.assertNotIn("--recipe", cmd)
        self.assertIn("--quiet", cmd)


class RecipeParamTest(unittest.TestCase):
    def test_one_line_strips_newlines(self):
        self.assertEqual(one_line("You are Fizz.\n\nSend with: buzz"), "You are Fizz. Send with: buzz")
        self.assertNotIn("\n", one_line("a\nb\nc"))

    def test_one_line_strips_yaml_forbidden_controls(self):
        dirty = "You are Fizz.\x1b[32mgreen\x1b[0m\x00\x08keep"
        cleaned = one_line(dirty)
        self.assertEqual(cleaned, "You are Fizz. [32mgreen [0m keep")
        self.assertTrue(all(ord(ch) >= 32 and ord(ch) != 127 for ch in cleaned))

    def test_uses_mention_body_not_full_prompt(self):
        params = recipe_params(
            {
                "BUZZ_MESSAGE": "Test guy",
                "BUZZ_CHANNEL_ID": "chan-1",
                "BUZZ_IDENTITY": "You are Health.",
                "BUZZ_SEND_CMD": "buzz messages send --channel chan-1 --content '...'",
                "BUZZ_AUTHOR_PUBKEY": "ab",
                "BUZZ_EVENT_ID": "e1",
            },
            "You are Health.\n\nYou were mentioned in channel chan-1.\nSend with: buzz",
        )
        self.assertEqual(params["message"], "Test guy")
        self.assertIn("messages send", params["send_cmd"])
        self.assertNotIn("channel", params)
        self.assertNotIn("author", params)
        self.assertNotIn("event_id", params)

    def test_recipe_argv_stays_single_line(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            dest = root / "reply"
            dest.mkdir()
            (dest / "recipe.yaml").write_text("title: Reply\n", encoding="utf-8")
            cmd = build_goose_cmd(
                "ignored full prompt\nwith colon: value",
                "",
                recipe_root=root,
                params=recipe_params({"BUZZ_MESSAGE": "Test guy\nstill here", "BUZZ_CHANNEL_ID": "c1"}, ""),
            )
            for flag, val in zip(cmd, cmd[1:]):
                if flag == "--params":
                    self.assertNotIn("\n", val)
            self.assertEqual(_params(cmd)["message"], "Test guy still here")


class SendArgvTest(unittest.TestCase):
    def test_includes_reply_to(self):
        cmd = build_send_argv("chan-1", "hello", "evt-9")
        self.assertEqual(cmd[:3], ["buzz", "messages", "send"])
        self.assertEqual(cmd[cmd.index("--channel") + 1], "chan-1")
        self.assertEqual(cmd[cmd.index("--content") + 1], "hello")
        self.assertEqual(cmd[cmd.index("--reply-to") + 1], "evt-9")

    def test_omits_reply_to_when_empty(self):
        cmd = build_send_argv("chan-1", "hello")
        self.assertNotIn("--reply-to", cmd)


class PrepareTurnTest(unittest.TestCase):
    def test_moim_targets_tom_and_cwd_is_workspace(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw) / "base"
            home = Path(raw) / "home"
            mount = Path(raw) / "mnt"
            mount.mkdir()
            cfg = base / ".config" / "goose"
            cfg.mkdir(parents=True)
            (cfg / "config.yaml").write_text("extensions: {}\n", encoding="utf-8")
            (cfg / "guardrails.md").write_text("Stay within 5 enabled extensions.\n", encoding="utf-8")
            sync_agent_home(base, home)
            env = {
                "AGENT_NAME": "fizz",
                "BUZZ_WORKSPACE": str(mount),
                "BUZZ_TEAM_INSTRUCTIONS": "Be kind.",
            }
            cwd = prepare_turn(env, "fizz", home)
            self.assertEqual(cwd, mount / "agents" / "fizz")
            self.assertEqual(env["HOME"], str(home))
            self.assertNotEqual(cwd, home)
            self.assertTrue(env["GOOSE_MOIM_MESSAGE_FILE"].endswith("tom.md"))
            tom = Path(env["GOOSE_MOIM_MESSAGE_FILE"]).read_text(encoding="utf-8")
            self.assertIn("5 enabled extensions", tom)
            self.assertIn("[Team Instructions]", tom)
            self.assertIn("[Workspace]", tom)


if __name__ == "__main__":
    unittest.main()
