from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HINTS = (ROOT / "goose" / ".goosehints").read_text(encoding="utf-8").lower()
GUARDRAILS = (ROOT / "goose" / "guardrails.md").read_text(encoding="utf-8").lower()


class GuardrailsTest(unittest.TestCase):
    def test_extension_manager_budget(self):
        self.assertIn("5 enabled extensions", GUARDRAILS)
        self.assertIn("50 tools", GUARDRAILS)
        self.assertIn("search_available_extensions", GUARDRAILS)
        self.assertIn("list_functions", GUARDRAILS)
        self.assertIn("disable", GUARDRAILS)
        self.assertIn("extension__tool", GUARDRAILS)
        self.assertIn("never invent", GUARDRAILS)
        self.assertNotIn("github__search_repositories", GUARDRAILS)
        self.assertNotIn("list_repositories", GUARDRAILS)
        self.assertIn("do not enable code mode", GUARDRAILS)
        self.assertIn("buzz messages send", GUARDRAILS)


class GoosehintsTest(unittest.TestCase):
    def test_session_context_only(self):
        self.assertIn("buzz cloud", HINTS)
        self.assertIn("buzz messages send", HINTS)
        self.assertIn("text-only answer is not delivered", HINTS)
        self.assertIn("buzz reactions", HINTS)
        self.assertNotIn("5 enabled extensions", HINTS)
        self.assertNotIn("search_available_extensions", HINTS)
        self.assertNotIn("list_repositories", HINTS)


if __name__ == "__main__":
    unittest.main()
