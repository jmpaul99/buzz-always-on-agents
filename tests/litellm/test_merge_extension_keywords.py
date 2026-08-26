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
        self.assertIn("SIMPLE: [groq-qwen, groq-fast, gemini-flash]", self.cfg)
        self.assertIn("MEDIUM: [groq-qwen, gemini-flash, glm, kimi]", self.cfg)
        self.assertIn("COMPLEX: [glm, kimi, deepseek-pro, minimax-m27]", self.cfg)
        self.assertIn("REASONING: [glm, kimi, gemini-flash]", self.cfg)
        self.assertNotIn("SIMPLE: [groq-fast, deepseek-flash, gemini-lite]", self.cfg)
        self.assertIn("default_model: groq-qwen", self.cfg)

    def test_agentic_slugs_present(self):
        self.assertIn("nvidia_nim/z-ai/glm-5.2", self.cfg)
        self.assertIn("nvidia_nim/moonshotai/kimi-k2.6", self.cfg)
        self.assertIn("nvidia_nim/deepseek-ai/deepseek-v4-pro", self.cfg)
        self.assertIn("nvidia_nim/minimaxai/minimax-m2.7", self.cfg)
        self.assertIn("openrouter/stealth/ox-alpha", self.cfg)
        self.assertIn("openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", self.cfg)
        self.assertIn("openrouter/poolside/laguna-s-2.1:free", self.cfg)

    def test_weak_models_not_on_goose_path(self):
        simple = next(line for line in self.cfg.splitlines() if "SIMPLE:" in line)
        self.assertNotIn("gemini-lite", simple)
        self.assertNotIn("groq-20b", simple)
        fallbacks = self.cfg.split("default_fallbacks:", 1)[1].split("general_settings:", 1)[0]
        for weak in ("gemini-lite", "groq-20b", "openrouter-free", "openrouter-cheap"):
            self.assertNotIn(weak, fallbacks)
        self.assertIn("model_name: gemini-lite", self.cfg)
        self.assertIn("model_name: groq-20b", self.cfg)


if __name__ == "__main__":
    unittest.main()
