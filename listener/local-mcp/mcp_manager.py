"""Stdio MCP multiplexer: buzz-dev-mcp plus per-agent catalog extras.

buzz-acp attaches one MCP command. This process is that command. It proxies
buzz-dev-mcp, optionally spawns extras, and exposes mcp_list / mcp_enable /
mcp_disable / mcp_register.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, IO

_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE.parent, Path("/opt/buzz-listener")):
    if (_candidate / "mcp_catalog.py").is_file():
        sys.path.insert(0, str(_candidate))
        break

import mcp_catalog  # noqa: E402

PROTOCOL = "2024-11-05"
MANAGER_TOOLS = (
    {
        "name": "mcp_list",
        "description": (
            "List shipped and registered MCP extras for this agent, whether they "
            "are enabled, and any missing env keys. At most "
            f"{mcp_catalog.MAX_ENABLED} extras can be enabled."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "mcp_enable",
        "description": (
            "Enable a catalog extra for this agent, spawn it, and return its tool "
            "names. Tools are guaranteed on the next mention. Cap is "
            f"{mcp_catalog.MAX_ENABLED} extras."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"slug": {"type": "string"}},
            "required": ["slug"],
            "additionalProperties": False,
        },
    },
    {
        "name": "mcp_disable",
        "description": "Disable a catalog extra for this agent and stop its process. Cannot disable buzz-dev-mcp.",
        "inputSchema": {
            "type": "object",
            "properties": {"slug": {"type": "string"}},
            "required": ["slug"],
            "additionalProperties": False,
        },
    },
    {
        "name": "mcp_register",
        "description": (
            "Register a new extra spawn spec in the VM overlay (not git). Does not "
            "enable it. command must be npx, uv, uvx, python, or python3."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "name": {"type": "string"},
                "command": {"type": "string"},
                "args_json": {"type": "string", "description": "JSON array of strings"},
                "env_keys_json": {"type": "string", "description": "JSON array of env var names already set on the VM"},
            },
            "required": ["slug", "command", "args_json"],
            "additionalProperties": False,
        },
    },
)


def _json_bytes(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


def read_rpc(stream: IO[bytes]) -> dict[str, Any] | None:
    header = stream.readline()
    if not header:
        return None
    if header.lower().startswith(b"content-length:"):
        try:
            length = int(header.split(b":", 1)[1].strip())
        except ValueError:
            return None
        while True:
            extra = stream.readline()
            if extra in (b"\r\n", b"\n", b""):
                break
        body = stream.read(length)
        if not body:
            return None
        parsed = json.loads(body.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else None
    line = header.strip()
    if not line:
        return read_rpc(stream)
    parsed = json.loads(line.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else None


def write_rpc(stream: IO[bytes], msg: dict[str, Any]) -> None:
    stream.write(_json_bytes(msg))
    stream.flush()


class StdioMcpClient:
    def __init__(self, proc: subprocess.Popen[bytes], timeout: float = 45.0) -> None:
        self.proc = proc
        self.timeout = timeout
        self._id = 0
        self._pending: dict[int, dict[str, Any] | None] = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._dead = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        stdout = self.proc.stdout
        if stdout is None:
            self._mark_dead()
            return
        try:
            while True:
                msg = read_rpc(stdout)
                if msg is None:
                    break
                if "id" not in msg:
                    continue
                try:
                    rid = int(msg["id"])
                except (TypeError, ValueError):
                    continue
                with self._cond:
                    if rid in self._pending:
                        self._pending[rid] = msg
                        self._cond.notify_all()
        except Exception:
            pass
        self._mark_dead()

    def _mark_dead(self) -> None:
        with self._cond:
            self._dead = True
            self._cond.notify_all()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        stdin = self.proc.stdin
        if stdin is None:
            raise RuntimeError("child stdin closed")
        with self._cond:
            self._id += 1
            rid = self._id
            self._pending[rid] = None
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            write_rpc(stdin, msg)
        except BrokenPipeError as exc:
            raise RuntimeError("child stdin closed") from exc
        deadline = time.monotonic() + self.timeout
        with self._cond:
            while self._pending.get(rid) is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._dead:
                    self._pending.pop(rid, None)
                    raise RuntimeError(f"timeout waiting for {method}")
                self._cond.wait(timeout=remaining)
            reply = self._pending.pop(rid)
        if not reply:
            raise RuntimeError(f"no reply for {method}")
        if "error" in reply:
            err = reply["error"]
            raise RuntimeError(err if isinstance(err, str) else json.dumps(err))
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        stdin = self.proc.stdin
        if stdin is None:
            return
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        try:
            write_rpc(stdin, msg)
        except BrokenPipeError:
            pass

    def initialize(self) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "buzz-mcp-manager", "version": "1.0"},
            },
        )
        self.notify("notifications/initialized")
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        result = self.request("tools/list", {})
        tools = result.get("tools") or []
        if not isinstance(tools, list):
            return []
        return [item for item in tools if isinstance(item, dict) and item.get("name")]

    def call_tool(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
                self.proc.wait(timeout=2)
            except Exception:
                pass
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
        self._mark_dead()


def _drain(stream: IO[bytes]) -> None:
    try:
        while stream.read(4096):
            pass
    except Exception:
        pass


def spawn_mcp(spec: dict[str, Any], env: dict[str, str]) -> StdioMcpClient:
    command = str(spec.get("command") or "")
    args = spec.get("args") or []
    if not isinstance(args, list):
        args = []
    argv = [command, *[str(item) for item in args]]
    resolved = shutil.which(command, path=env.get("PATH"))
    if resolved:
        argv[0] = resolved
    elif command in {"python", "python3"}:
        argv[0] = sys.executable
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if proc.stderr is not None:
        threading.Thread(target=_drain, args=(proc.stderr,), daemon=True).start()
    client = StdioMcpClient(proc)
    try:
        client.initialize()
    except Exception:
        client.close()
        raise RuntimeError("MCP child failed to start") from None
    return client


class Manager:
    def __init__(
        self,
        *,
        catalog_path: Path | None = None,
        overlay_path: Path | None = None,
        enabled_file: Path | None = None,
        agent_name: str | None = None,
        workspace: Path | None = None,
        environ: dict[str, str] | None = None,
        spawn: Any = spawn_mcp,
    ) -> None:
        self.environ = environ if environ is not None else dict(os.environ)
        self.catalog_path = catalog_path or mcp_catalog.default_catalog_path()
        self.overlay_path = overlay_path or mcp_catalog.default_overlay_path()
        self.workspace = workspace or mcp_catalog.default_workspace()
        self.agent_name = (agent_name or self.environ.get("AGENT_NAME") or "").strip()
        self.enabled_file = enabled_file or (
            mcp_catalog.enabled_path(self.agent_name, self.workspace) if self.agent_name else self.workspace / "mcp-enabled.json"
        )
        self._spawn = spawn
        self.catalog = mcp_catalog.load_catalog(self.catalog_path)
        self.dev: StdioMcpClient | None = None
        self.extras: dict[str, StdioMcpClient] = {}
        self.extra_tools: dict[str, list[dict[str, Any]]] = {}

    def overlay(self) -> dict[str, Any]:
        return mcp_catalog.load_overlay(self.overlay_path)

    def enabled_slugs(self) -> list[str]:
        return mcp_catalog.load_enabled(self.enabled_file)

    def start(self) -> None:
        spec = mcp_catalog.always_on_spec(self.catalog)
        env = mcp_catalog.child_env(list(spec.get("env_keys") or []), self.environ, trusted=True)
        command = self.environ.get("BUZZ_DEV_MCP_COMMAND") or spec.get("command") or "buzz-dev-mcp"
        args = spec.get("args") or []
        if self.environ.get("BUZZ_DEV_MCP_ARGS"):
            args = json.loads(self.environ["BUZZ_DEV_MCP_ARGS"])
        self.dev = self._spawn({"command": command, "args": args}, env)
        for slug in self.enabled_slugs():
            try:
                self._spawn_extra(slug, persist=False)
            except Exception:
                continue

    def close(self) -> None:
        for client in list(self.extras.values()):
            client.close()
        self.extras.clear()
        if self.dev:
            self.dev.close()
            self.dev = None

    def _spawn_extra(self, slug: str, persist: bool) -> list[dict[str, Any]]:
        item = mcp_catalog.find_extra(slug, self.catalog, self.overlay())
        if item is None:
            raise ValueError(f"unknown extra {slug}")
        if slug in mcp_catalog.BROWSER_SLUGS:
            raise ValueError(f"{slug} is not allowed")
        missing = mcp_catalog.missing_env_keys(item, self.environ)
        if missing:
            raise ValueError(f"missing env: {', '.join(missing)}")
        if slug in self.extras:
            return self.extra_tools.get(slug) or []
        env = mcp_catalog.child_env(list(item.get("env_keys") or []), self.environ, trusted=False)
        try:
            client = self._spawn(item, env)
        except Exception:
            raise RuntimeError(f"extra {slug} failed to start") from None
        tools = []
        for tool in client.list_tools():
            row = dict(tool)
            row["name"] = mcp_catalog.extra_tool_name(slug, str(tool["name"]))
            tools.append(row)
        self.extras[slug] = client
        self.extra_tools[slug] = tools
        if persist:
            enabled = mcp_catalog.enable_slug(self.enabled_slugs(), slug)
            mcp_catalog.save_enabled(self.enabled_file, enabled)
        return tools

    def list_payload(self) -> dict[str, Any]:
        enabled = self.enabled_slugs()
        extras = [
            mcp_catalog.extra_status(item, enabled, self.environ)
            for item in mcp_catalog.merge_extras(self.catalog, self.overlay())
        ]
        return {
            "max_enabled": mcp_catalog.MAX_ENABLED,
            "enabled": enabled,
            "extras": extras,
        }

    def enable(self, slug: str) -> dict[str, Any]:
        slug = (slug or "").strip().lower()
        if slug in mcp_catalog.always_on_slugs(self.catalog):
            raise ValueError(f"{slug} is always-on")
        if slug not in self.extras:
            enabled = self.enabled_slugs()
            if slug not in enabled:
                mcp_catalog.enable_slug(enabled, slug)
        tools = self._spawn_extra(slug, persist=True)
        return {
            "ok": True,
            "slug": slug,
            "tools": [{"name": t.get("name"), "description": t.get("description") or ""} for t in tools],
            "note": "Tools are listed at session start and are guaranteed on the next mention.",
        }

    def disable(self, slug: str) -> dict[str, Any]:
        slug = (slug or "").strip().lower()
        if slug in mcp_catalog.always_on_slugs(self.catalog):
            raise ValueError("cannot disable buzz-dev-mcp")
        client = self.extras.pop(slug, None)
        self.extra_tools.pop(slug, None)
        if client:
            client.close()
        mcp_catalog.save_enabled(self.enabled_file, mcp_catalog.disable_slug(self.enabled_slugs(), slug))
        return {"ok": True, "slug": slug, "enabled": False}

    def register(
        self,
        slug: str,
        command: str,
        args_json: str,
        name: str = "",
        env_keys_json: str = "[]",
    ) -> dict[str, Any]:
        cleaned = mcp_catalog.append_overlay(
            {
                "slug": slug,
                "name": name or slug,
                "command": command,
                "args": args_json,
                "env_keys": env_keys_json,
            },
            overlay_path=self.overlay_path,
            catalog=self.catalog,
            overlay=self.overlay(),
            env=self.environ,
        )
        return {
            "ok": True,
            "slug": cleaned["slug"],
            "enabled": False,
            "note": "Registered in the VM overlay. Call mcp_enable to attach it for this agent.",
        }

    def list_tools(self) -> list[dict[str, Any]]:
        tools = [dict(item) for item in MANAGER_TOOLS]
        if self.dev:
            tools.extend(self.dev.list_tools())
        for slug in self.extras:
            tools.extend(self.extra_tools.get(slug) or [])
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        args = arguments or {}
        try:
            if name == "mcp_list":
                return _text(self.list_payload())
            if name == "mcp_enable":
                return _text(self.enable(str(args.get("slug") or "")))
            if name == "mcp_disable":
                return _text(self.disable(str(args.get("slug") or "")))
            if name == "mcp_register":
                return _text(
                    self.register(
                        str(args.get("slug") or ""),
                        str(args.get("command") or ""),
                        str(args.get("args_json") or "[]"),
                        str(args.get("name") or ""),
                        str(args.get("env_keys_json") or "[]"),
                    )
                )
        except Exception as exc:
            return _text({"ok": False, "error": str(exc)}, is_error=True)
        split = mcp_catalog.split_extra_tool(name, list(self.extras))
        if split:
            slug, tool = split
            return self.extras[slug].call_tool(tool, args)
        if self.dev:
            return self.dev.call_tool(name, args)
        return _text({"ok": False, "error": f"unknown tool {name}"}, is_error=True)


def _text(payload: Any, is_error: bool = False) -> dict[str, Any]:
    body = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    return {
        "content": [{"type": "text", "text": body}],
        "isError": is_error,
    }


def _result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def serve(manager: Manager, stdin: IO[bytes], stdout: IO[bytes]) -> None:
    manager.start()
    try:
        while True:
            msg = read_rpc(stdin)
            if msg is None:
                break
            method = str(msg.get("method") or "")
            req_id = msg.get("id")
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            if method == "initialize":
                write_rpc(
                    stdout,
                    _result(
                        req_id,
                        {
                            "protocolVersion": PROTOCOL,
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "buzz-mcp-manager", "version": "1.0"},
                        },
                    ),
                )
                continue
            if req_id is None:
                continue
            if method == "ping":
                write_rpc(stdout, _result(req_id, {}))
                continue
            if method == "tools/list":
                write_rpc(stdout, _result(req_id, {"tools": manager.list_tools()}))
                continue
            if method == "tools/call":
                name = str(params.get("name") or "")
                arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                try:
                    result = manager.call_tool(name, arguments)
                except Exception as exc:
                    result = _text({"ok": False, "error": str(exc)}, is_error=True)
                write_rpc(stdout, _result(req_id, result))
                continue
            if method in {"resources/list", "prompts/list"}:
                key = "resources" if method.startswith("resources") else "prompts"
                write_rpc(stdout, _result(req_id, {key: []}))
                continue
            if method in {"shutdown", "exit"}:
                if method == "shutdown":
                    write_rpc(stdout, _result(req_id, {}))
                break
            write_rpc(stdout, _error(req_id, -32601, f"method not found: {method}"))
    finally:
        manager.close()


def main() -> int:
    manager = Manager()
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    serve(manager, stdin, stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
