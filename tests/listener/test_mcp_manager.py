from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LISTENER = ROOT / "listener"
sys.path.insert(0, str(LISTENER))
sys.path.insert(0, str(LISTENER / "local-mcp"))

import mcp_catalog  # noqa: E402
import mcp_manager  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_stdio_mcp.py"


def _catalog(extras: list[dict]) -> dict:
    return {
        "always_on": [
            {
                "slug": "buzz-dev-mcp",
                "enabled": True,
                "command": sys.executable,
                "args": [str(FAKE), "--name", "dev", "--tool", "shell"],
                "env_keys": [],
            }
        ],
        "extras": extras,
    }


class McpManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.catalog_path = self.root / "mcp-catalog.json"
        self.overlay_path = self.root / "_mcp-overlay.json"
        self.workspace = self.root / "workspace"
        extra = {
            "slug": "github",
            "enabled": False,
            "name": "GitHub",
            "command": sys.executable,
            "args": [str(FAKE), "--name", "github", "--tool", "search"],
            "env_keys": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
        }
        self.catalog_path.write_text(json.dumps(_catalog([extra])), encoding="utf-8")
        self.overlay_path.write_text(json.dumps({"extras": []}), encoding="utf-8")
        self.environ = {
            "PATH": self._path(),
            "HOME": str(self.root),
            "AGENT_NAME": "fizz",
            "GITHUB_PERSONAL_ACCESS_TOKEN": "ghs_test",
            "BUZZ_PRIVATE_KEY": "nsec1secret",
            "BUZZ_RELAY_URL": "wss://example.communities.buzz.xyz",
            "BUZZ_AUTH_TAG": "[]",
            "LITELLM_MASTER_KEY": "sk-secret",
            "OPENAI_COMPAT_API_KEY": "sk-secret",
            "CUSTOM_KEY": "1",
        }
        self.manager = mcp_manager.Manager(
            catalog_path=self.catalog_path,
            overlay_path=self.overlay_path,
            agent_name="fizz",
            workspace=self.workspace,
            environ=self.environ,
        )

    def tearDown(self):
        self.manager.close()
        self.tmp.cleanup()

    def _path(self) -> str:
        import os

        return os.environ.get("PATH") or os.environ.get("Path") or ""

    def test_list_enable_disable_prefix_and_no_secret_leak(self):
        self.manager.start()
        listed = json.loads(self.manager.call_tool("mcp_list", {})["content"][0]["text"])
        self.assertEqual(listed["max_enabled"], 2)
        self.assertFalse(listed["extras"][0]["enabled"])
        names = {item["name"] for item in self.manager.list_tools()}
        self.assertIn("mcp_list", names)
        self.assertIn("shell", names)
        self.assertNotIn("github__search", names)

        enabled = json.loads(self.manager.call_tool("mcp_enable", {"slug": "github"})["content"][0]["text"])
        self.assertTrue(enabled["ok"])
        self.assertEqual(enabled["tools"][0]["name"], "github__search")
        names = {item["name"] for item in self.manager.list_tools()}
        self.assertIn("github__search", names)
        self.assertTrue(self.manager.enabled_file.is_file())
        self.assertEqual(mcp_catalog.load_enabled(self.manager.enabled_file), ["github"])

        result = self.manager.call_tool("github__search", {"text": "hello"})
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["text"], "hello")
        self.assertEqual(payload["leaked"], [])
        self.assertEqual(payload["server"], "github")

        dev = json.loads(self.manager.call_tool("shell", {"text": "who"})["content"][0]["text"])
        self.assertEqual(dev["server"], "dev")
        self.assertIn("BUZZ_PRIVATE_KEY", dev["leaked"])

        disabled = json.loads(self.manager.call_tool("mcp_disable", {"slug": "github"})["content"][0]["text"])
        self.assertTrue(disabled["ok"])
        names = {item["name"] for item in self.manager.list_tools()}
        self.assertNotIn("github__search", names)
        self.assertEqual(mcp_catalog.load_enabled(self.manager.enabled_file), [])

    def test_register_then_enable(self):
        self.manager.start()
        registered = json.loads(
            self.manager.call_tool(
                "mcp_register",
                {
                    "slug": "custommcp",
                    "name": "Custom",
                    "command": "python",
                    "args_json": json.dumps([str(FAKE), "--name", "custom", "--tool", "pinger"]),
                    "env_keys_json": json.dumps(["CUSTOM_KEY"]),
                },
            )["content"][0]["text"]
        )
        self.assertTrue(registered["ok"])
        self.assertFalse(registered["enabled"])
        overlay = mcp_catalog.load_overlay(self.overlay_path)
        self.assertEqual(overlay["extras"][0]["slug"], "custommcp")
        enabled = json.loads(self.manager.call_tool("mcp_enable", {"slug": "custommcp"})["content"][0]["text"])
        self.assertTrue(enabled["ok"])
        self.assertEqual(enabled["tools"][0]["name"], "custommcp__pinger")

    def test_enable_missing_env_and_cap(self):
        extra2 = {
            "slug": "stripe",
            "enabled": False,
            "name": "Stripe",
            "command": sys.executable,
            "args": [str(FAKE), "--name", "stripe", "--tool", "charge"],
            "env_keys": [],
        }
        extra3 = {
            "slug": "repomix",
            "enabled": False,
            "name": "Repomix",
            "command": sys.executable,
            "args": [str(FAKE), "--name", "repo", "--tool", "pack"],
            "env_keys": [],
        }
        self.catalog_path.write_text(json.dumps(_catalog([
            {
                "slug": "github",
                "enabled": False,
                "name": "GitHub",
                "command": sys.executable,
                "args": [str(FAKE), "--name", "github", "--tool", "search"],
                "env_keys": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
            },
            extra2,
            extra3,
        ])), encoding="utf-8")
        self.manager.catalog = mcp_catalog.load_catalog(self.catalog_path)
        self.manager.environ.pop("GITHUB_PERSONAL_ACCESS_TOKEN", None)
        self.manager.start()
        missing = json.loads(self.manager.call_tool("mcp_enable", {"slug": "github"})["content"][0]["text"])
        self.assertFalse(missing["ok"])
        self.assertIn("missing env", missing["error"])
        self.manager.environ["GITHUB_PERSONAL_ACCESS_TOKEN"] = "ghs_test"
        self.assertTrue(json.loads(self.manager.call_tool("mcp_enable", {"slug": "stripe"})["content"][0]["text"])["ok"])
        self.assertTrue(json.loads(self.manager.call_tool("mcp_enable", {"slug": "repomix"})["content"][0]["text"])["ok"])
        cap = json.loads(self.manager.call_tool("mcp_enable", {"slug": "github"})["content"][0]["text"])
        self.assertFalse(cap["ok"])
        self.assertIn("at most 2", cap["error"])

    def test_cannot_disable_always_on(self):
        self.manager.start()
        result = json.loads(self.manager.call_tool("mcp_disable", {"slug": "buzz-dev-mcp"})["content"][0]["text"])
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
