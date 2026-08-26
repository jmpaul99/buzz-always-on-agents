from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "listener"))

from agenthome import sync_agent_home
from worker import build_goose_cmd


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
            self.assertIn("--params", cmd)
            self.assertEqual(cmd[cmd.index("--params") + 1], "message=list my repos")
            self.assertNotIn("-t", cmd)

    def test_text_when_no_recipe(self):
        cmd = build_goose_cmd("hello", "", recipe_root=Path("/missing"))
        self.assertIn("-t", cmd)
        self.assertEqual(cmd[cmd.index("-t") + 1], "hello")
        self.assertNotIn("--recipe", cmd)


if __name__ == "__main__":
    unittest.main()
