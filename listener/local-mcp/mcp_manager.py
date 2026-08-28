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
FRAME_NEWLINE = "newline"
FRAME_CONTENT_LENGTH = "content-length"
# buzz-agent aborts MCP init at 30s. Reply to initialize without waiting for
# buzz-dev-mcp (e2-micro spawn + extras can exceed that and leave Desktop Activity hung).
BOOT_WAIT_SECS = 20.0
MANAGER_TOOLS = (
    {
        "name": "mcp_list",
        "description": (
            "List always-on Buzz tools plus shipped and registered extras, "
            "whether extras are enabled, and any missing env keys. Always-on is "
            "not in the extra cap. At most "
            f"{mcp_catalog.MAX_ENABLED} extras can be enabled."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "mcp_enable",
        "description": (
            "Enable a catalog extra for this agent and spawn it in the background "
            "so Buzz always-on tools keep working. Returns starting:true; extra "
            "tool names are on the next mention or via mcp_tools. Cap is "
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
    {
        "name": "mcp_tools",
        "description": (
            "Page extra MCP tool names. Always-on Buzz tools are not paged. "
            "Pass cursor from the previous next_cursor. Prefixed extra names "
            "remain callable even when not on this page."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "cursor": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
)


def _rpc_bytes(msg: dict[str, Any], framing: str = FRAME_NEWLINE) -> bytes:
    # This sprig's rmcp stdio is NDJSON (newline JSON). Content-Length makes
    # buzz-dev-mcp return JSON-RPC -32700. Official extras (mcp-remote, Python
    # MCP SDK) still use Content-Length.
    body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    if framing == FRAME_CONTENT_LENGTH:
        return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
    return body + b"\n"


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


def write_rpc(stream: IO[bytes], msg: dict[str, Any], framing: str = FRAME_NEWLINE) -> None:
    stream.write(_rpc_bytes(msg, framing))
    stream.flush()


class StdioMcpClient:
    def __init__(self, proc: subprocess.Popen[bytes], timeout: float = 45.0, framing: str = FRAME_NEWLINE) -> None:
        self.proc = proc
        self.timeout = timeout
        self.framing = framing
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

    @property
    def alive(self) -> bool:
        if self.proc.poll() is not None:
            return False
        with self._lock:
            return not self._dead

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
            write_rpc(stdin, msg, self.framing)
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
            write_rpc(stdin, msg, self.framing)
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
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()
    except Exception:
        pass


def spawn_mcp(spec: dict[str, Any], env: dict[str, str]) -> StdioMcpClient:
    command = str(spec.get("command") or "")
    args = spec.get("args") or []
    if not isinstance(args, list):
        args = []
    argv = [command, *mcp_catalog.expand_args([str(item) for item in args], env)]
    resolved = shutil.which(command, path=env.get("PATH"))
    if resolved:
        argv[0] = resolved
    elif command in {"python", "python3"}:
        argv[0] = sys.executable
    elif command == "github-mcp-server" and Path("/usr/local/bin/github-mcp-server").is_file():
        argv[0] = "/usr/local/bin/github-mcp-server"
    framing = str(spec.get("framing") or FRAME_NEWLINE)
    if framing not in {FRAME_NEWLINE, FRAME_CONTENT_LENGTH}:
        framing = FRAME_NEWLINE
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if proc.stderr is not None:
        threading.Thread(target=_drain, args=(proc.stderr,), daemon=True).start()
    client = StdioMcpClient(proc, framing=framing)
    try:
        client.initialize()
    except Exception as exc:
        client.close()
        raise RuntimeError(f"MCP child failed to start: {exc}") from None
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
        runtime_path: Path | None = None,
        spawn: Any = spawn_mcp,
    ) -> None:
        raw = environ if environ is not None else dict(os.environ)
        self.environ = mcp_catalog.fill_missing_from_runtime(raw, runtime_path)
        self.catalog_path = catalog_path or mcp_catalog.default_catalog_path()
        self.overlay_path = overlay_path or mcp_catalog.default_overlay_path()
        self.workspace = workspace or Path(
            self.environ.get("BUZZ_WORKSPACE") or mcp_catalog.default_workspace()
        )
        self.agent_name = (agent_name or self.environ.get("AGENT_NAME") or "").strip()
        self.enabled_file = enabled_file or (
            mcp_catalog.enabled_path(self.agent_name, self.workspace) if self.agent_name else self.workspace / "mcp-enabled.json"
        )
        self._spawn = spawn
        self.catalog = mcp_catalog.load_catalog(self.catalog_path)
        self.dev: StdioMcpClient | None = None
        self.dev_tools: list[dict[str, Any]] = []
        self.extras: dict[str, StdioMcpClient] = {}
        self.extra_tools: dict[str, list[dict[str, Any]]] = {}
        self._starting: set[str] = set()
        self.last_error: dict[str, str] = {}
        self._extra_window_cursor = "0"
        self._published = False
        self._lock = threading.Lock()
        self.on_tools_changed: Any = None

    def overlay(self) -> dict[str, Any]:
        return mcp_catalog.load_overlay(self.overlay_path)

    def enabled_slugs(self) -> list[str]:
        return mcp_catalog.load_enabled(self.enabled_file)

    def _dev_argv(self) -> tuple[dict[str, Any], dict[str, str]]:
        spec = mcp_catalog.always_on_spec(self.catalog)
        env = mcp_catalog.child_env(list(spec.get("env_keys") or []), self.environ, trusted=True)
        command = self.environ.get("BUZZ_DEV_MCP_COMMAND") or spec.get("command") or "buzz-dev-mcp"
        args = spec.get("args") or []
        if self.environ.get("BUZZ_DEV_MCP_ARGS"):
            args = json.loads(self.environ["BUZZ_DEV_MCP_ARGS"])
        return {"command": command, "args": args}, env

    def _start_dev(self) -> None:
        row, env = self._dev_argv()
        self.dev = self._spawn(row, env)
        try:
            self.dev_tools = [dict(item) for item in self.dev.list_tools()]
        except Exception:
            if not self.dev_tools:
                raise

    def _ensure_dev(self) -> None:
        if self.dev is not None and self.dev.alive:
            return
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None
        try:
            self._start_dev()
        except Exception as exc:
            print(f"buzz-dev-mcp failed to start: {exc}", file=sys.stderr)

    def start(self) -> None:
        self._start_dev()
        for slug in self.enabled_slugs():
            self._start_extra_bg(slug, persist=False)

    def close(self) -> None:
        for client in list(self.extras.values()):
            client.close()
        self.extras.clear()
        self.extra_tools.clear()
        self.last_error.clear()
        if self.dev:
            self.dev.close()
            self.dev = None

    def _all_extra_tools(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for slug in self.extras:
            out.extend(self.extra_tools.get(slug) or [])
        return out

    def _summaries(self, tools: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [{"name": str(t.get("name") or ""), "description": str(t.get("description") or "")} for t in tools]

    def _notify_tools_changed(self) -> None:
        cb = self.on_tools_changed
        if cb:
            try:
                cb()
            except Exception:
                pass

    def _start_extra_bg(self, slug: str, persist: bool) -> None:
        with self._lock:
            if slug in self.extras or slug in self._starting:
                return
            self._starting.add(slug)
        threading.Thread(
            target=self._spawn_extra_bg,
            args=(slug, persist),
            daemon=True,
            name=f"mcp-extra-{slug}",
        ).start()

    def _spawn_extra_bg(self, slug: str, persist: bool) -> None:
        try:
            self._spawn_extra(slug, persist=persist)
            with self._lock:
                self.last_error.pop(slug, None)
        except Exception as exc:
            print(f"extra {slug} failed to start: {exc}", file=sys.stderr)
            with self._lock:
                self.last_error[slug] = str(exc)
                self._starting.discard(slug)
            mcp_catalog.save_enabled(self.enabled_file, mcp_catalog.disable_slug(self.enabled_slugs(), slug))
        finally:
            with self._lock:
                self._starting.discard(slug)
        self._notify_tools_changed()

    def _spawn_extra(self, slug: str, persist: bool) -> list[dict[str, Any]]:
        item = mcp_catalog.find_extra(slug, self.catalog, self.overlay())
        if item is None:
            raise ValueError(f"unknown extra {slug}")
        if slug in mcp_catalog.BROWSER_SLUGS:
            raise ValueError(f"{slug} is not allowed")
        missing = mcp_catalog.missing_env_keys(item, self.environ)
        if missing:
            raise ValueError(f"missing env: {', '.join(missing)}")
        with self._lock:
            if slug in self.extras:
                return self.extra_tools.get(slug) or []
        env = mcp_catalog.extra_child_env(item, self.environ)
        row = dict(item)
        row["framing"] = FRAME_CONTENT_LENGTH
        try:
            client = self._spawn(row, env)
        except Exception as exc:
            raise RuntimeError(f"extra {slug} failed to start: {exc}") from None
        try:
            tools = []
            for tool in client.list_tools():
                named = dict(tool)
                named["name"] = mcp_catalog.extra_tool_name(slug, str(tool["name"]))
                tools.append(named)
        except Exception as exc:
            client.close()
            raise RuntimeError(f"extra {slug} failed to start: {exc}") from None
        with self._lock:
            if slug in self.extras:
                client.close()
                return self.extra_tools.get(slug) or []
            self.extras[slug] = client
            self.extra_tools[slug] = tools
            if persist:
                enabled = mcp_catalog.enable_slug(self.enabled_slugs(), slug)
                mcp_catalog.save_enabled(self.enabled_file, enabled)
        return tools

    def _occupied_slugs(self) -> set[str]:
        return set(self.extras) | set(self._starting) | set(self.enabled_slugs())

    def list_payload(self) -> dict[str, Any]:
        self._ensure_dev()
        enabled = self.enabled_slugs()
        with self._lock:
            running = set(self.extras)
            starting = set(self._starting)
            errors = dict(self.last_error)
            counts = {slug: len(tools) for slug, tools in self.extra_tools.items()}
        extras = [
            mcp_catalog.extra_status(
                item,
                enabled,
                self.environ,
                running=str(item.get("slug") or "") in running,
                starting=str(item.get("slug") or "") in starting,
                tool_count=counts.get(str(item.get("slug") or ""), 0),
                last_error=errors.get(str(item.get("slug") or "")),
            )
            for item in mcp_catalog.merge_extras(self.catalog, self.overlay())
        ]
        spec = mcp_catalog.always_on_spec(self.catalog)
        return {
            "max_enabled": mcp_catalog.MAX_ENABLED,
            "always_on": [
                {
                    "slug": str(spec.get("slug") or "buzz-dev-mcp"),
                    "name": str(spec.get("display_name") or spec.get("name") or "buzz-dev-mcp"),
                    "alive": bool(self.dev and self.dev.alive),
                    "tools": [str(item.get("name") or "") for item in self.dev_tools],
                }
            ],
            "enabled": enabled,
            "extras": extras,
            "extra_page_size": mcp_catalog.EXTRA_PAGE_SIZE,
        }

    def enable(self, slug: str) -> dict[str, Any]:
        slug = (slug or "").strip().lower()
        if slug in mcp_catalog.always_on_slugs(self.catalog):
            raise ValueError(f"{slug} is always-on")
        item = mcp_catalog.find_extra(slug, self.catalog, self.overlay())
        if item is None:
            raise ValueError(f"unknown extra {slug}")
        missing = mcp_catalog.missing_env_keys(item, self.environ)
        if missing:
            raise ValueError(f"missing env: {', '.join(missing)}")
        note = (
            "Always-on Buzz tools stay available. Extra tools beyond this page: "
            "call mcp_tools with next_cursor. Prefixed names still work. "
            "Tools are guaranteed on the next mention if spawn succeeds. "
            "Tokens are not in shell env; if mcp_tools is empty, the extra failed."
        )
        with self._lock:
            if slug in self.extras:
                page, nxt = mcp_catalog.page_tools(self.extra_tools.get(slug) or [], "0")
                return {
                    "ok": True,
                    "slug": slug,
                    "starting": False,
                    "status": "running",
                    "tools": self._summaries(page),
                    "next_cursor": nxt,
                    "note": note,
                }
            occupied = self._occupied_slugs()
            if slug not in occupied and len(occupied) >= mcp_catalog.MAX_ENABLED:
                raise ValueError(f"at most {mcp_catalog.MAX_ENABLED} extras enabled per agent")
            already = slug in self._starting
            self.last_error.pop(slug, None)
        if already:
            return {
                "ok": True,
                "slug": slug,
                "starting": True,
                "status": "starting",
                "tools": [],
                "next_cursor": None,
                "note": note,
            }
        self._start_extra_bg(slug, persist=True)
        return {
            "ok": True,
            "slug": slug,
            "starting": True,
            "status": "starting",
            "tools": [],
            "next_cursor": None,
            "note": note,
        }

    def disable(self, slug: str) -> dict[str, Any]:
        slug = (slug or "").strip().lower()
        if slug in mcp_catalog.always_on_slugs(self.catalog):
            raise ValueError("cannot disable buzz-dev-mcp")
        with self._lock:
            client = self.extras.pop(slug, None)
            self.extra_tools.pop(slug, None)
            self._starting.discard(slug)
            self.last_error.pop(slug, None)
        if client:
            client.close()
        mcp_catalog.save_enabled(self.enabled_file, mcp_catalog.disable_slug(self.enabled_slugs(), slug))
        self._notify_tools_changed()
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

    def tools_page(self, slug: str = "", cursor: str = "") -> dict[str, Any]:
        slug = (slug or "").strip().lower()
        with self._lock:
            starting = slug in self._starting
            err = self.last_error.get(slug) if slug else None
            if slug:
                items = list(self.extra_tools.get(slug) or [])
            else:
                items = self._all_extra_tools()
            page, nxt = mcp_catalog.page_tools(items, cursor or "0")
            if not slug:
                self._extra_window_cursor = cursor or "0"
        if slug and err and slug not in self.extras:
            return {
                "ok": False,
                "slug": slug,
                "status": "failed",
                "cursor": cursor or "0",
                "tools": [],
                "next_cursor": None,
                "error": err,
            }
        if slug and starting and not items:
            return {
                "ok": True,
                "slug": slug,
                "status": "starting",
                "cursor": cursor or "0",
                "tools": [],
                "next_cursor": None,
            }
        if cursor not in (None, ""):
            self._notify_tools_changed()
        return {
            "ok": True,
            "slug": slug or None,
            "status": "running" if items else "off",
            "cursor": cursor or "0",
            "tools": self._summaries(page),
            "next_cursor": nxt,
        }

    def list_tools(self) -> list[dict[str, Any]]:
        self._ensure_dev()
        tools = [dict(item) for item in MANAGER_TOOLS]
        tools.extend(dict(item) for item in self.dev_tools)
        with self._lock:
            page, _nxt = mcp_catalog.page_tools(self._all_extra_tools(), self._extra_window_cursor)
        tools.extend(page)
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        args = arguments or {}
        if name == "shell":
            self._note_publish(args)
        if name == "_Stop":
            return self._stop_hook(args)
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
            if name == "mcp_tools":
                return _text(self.tools_page(str(args.get("slug") or ""), str(args.get("cursor") or "")))
        except Exception as exc:
            return _text({"ok": False, "error": str(exc)}, is_error=True)
        with self._lock:
            extra_slugs = list(self.extras)
        split = mcp_catalog.split_extra_tool(name, extra_slugs)
        if split:
            slug, tool = split
            client = self.extras.get(slug)
            if client is None:
                return _text({"ok": False, "error": f"extra {slug} is not running"}, is_error=True)
            try:
                return client.call_tool(tool, args)
            except Exception as exc:
                return _text({"ok": False, "error": str(exc)}, is_error=True)
        self._ensure_dev()
        if self.dev and self.dev.alive:
            try:
                return self.dev.call_tool(name, args)
            except Exception as exc:
                return _text({"ok": False, "error": str(exc)}, is_error=True)
        return _text({"ok": False, "error": f"unknown tool {name}"}, is_error=True)

    def _note_publish(self, args: dict[str, Any]) -> None:
        cmd = str(args.get("command") or args.get("text") or "")
        if "messages send" in cmd or "reactions add" in cmd:
            self._published = True

    def _stop_hook(self, args: dict[str, Any]) -> dict[str, Any]:
        child_text = ""
        self._ensure_dev()
        if self.dev and self.dev.alive:
            try:
                child_text = _result_text(self.dev.call_tool("_Stop", args))
            except Exception:
                child_text = ""
        if self._published:
            self._published = False
            return _text(child_text)
        nag = (
            "ACP Activity is not a channel or thread post. Phone users never see it. "
            "Call the registered tool run-mcp__shell (bare shell is unknown and fails) with: "
            "buzz messages send --channel <uuid from <context>> --content '...' "
            "using the reply destination from <context>. "
            "For multiline content: printf 'line\\n\\nline\\n' | buzz messages send "
            "--channel <uuid> --content -. Do not end this turn with only assistant text."
        )
        body = nag if not child_text.strip() else f"{nag}\n{child_text}"
        return _text(body)


def _result_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    return "".join(parts)


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


def _boot(manager: Manager, ready: threading.Event) -> None:
    try:
        manager.start()
    except Exception as exc:
        print(f"buzz-dev-mcp failed to start: {exc}", file=sys.stderr)
    finally:
        ready.set()


def serve(manager: Manager, stdin: IO[bytes], stdout: IO[bytes]) -> None:
    ready = threading.Event()
    write_lock = threading.Lock()

    def send(msg: dict[str, Any]) -> None:
        with write_lock:
            write_rpc(stdout, msg)

    manager.on_tools_changed = lambda: send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    threading.Thread(target=_boot, args=(manager, ready), daemon=True, name="mcp-boot").start()
    try:
        while True:
            msg = read_rpc(stdin)
            if msg is None:
                break
            method = str(msg.get("method") or "")
            req_id = msg.get("id")
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            if method == "initialize":
                version = str(params.get("protocolVersion") or PROTOCOL)
                send(
                    _result(
                        req_id,
                        {
                            "protocolVersion": version,
                            "capabilities": {"tools": {"listChanged": True}},
                            "serverInfo": {"name": "buzz-mcp-manager", "version": "1.0"},
                        },
                    )
                )
                continue
            if req_id is None:
                continue
            if method == "ping":
                send(_result(req_id, {}))
                continue
            if method in {"tools/list", "tools/call"}:
                ready.wait(timeout=BOOT_WAIT_SECS)
            if method == "tools/list":
                try:
                    listed = manager.list_tools()
                except Exception as exc:
                    send(_error(req_id, -32000, str(exc)))
                    continue
                send(_result(req_id, {"tools": listed}))
                continue
            if method == "tools/call":
                name = str(params.get("name") or "")
                arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                try:
                    result = manager.call_tool(name, arguments)
                except Exception as exc:
                    result = _text({"ok": False, "error": str(exc)}, is_error=True)
                send(_result(req_id, result))
                continue
            if method in {"resources/list", "prompts/list"}:
                key = "resources" if method.startswith("resources") else "prompts"
                send(_result(req_id, {key: []}))
                continue
            if method in {"shutdown", "exit"}:
                if method == "shutdown":
                    send(_result(req_id, {}))
                break
            send(_error(req_id, -32601, f"method not found: {method}"))
    finally:
        manager.on_tools_changed = None
        manager.close()


def main() -> int:
    manager = Manager()
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    serve(manager, stdin, stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
