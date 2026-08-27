from __future__ import annotations

import sys
import tempfile
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

    def test_googleadc_points_at_vm_path_and_repo_source(self):
        by_slug = {item["slug"]: item for item in mcp_catalog.entries(self.catalog, "extras")}
        adc = by_slug["googleadc"]
        self.assertIn("/opt/buzz/local-mcp/google_adc_mcp.py", adc["args"])
        src = Path(__file__).resolve().parents[2] / "listener" / "local-mcp" / "google_adc_mcp.py"
        self.assertTrue(src.is_file())

    def test_keywords_from_disabled_extras(self):
        keys = mcp_catalog.extra_keywords(self.catalog)
        self.assertIn("github", keys)
        self.assertIn("stripe", keys)
        self.assertNotIn("playwright", keys)
        self.assertNotIn("buzz-dev-mcp", keys)


class OverlayAndEnabledTest(unittest.TestCase):
    def setUp(self):
        self.catalog = mcp_catalog.load_catalog()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.overlay_path = self.root / "_mcp-overlay.json"
        self.enabled_file = self.root / "agents" / "fizz" / "mcp-enabled.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge_overlay_appends_new_slug_catalog_wins(self):
        overlay = {
            "extras": [
                {"slug": "github", "command": "npx", "args": ["-y", "ignored"], "env_keys": []},
                {"slug": "custommcp", "command": "npx", "args": ["-y", "tavily-mcp"], "env_keys": []},
            ]
        }
        merged = mcp_catalog.merge_extras(self.catalog, overlay)
        by_slug = {item["slug"]: item for item in merged}
        self.assertEqual(by_slug["github"]["_source"], "catalog")
        self.assertIn("mcp-remote", by_slug["github"]["args"])
        self.assertEqual(by_slug["custommcp"]["_source"], "overlay")

    def test_register_rejects_banned_unknown_command_and_missing_env(self):
        with self.assertRaisesRegex(ValueError, "not allowed"):
            mcp_catalog.validate_register(
                {"slug": "playwright", "command": "npx", "args": ["-y", "playwright-mcp"], "env_keys": []},
                catalog=self.catalog,
                overlay={"extras": []},
                env={},
            )
        with self.assertRaisesRegex(ValueError, "command must be one of"):
            mcp_catalog.validate_register(
                {"slug": "evil", "command": "bash", "args": ["-c", "true"], "env_keys": []},
                catalog=self.catalog,
                overlay={"extras": []},
                env={},
            )
        with self.assertRaisesRegex(ValueError, "already exists"):
            mcp_catalog.validate_register(
                {"slug": "github", "command": "npx", "args": ["-y", "mcp-remote", "https://example.com"], "env_keys": []},
                catalog=self.catalog,
                overlay={"extras": []},
                env={},
            )
        with self.assertRaisesRegex(ValueError, "not set"):
            mcp_catalog.validate_register(
                {
                    "slug": "gh2",
                    "command": "npx",
                    "args": ["-y", "mcp-remote", "https://example.com/mcp"],
                    "env_keys": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
                },
                catalog=self.catalog,
                overlay={"extras": []},
                env={},
            )
        with self.assertRaisesRegex(ValueError, "not allowed"):
            mcp_catalog.validate_register(
                {
                    "slug": "evilrelay",
                    "command": "npx",
                    "args": ["-y", "mcp-remote", "https://example.com/mcp"],
                    "env_keys": ["BUZZ_RELAY_URL"],
                },
                catalog=self.catalog,
                overlay={"extras": []},
                env={"BUZZ_RELAY_URL": "wss://example.communities.buzz.xyz"},
            )

    def test_register_npx_and_python_and_append_overlay(self):
        script = self.root / "server.py"
        script.write_text("print(1)\n", encoding="utf-8")
        spec = mcp_catalog.validate_register(
            {
                "slug": "custommcp",
                "name": "Custom",
                "command": "python",
                "args": [str(script)],
                "env_keys": ["CUSTOM_KEY"],
            },
            catalog=self.catalog,
            overlay={"extras": []},
            env={"CUSTOM_KEY": "1"},
        )
        self.assertEqual(spec["slug"], "custommcp")
        self.assertFalse(spec["enabled"])
        saved = mcp_catalog.append_overlay(
            spec,
            overlay_path=self.overlay_path,
            catalog=self.catalog,
            env={"CUSTOM_KEY": "1"},
        )
        overlay = mcp_catalog.load_overlay(self.overlay_path)
        self.assertEqual(overlay["extras"][0]["slug"], saved["slug"])

    def test_enabled_set_cap(self):
        enabled = mcp_catalog.enable_slug([], "github")
        enabled = mcp_catalog.enable_slug(enabled, "stripe")
        with self.assertRaisesRegex(ValueError, "at most 2"):
            mcp_catalog.enable_slug(enabled, "tavilywebsearch")
        mcp_catalog.save_enabled(self.enabled_file, enabled)
        self.assertEqual(mcp_catalog.load_enabled(self.enabled_file), ["github", "stripe"])
        self.assertEqual(mcp_catalog.disable_slug(enabled, "github"), ["stripe"])

    def test_child_env_trusted_vs_untrusted(self):
        parent = {
            "PATH": "/usr/bin",
            "HOME": "/tmp",
            "BUZZ_PRIVATE_KEY": "nsec1secret",
            "NOSTR_PRIVATE_KEY": "nsec1secret",
            "BUZZ_RELAY_URL": "wss://example.communities.buzz.xyz",
            "BUZZ_AUTH_TAG": "[\"auth\"]",
            "BUZZ_ACP_DISPLAY_NAME": "Fizz",
            "LITELLM_MASTER_KEY": "sk-secret",
            "OPENAI_COMPAT_API_KEY": "sk-secret",
            "BUZZ_SYNC_TOKEN": "sync-secret",
            "GITHUB_PERSONAL_ACCESS_TOKEN": "ghs_x",
            "GOOGLE_CLOUD_PROJECT": "proj",
        }
        extra = mcp_catalog.child_env(["GITHUB_PERSONAL_ACCESS_TOKEN", "BUZZ_RELAY_URL"], parent, trusted=False)
        self.assertEqual(extra["GITHUB_PERSONAL_ACCESS_TOKEN"], "ghs_x")
        self.assertEqual(extra["GOOGLE_CLOUD_PROJECT"], "proj")
        for key in mcp_catalog.BUZZ_IDENTITY_ENV:
            self.assertNotIn(key, extra)
        self.assertNotIn("LITELLM_MASTER_KEY", extra)
        self.assertNotIn("OPENAI_COMPAT_API_KEY", extra)
        self.assertNotIn("BUZZ_SYNC_TOKEN", extra)

        trusted = mcp_catalog.child_env([], parent, trusted=True)
        self.assertEqual(trusted["BUZZ_PRIVATE_KEY"], "nsec1secret")
        self.assertEqual(trusted["BUZZ_RELAY_URL"], "wss://example.communities.buzz.xyz")
        self.assertEqual(trusted["BUZZ_AUTH_TAG"], "[\"auth\"]")
        self.assertEqual(trusted["BUZZ_ACP_DISPLAY_NAME"], "Fizz")
        self.assertNotIn("LITELLM_MASTER_KEY", trusted)
        self.assertNotIn("GITHUB_PERSONAL_ACCESS_TOKEN", trusted)

    def test_tool_prefix(self):
        self.assertEqual(mcp_catalog.extra_tool_name("github", "search"), "github__search")
        self.assertEqual(mcp_catalog.split_extra_tool("github__search", ["github", "stripe"]), ("github", "search"))
        self.assertIsNone(mcp_catalog.split_extra_tool("shell", ["github"]))



if __name__ == "__main__":
    unittest.main()
