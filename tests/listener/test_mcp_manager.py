from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
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
        return os.environ.get("PATH") or os.environ.get("Path") or ""

    def _wait_extra(self, slug: str, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if slug in self.manager.extras:
                return
            time.sleep(0.05)
        self.fail(f"extra {slug} did not start")

    def test_write_rpc_uses_newline_json(self):
        import io

        buf = io.BytesIO()
        mcp_manager.write_rpc(buf, {"jsonrpc": "2.0", "id": 1, "result": {}})
        raw = buf.getvalue()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertFalse(raw.lower().startswith(b"content-length:"))
        buf.seek(0)
        parsed = mcp_manager.read_rpc(buf)
        self.assertEqual(parsed, {"jsonrpc": "2.0", "id": 1, "result": {}})

        framed = io.BytesIO()
        mcp_manager.write_rpc(
            framed,
            {"jsonrpc": "2.0", "id": 2, "result": {}},
            mcp_manager.FRAME_CONTENT_LENGTH,
        )
        self.assertTrue(framed.getvalue().lower().startswith(b"content-length:"))
        framed.seek(0)
        self.assertEqual(mcp_manager.read_rpc(framed)["id"], 2)

    def test_list_enable_disable_prefix_and_no_secret_leak(self):
        self.manager.start()
        listed = json.loads(self.manager.call_tool("mcp_list", {})["content"][0]["text"])
        self.assertEqual(listed["max_enabled"], 2)
        self.assertFalse(listed["extras"][0]["enabled"])
        self.assertEqual(listed["always_on"][0]["slug"], "buzz-dev-mcp")
        self.assertIn("shell", listed["always_on"][0]["tools"])
        names = {item["name"] for item in self.manager.list_tools()}
        self.assertIn("mcp_list", names)
        self.assertIn("mcp_tools", names)
        self.assertIn("shell", names)
        self.assertNotIn("github_search", names)
        self.assertTrue(all("__" not in item["name"] for item in self.manager.list_tools()))

        enabled = json.loads(self.manager.call_tool("mcp_enable", {"slug": "github"})["content"][0]["text"])
        self.assertTrue(enabled["ok"])
        self.assertTrue(enabled["starting"])
        self._wait_extra("github")
        listed = json.loads(self.manager.call_tool("mcp_list", {})["content"][0]["text"])
        github = next(item for item in listed["extras"] if item["slug"] == "github")
        self.assertEqual(github["status"], "running")
        self.assertTrue(github["enabled"])
        self.assertGreater(github["tool_count"], 0)
        names = {item["name"] for item in self.manager.list_tools()}
        self.assertIn("github_search", names)
        self.assertIn("shell", names)
        self.assertTrue(all("__" not in item["name"] for item in self.manager.list_tools()))
        self.assertTrue(self.manager.enabled_file.is_file())
        self.assertEqual(mcp_catalog.load_enabled(self.manager.enabled_file), ["github"])

        result = self.manager.call_tool("github_search", {"text": "hello"})
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
        self.assertNotIn("github_search", names)
        self.assertEqual(mcp_catalog.load_enabled(self.manager.enabled_file), [])

    def test_enable_github_from_runtime_env_file(self):
        runtime = self.root / "_runtime.env"
        runtime.write_text("GITHUB_PERSONAL_ACCESS_TOKEN=ghs_runtime\n", encoding="utf-8")
        self.manager.close()
        env = dict(self.environ)
        env.pop("GITHUB_PERSONAL_ACCESS_TOKEN", None)
        self.manager = mcp_manager.Manager(
            catalog_path=self.catalog_path,
            overlay_path=self.overlay_path,
            agent_name="fizz",
            workspace=self.workspace,
            environ=env,
            runtime_path=runtime,
        )
        self.manager.start()
        listed = json.loads(self.manager.call_tool("mcp_list", {})["content"][0]["text"])
        github = next(item for item in listed["extras"] if item["slug"] == "github")
        self.assertEqual(github["missing_env_keys"], [])
        enabled = json.loads(self.manager.call_tool("mcp_enable", {"slug": "github"})["content"][0]["text"])
        self.assertTrue(enabled["ok"])
        self._wait_extra("github")

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
        self._wait_extra("custommcp")
        names = {item["name"] for item in self.manager.list_tools()}
        self.assertIn("custommcp_pinger", names)

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

    def test_serve_answers_initialize_before_child_ready(self):
        """buzz-agent's MCP init deadline is 30s; child spawn must not block it."""
        hang = threading.Event()

        def hang_spawn(spec, env):
            hang.wait(timeout=30)
            raise RuntimeError("child hung")

        self.manager._spawn = hang_spawn
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()
        server = threading.Thread(
            target=mcp_manager.serve,
            args=(
                self.manager,
                os.fdopen(stdin_r, "rb", buffering=0),
                os.fdopen(stdout_w, "wb", buffering=0),
            ),
            daemon=True,
        )
        reply: dict[str, bytes] = {}

        def _read() -> None:
            reply["raw"] = os.read(stdout_r, 4096)

        reader = threading.Thread(target=_read, daemon=True)
        reader.start()
        server.start()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        }
        started = time.monotonic()
        os.write(stdin_w, json.dumps(payload).encode("utf-8") + b"\n")
        reader.join(timeout=2)
        elapsed = time.monotonic() - started
        hang.set()
        os.close(stdin_w)
        self.assertFalse(reader.is_alive())
        self.assertLess(elapsed, 1.5)
        raw = reply.get("raw") or b""
        self.assertIn(b"buzz-mcp-manager", raw)
        parsed = json.loads(raw.splitlines()[0])
        self.assertEqual(parsed["result"]["serverInfo"]["name"], "buzz-mcp-manager")

    def test_enable_returns_before_extra_spawn(self):
        hang = threading.Event()
        real = self.manager._spawn

        def maybe_hang(spec, env):
            args = spec.get("args") or []
            if "github" in args:
                hang.wait(timeout=30)
                raise RuntimeError("extra hung")
            return real(spec, env)

        self.manager.start()
        self.manager._spawn = maybe_hang
        started = time.monotonic()
        enabled = json.loads(self.manager.call_tool("mcp_enable", {"slug": "github"})["content"][0]["text"])
        elapsed = time.monotonic() - started
        hang.set()
        self.assertTrue(enabled["ok"])
        self.assertTrue(enabled["starting"])
        self.assertLess(elapsed, 1.5)
        shell = json.loads(self.manager.call_tool("shell", {"text": "who"})["content"][0]["text"])
        self.assertEqual(shell["server"], "dev")
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if "github" in self.manager.last_error and "github" not in self.manager._starting:
                break
            time.sleep(0.05)
        self.assertIn("github", self.manager.last_error)
        self.assertEqual(mcp_catalog.load_enabled(self.manager.enabled_file), [])
        listed = json.loads(self.manager.call_tool("mcp_list", {})["content"][0]["text"])
        github = next(item for item in listed["extras"] if item["slug"] == "github")
        self.assertEqual(github["status"], "failed")
        self.assertFalse(github["enabled"])
        self.assertIn("extra hung", github["last_error"] or "")
        tools = json.loads(self.manager.call_tool("mcp_tools", {"slug": "github"})["content"][0]["text"])
        self.assertFalse(tools["ok"])
        self.assertEqual(tools["status"], "failed")

    def test_list_tools_keeps_always_on_if_dev_dies(self):
        self.manager.start()
        self.assertIn("shell", {item["name"] for item in self.manager.list_tools()})
        self.manager.dev.close()
        names = {item["name"] for item in self.manager.list_tools()}
        self.assertIn("shell", names)

    def test_extra_tools_are_paged_but_still_callable(self):
        extra = {
            "slug": "github",
            "enabled": False,
            "name": "GitHub",
            "command": sys.executable,
            "args": [str(FAKE), "--name", "github", "--tool", "t", "--count", "30"],
            "env_keys": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
        }
        self.catalog_path.write_text(json.dumps(_catalog([extra])), encoding="utf-8")
        self.manager.catalog = mcp_catalog.load_catalog(self.catalog_path)
        self.manager.start()
        self.assertTrue(json.loads(self.manager.call_tool("mcp_enable", {"slug": "github"})["content"][0]["text"])["ok"])
        self._wait_extra("github")
        names = [item["name"] for item in self.manager.list_tools()]
        extra_names = [n for n in names if n.startswith("github_")]
        self.assertEqual(len(extra_names), mcp_catalog.EXTRA_PAGE_SIZE)
        self.assertIn("github_t00", extra_names)
        self.assertNotIn("github_t12", extra_names)
        self.assertIn("shell", names)
        payload = json.loads(self.manager.call_tool("github_t12", {"text": "later"})["content"][0]["text"])
        self.assertEqual(payload["text"], "later")
        page = json.loads(self.manager.call_tool("mcp_tools", {"cursor": "12"})["content"][0]["text"])
        self.assertEqual(page["tools"][0]["name"], "github_t12")
        self.assertEqual(page["next_cursor"], "24")

    def test_dead_extra_does_not_drop_always_on(self):
        self.manager.start()
        self.assertTrue(json.loads(self.manager.call_tool("mcp_enable", {"slug": "github"})["content"][0]["text"])["ok"])
        self._wait_extra("github")
        self.manager.extras["github"].close()
        names = {item["name"] for item in self.manager.list_tools()}
        self.assertIn("shell", names)
        self.assertIn("mcp_list", names)

    def test_stop_hook_nags_until_messages_send(self):
        self.manager.start()
        nag = self.manager.call_tool("_Stop", {})["content"][0]["text"]
        self.assertIn("run-mcp__shell", nag)
        self.assertIn("ACP Activity", nag)
        self.manager.call_tool("shell", {"command": "echo hello"})
        still = self.manager.call_tool("_Stop", {})["content"][0]["text"]
        self.assertIn("run-mcp__shell", still)
        self.manager.call_tool(
            "shell",
            {"command": "buzz messages send --channel abc --content hi"},
        )
        done = self.manager.call_tool("_Stop", {})["content"][0]["text"]
        self.assertNotIn("run-mcp__shell", done)


if __name__ == "__main__":
    unittest.main()
