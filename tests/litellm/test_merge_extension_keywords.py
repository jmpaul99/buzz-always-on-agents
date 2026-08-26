from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "litellm"))

from merge_extension_keywords import disabled_extension_keywords, merge_keyword_block

ROOT = Path(__file__).resolve().parents[2]


class MergeExtensionKeywordsTest(unittest.TestCase):
    def test_collects_disabled_only(self):
        sample = """
extensions:
  github:
    enabled: false
    name: GitHub
    display_name: GitHub
  developer:
    enabled: true
    name: developer
    display_name: Developer
  playwright:
    enabled: false
    name: Playwright
    display_name: Playwright
active_provider: litellm
"""
        keys = disabled_extension_keywords(sample)
        self.assertIn("github", keys)
        self.assertIn("playwright", keys)
        self.assertNotIn("developer", keys)

    def test_merges_without_dropping_base(self):
        src = "        custom_technical_keywords:\n          [buzz, nostr]\n"
        out = merge_keyword_block(src, ["github", "playwright"])
        self.assertIn("buzz", out)
        self.assertIn("github", out)
        self.assertIn("playwright", out)

    def test_real_goose_config(self):
        text = (ROOT / "goose" / "config.yaml").read_text(encoding="utf-8")
        keys = disabled_extension_keywords(text)
        self.assertIn("github", keys)
        self.assertIn("playwright", keys)
        self.assertIn("stripe", keys)
        self.assertNotIn("developer", keys)


class RouterTargetsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = (ROOT / "litellm" / "config.yaml").read_text(encoding="utf-8")

    def test_groq_fast_is_120b(self):
        self.assertIn("groq/openai/gpt-oss-120b", self.cfg)
        self.assertIn("groq/openai/gpt-oss-20b", self.cfg)
        self.assertRegex(self.cfg, r"model_name: groq-fast\n(?:.*\n){1,4}.*gpt-oss-120b")

    def test_simple_prefers_tool_capable(self):
        self.assertIn("SIMPLE: [groq-fast, groq-qwen, gemini-flash]", self.cfg)
        self.assertIn("MEDIUM: [groq-qwen, gemini-flash, nemotron, deepseek-flash]", self.cfg)
        self.assertNotIn("SIMPLE: [groq-fast, deepseek-flash, gemini-lite]", self.cfg)
        self.assertIn("default_model: groq-fast", self.cfg)

    def test_weak_models_are_fallback_only(self):
        simple = next(line for line in self.cfg.splitlines() if "SIMPLE:" in line)
        self.assertNotIn("gemini-lite", simple)
        self.assertNotIn("groq-20b", simple)
        self.assertIn("- gemini-lite", self.cfg)
        self.assertIn("- groq-20b", self.cfg)


if __name__ == "__main__":
    unittest.main()
