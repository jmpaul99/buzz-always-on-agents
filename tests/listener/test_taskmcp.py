from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "goose"))
sys.path.insert(0, str(ROOT / "listener"))

import generate_recipes  # noqa: E402
import taskmcp  # noqa: E402

CONFIG = (ROOT / "goose" / "config.yaml").read_text(encoding="utf-8")


class ParseConfigTest(unittest.TestCase):
    def test_github_and_playwright(self):
        ext = taskmcp.parse_goose_extensions(CONFIG)
        self.assertEqual(ext["github"]["type"], "streamable_http")
        self.assertFalse(ext["github"]["enabled"])
        self.assertIn("GITHUB_PERSONAL_ACCESS_TOKEN", ext["github"]["env_keys"])
        self.assertIn("Authorization", ext["github"]["headers"])
        self.assertEqual(ext["playwright"]["type"], "stdio")
        self.assertEqual(ext["playwright"]["cmd"], "npx")
        self.assertIn("@playwright/mcp@latest", ext["playwright"]["args"])
        self.assertTrue(ext["developer"]["enabled"])
        mcps = taskmcp.task_mcps(ext)
        self.assertIn("github", mcps)
        self.assertNotIn("developer", mcps)
        self.assertNotIn("skills", mcps)
        self.assertNotIn("scheduler", taskmcp.task_mcps(ext))
        self.assertNotIn("summon", taskmcp.task_mcps(ext))


class RecipeGenerateTest(unittest.TestCase):
    def test_github_and_playwright_recipes(self):
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw)
            catalog = out / "task-mcps.json"
            slugs = generate_recipes.write_recipes(CONFIG, out, catalog)
            self.assertIn("github", slugs)
            self.assertIn("playwright", slugs)
            self.assertNotIn("reply", slugs)
            reply = (out / "reply" / "recipe.yaml").read_text(encoding="utf-8")
            self.assertIn("buzz messages send", reply)
            self.assertIn("text instead of send", reply)
            self.assertIn("exactly one channel reply", reply)
            self.assertNotIn("key: channel", reply)
            self.assertNotIn("key: author", reply)
            self.assertNotIn("key: event_id", reply)
            self.assertIn("max_turns: 25", reply)
            self.assertIn("name: developer", reply)
            self.assertNotIn("name: github", reply)
            github = (out / "github" / "recipe.yaml").read_text(encoding="utf-8")
            self.assertIn("name: github", github)
            self.assertIn("name: developer", github)
            self.assertIn("name: tom", github)
            self.assertIn("{{ message }}", github)
            self.assertIn("{{ send_cmd }}", github)
            self.assertIn("{{ identity }}", github)
            self.assertIn("buzz messages send", github)
            self.assertIn("max_turns: 25", github)
            try:
                import yaml  # type: ignore
            except ImportError:
                yaml = None
            if yaml is not None:
                yaml.safe_load(reply)
                yaml.safe_load(github)
            self.assertNotIn("list_repositories", github)
            self.assertNotIn("available_tools", github)
            self.assertNotIn("extensionmanager", github.lower())
            self.assertNotIn("Extension Manager", github)
            self.assertTrue((out / "playwright" / "recipe.yaml").is_file())
            playwright = (out / "playwright" / "recipe.yaml").read_text(encoding="utf-8")
            self.assertIn("name: playwright", playwright)
            self.assertIn("name: developer", playwright)
            self.assertIn("@playwright/mcp@latest", playwright)
            self.assertIn("GITHUB_PERSONAL_ACCESS_TOKEN", github)


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.catalog = taskmcp.catalog_records(taskmcp.parse_goose_extensions(CONFIG))

    def test_github_only(self):
        self.assertEqual(
            taskmcp.match_task_recipe("list my github repos", self.catalog),
            "github",
        )

    def test_poem_and_github(self):
        self.assertEqual(
            taskmcp.match_task_recipe(
                "Write a poem, list my github repos, and react",
                self.catalog,
            ),
            "github",
        )

    def test_github_and_stripe(self):
        self.assertIsNone(taskmcp.match_task_recipe("github and stripe", self.catalog))

    def test_hello(self):
        self.assertIsNone(taskmcp.match_task_recipe("hello", self.catalog))


class CatalogFileTest(unittest.TestCase):
    def test_committed_catalog_matches_config(self):
        expected = taskmcp.catalog_records(taskmcp.parse_goose_extensions(CONFIG))
        path = ROOT / "listener" / "task-mcps.json"
        self.assertTrue(path.is_file())
        loaded = taskmcp.load_catalog(path)
        self.assertEqual(loaded, expected)


if __name__ == "__main__":
    unittest.main()
