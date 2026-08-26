from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "listener"))

import mcp_catalog  # noqa: E402


class McpCatalogTest(unittest.TestCase):
    def setUp(self):
        self.catalog = mcp_catalog.load_catalog()

    def test_always_on_is_buzz_dev_mcp(self):
        always = mcp_catalog.entries(self.catalog, "always_on")
        self.assertEqual(len(always), 1)
        self.assertEqual(always[0]["slug"], "buzz-dev-mcp")
        self.assertTrue(always[0]["enabled"])
        self.assertEqual(always[0]["command"], "buzz-dev-mcp")

    def test_no_browser_slugs(self):
        slugs = {item["slug"] for item in mcp_catalog.entries(self.catalog, "always_on")}
        slugs |= {item["slug"] for item in mcp_catalog.entries(self.catalog, "extras")}
        for banned in mcp_catalog.BROWSER_SLUGS:
            self.assertNotIn(banned, slugs)

    def test_extras_disabled(self):
        extras = mcp_catalog.entries(self.catalog, "extras")
        wanted = {
            "github",
            "stripe",
            "tavilywebsearch",
            "googleadc",
            "containeruse",
            "linuxmcpserver",
            "repomix",
            "youtubetranscript",
        }
        self.assertEqual({item["slug"] for item in extras}, wanted)
        self.assertTrue(all(item.get("enabled") is False for item in extras))

    def test_http_extras_use_mcp_remote(self):
        by_slug = {item["slug"]: item for item in mcp_catalog.entries(self.catalog, "extras")}
        self.assertEqual(by_slug["github"]["command"], "npx")
        self.assertIn("mcp-remote", by_slug["github"]["args"])
        self.assertIn("mcp-remote", by_slug["stripe"]["args"])
        self.assertIn("GITHUB_PERSONAL_ACCESS_TOKEN", by_slug["github"]["env_keys"])
        self.assertIn("STRIPE_API_KEY", by_slug["stripe"]["env_keys"])

    def test_keywords_from_disabled_extras(self):
        keys = mcp_catalog.extra_keywords(self.catalog)
        self.assertIn("github", keys)
        self.assertIn("stripe", keys)
        self.assertNotIn("playwright", keys)
        self.assertNotIn("buzz-dev-mcp", keys)


if __name__ == "__main__":
    unittest.main()
