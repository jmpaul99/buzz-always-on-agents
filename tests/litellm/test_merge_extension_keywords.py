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


if __name__ == "__main__":
    unittest.main()
