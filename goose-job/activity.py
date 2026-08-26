"""Turn Goose CLI stdout into compact Buzz observer events.

Desktop Agent Activity expects thought chunks and tool_call frames, not a
raw terminal dump. Never emit nsecs or env blobs.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
NSEC_RE = re.compile(r"nsec1[a-z0-9]{20,}", re.I)
SECRET_ASSIGN_RE = re.compile(
    r"\b(BUZZ_PRIVATE_KEY|BUZZ_AUTH_TAG|AWS_SECRET_ACCESS_KEY|OPENAI_API_KEY|"
    r"GEMINI_API_KEY|GROQ_API_KEY|NVIDIA_NIM_API_KEY|STRIPE_API_KEY|"
    r"GITHUB_PERSONAL_ACCESS_TOKEN|TAVILY_API_KEY)\s*=\s*\S+",
    re.I,
)
TOOL_START_RE = re.compile(r"^▸\s+(\S+)\s*(.*)$")
SEP_RE = re.compile(r"^─{8,}\s*$")
BANNER_RE = re.compile(
    r"goose is ready|new session|litellm goose|/tmp/goose-|"
    r"^\( O\)>|^__\)|^L L |^Copy code block|"
    r"loading recipe:|parameters used to load this recipe|"
    r"^description:\s|default buzz mention|"
    r"^channel:\s|^author:\s|^send_cmd:\s|^event_id:\s|^identity:\s",
    re.I,
)
RECIPE_DUMP_RE = re.compile(
    r"loading recipe:|parameters used to load this recipe|"
    r"default buzz mention: do the work",
    re.I,
)
SKIP_TOOLS: set[str] = set()
SKIP_COMMAND_RE = re.compile(
    r"\benv\b.*\bBUZZ\b|BUZZ_PRIVATE_KEY|printenv|--help\b|buzz help",
    re.I,
)
ACCEPTED_RE = re.compile(r'"accepted"\s*:\s*true', re.I)
MAX_THOUGHT = 400
MAX_REPLY = 8000
MAX_RESULT = 240
STREAM_TYPES = {"message", "notification", "error", "complete"}
JSON_BUF_MAX = 200_000

EmitFn = Callable[[str, dict[str, Any]], None]


@dataclass
class _OpenTool:
    tool_id: str
    name: str
    args: dict[str, str]
    skip: bool
    out: list[str] = field(default_factory=list)
    emitted: bool = False
    tui: bool = False


def redact(text: str) -> str:
    text = strip_controls(text)
    text = NSEC_RE.sub("nsec1[redacted]", text)
    text = SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    return text


def strip_controls(text: str) -> str:
    text = OSC_RE.sub("", text)
    text = CSI_RE.sub("", text)
    text = ANSI_RE.sub("", text)
    return text.replace("\x07", "").replace("\x00", "")


def _split_tui(text: str) -> str:
    text = text.replace("▸ ", "\n▸ ")
    text = re.sub(r"(?<!\n)command:", "\ncommand:", text)
    text = re.sub(r"\{[\"']accepted[\"']", lambda m: "\n" + m.group(0), text)
    return text


def _unwrap(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if "Ok" in value:
        return _unwrap(value["Ok"])
    if "ok" in value:
        return _unwrap(value["ok"])
    # Goose tool_result_serde: {"status":"success","value":{...}}
    if "value" in value and "name" not in value and value.get("status") is not None:
        inner = value.get("value")
        if inner is not None:
            return _unwrap(inner)
    return value


def _texts(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, list):
        for item in value:
            found.extend(_texts(item))
    elif isinstance(value, dict):
        for key in ("text", "output", "stdout", "message", "value", "error"):
            raw = value.get(key)
            if isinstance(raw, str):
                found.append(raw)
        for item in value.values():
            if not isinstance(item, str):
                found.extend(_texts(item))
    return found


def _as_args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        if text[:1] in "{[":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return parsed
        return {"command": text[:500]}
    return {}


def _tool_call(payload: dict[str, Any]) -> tuple[str, dict[str, str]]:
    call = _unwrap(
        payload.get("toolCall")
        or payload.get("tool_call")
        or payload.get("toolUse")
        or payload
    )
    if not isinstance(call, dict):
        return "", {}
    name = str(call.get("name") or call.get("toolName") or payload.get("name") or "")
    raw_args = call.get("arguments")
    if raw_args is None:
        raw_args = call.get("args") or call.get("input") or call.get("parameters")
    args = _as_args(raw_args)
    flat: dict[str, str] = {}
    for key, val in args.items():
        if isinstance(val, str):
            flat[str(key)] = val[:500]
        else:
            flat[str(key)] = json.dumps(val, ensure_ascii=False)[:500]
    for src, dest in (("cmd", "command"), ("script", "command"), ("bash", "command")):
        if dest not in flat and src in flat:
            flat[dest] = flat[src]
    if "command" not in flat:
        for key in ("command", "cmd"):
            val = call.get(key)
            if isinstance(val, str) and val.strip():
                flat["command"] = val[:500]
                break
    return name, flat


def _acp_kind(name: str, command: str = "") -> str:
    n = name.lower().replace("-", "_")
    if command or n.endswith("shell") or n.endswith("_bash"):
        return "execute"
    if "read_file" in n or n.endswith("_read") or "view_image" in n:
        return "read"
    if "str_replace" in n or "write" in n or "edit" in n:
        return "edit"
    return "other"


def _is_banner(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if BANNER_RE.search(stripped) or RECIPE_DUMP_RE.search(stripped):
        return True
    if set(stripped) <= set("─━│┌┐└┘╭╮╰╯▸●()|_/\\ "):
        return True
    return False


class GooseActivityParser:
    def __init__(
        self,
        emit: EmitFn,
        on_activity: Callable[[], None] | None = None,
        on_reply: Callable[[], None] | None = None,
    ) -> None:
        self._emit = emit
        self._on_activity = on_activity
        self._on_reply = on_reply
        self._buf = ""
        self._json = ""
        self._tool_n = 0
        self._open: dict[str, _OpenTool] = {}
        self._tui_id = ""
        self._prose: list[str] = []
        self.replied = False
        self._seen_tool = False
        self.last_tool = ""
        self.last_command = ""
        self.last_reply = ""
        self.json_events = 0
        self.stdout_bytes = 0

    def feed(self, chunk: str) -> None:
        self.stdout_bytes += len(chunk)
        chunk = _split_tui(strip_controls(chunk)).replace("\r\n", "\n")
        self._buf += chunk
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if "\r" in line:
                line = line.rsplit("\r", 1)[-1]
            self._line(line)
        if "\r" in self._buf:
            self._buf = self._buf.rsplit("\r", 1)[-1]
        if len(self._buf) > 8000:
            self._line(self._buf)
            self._buf = ""

    def close(self) -> None:
        if self._buf.strip():
            self._line(self._buf)
            self._buf = ""
        if self._json.strip():
            self._line(self._json)
            self._json = ""
        self._flush_prose()
        for tool_id in list(self._open):
            self._finish_tool(tool_id, True)

    def _line(self, line: str) -> None:
        raw = strip_controls(line).rstrip()
        stripped = raw.strip()
        if self._json:
            self._json += stripped
            if not self._consume_json(self._json):
                if len(self._json) > JSON_BUF_MAX:
                    self._json = ""
                return
            return
        if stripped.startswith("{") and not stripped.endswith("}"):
            self._json = stripped
            return
        if stripped.startswith("{") and stripped.endswith("}"):
            if self._consume_json(stripped):
                return
        start = TOOL_START_RE.match(raw)
        if start:
            self._flush_prose()
            self._finish_tui(True)
            self._begin_tool(start.group(1))
            rest = start.group(2).strip()
            if rest:
                self._tool_line(rest)
            return
        if SEP_RE.match(raw):
            tool = self._open.get(self._tui_id)
            if tool and (tool.args or tool.out):
                self._finish_tui(True)
            return
        if self._tui_id:
            self._tool_line(raw)
            return
        if _is_banner(raw):
            return
        self._prose.append(raw)
        self._flush_prose()

    def _consume_json(self, text: str) -> bool:
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return False
        self._json = ""
        if isinstance(obj, dict) and obj.get("type") in STREAM_TYPES:
            self._stream_event(obj)
            return True
        if isinstance(obj, dict):
            blob = json.dumps(obj, ensure_ascii=False)
            if self._tui_id and self._tui_id in self._open:
                self._open[self._tui_id].out.append(blob)
                self._finish_tui(True)
                return True
            if self._looks_like_send(self.last_command, blob) or self._looks_like_send(
                self.last_command, "\n".join(_texts(obj))
            ):
                self._mark_replied()
                return True
        return False

    def _stream_event(self, obj: dict[str, Any]) -> None:
        self.json_events += 1
        kind = obj.get("type")
        if kind == "complete":
            self._complete_event(obj)
            return
        if kind == "notification":
            blob = "\n".join(_texts(obj))
            if self._looks_like_send(self.last_command, blob):
                self._mark_replied()
            if self._on_activity:
                self._on_activity()
            return
        if kind == "error":
            err = redact(str(obj.get("error") or "")).strip()
            if err:
                self._thought(err)
            return
        msg = obj.get("message")
        if not isinstance(msg, dict):
            return
        role = str(msg.get("role") or "").lower()
        tools_only = role in {"user", "system"}
        content = msg.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    self._stream_content(item, tools_only=tools_only)
        elif isinstance(content, dict):
            self._stream_content(content, tools_only=tools_only)
        elif isinstance(content, str) and content.strip() and not tools_only:
            self._assistant_text(content, thinking=False)

    def _stream_content(self, item: dict[str, Any], tools_only: bool = False) -> None:
        kind = str(item.get("type") or item.get("kind") or "").replace("_", "").lower()
        if kind in {"toolrequest", "tooluse", "frontendtoolrequest"}:
            if tools_only:
                return
            self._flush_prose()
            name, args = _tool_call(item)
            tool_id = str(item.get("id") or "")
            self._begin_tool(name or "tool", tool_id=tool_id, args=args)
            return
        if kind in {"toolresponse", "toolresult"}:
            output = "\n".join(
                _texts(_unwrap(item.get("toolResult") or item.get("tool_result") or item))
            )
            rid = str(
                item.get("id") or item.get("toolUseId") or item.get("tool_use_id") or ""
            )
            tool = self._open.get(rid) if rid else None
            if tool is None and len(self._open) == 1:
                tool = next(iter(self._open.values()))
            if tool is None:
                if output.strip() and self._looks_like_send(self.last_command, output):
                    self._mark_replied()
                return
            if output.strip():
                tool.out.append(output)
            self._finish_tool(tool.tool_id, True)
            return
        if tools_only:
            return
        if kind in {"text", "thinking"}:
            text = item.get("text") or item.get("thinking") or ""
            if isinstance(text, str) and text.strip():
                self._assistant_text(text, thinking=kind == "thinking")
            return
        text = item.get("text")
        if isinstance(text, str) and text.strip() and kind not in {"image", "redactedthinking"}:
            self._assistant_text(text, thinking=False)

    def _complete_event(self, obj: dict[str, Any]) -> None:
        msg = obj.get("message")
        if isinstance(msg, dict):
            role = str(msg.get("role") or "").lower()
            content = msg.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        self._stream_content(item, tools_only=role in {"user", "system"})
            elif isinstance(content, str) and content.strip() and role not in {"user", "system"}:
                self._assistant_text(content, thinking=False)
            return
        if isinstance(msg, str) and msg.strip():
            self._assistant_text(msg, thinking=False)
            return
        blob = "\n".join(_texts(obj)).strip()
        if blob:
            self._assistant_text(blob, thinking=False)

    def _store_reply(self, text: str) -> None:
        cleaned = redact(text).strip()
        if not cleaned or _is_banner(cleaned):
            return
        self.last_reply = cleaned[:MAX_REPLY]

    def record_external_send(self, command: str, output: str, *, ok: bool) -> None:
        args = {"command": command[:500]}
        self._begin_tool("shell", args=args)
        tool = self._open.get(self._tui_id) or (next(iter(self._open.values())) if self._open else None)
        if tool is not None and output.strip():
            tool.out.append(output)
        if tool is not None:
            self._finish_tool(tool.tool_id, ok)
        if ok:
            self._mark_replied()

    def _assistant_text(self, text: str, thinking: bool) -> None:
        # Buzz already shows the channel reply. Goose prints that same
        # assistant text after the shell send, so emitting it as Thinking
        # puts a duplicate bubble after the tool row.
        if not thinking:
            self._store_reply(text)
        if not thinking and (self.replied or self._seen_tool):
            return
        self._thought(text)

    def _thought(self, text: str) -> None:
        cleaned = redact(text).strip()
        if not cleaned or _is_banner(cleaned):
            return
        self._prose.append(cleaned)
        self._flush_prose()

    def _begin_tool(
        self,
        name: str,
        tool_id: str = "",
        args: dict[str, str] | None = None,
    ) -> None:
        self._seen_tool = True
        tui = not bool(tool_id)
        if tui:
            self._finish_tui(True)
            self._tool_n += 1
            tool_id = f"goose-{self._tool_n}"
        elif tool_id not in self._open:
            self._tool_n += 1
        self.last_tool = name or tool_id
        command = (args or {}).get("command") or ""
        if command:
            self.last_command = command[:80]
        skip = name.lower() in SKIP_TOOLS or bool(SKIP_COMMAND_RE.search(command))
        tool = _OpenTool(
            tool_id=tool_id,
            name=name,
            args=dict(args or {}),
            skip=skip,
            tui=tui,
        )
        self._open[tool_id] = tool
        if tui:
            self._tui_id = tool_id
        # TUI prints `▸ shell` before `command:`. Wait for the command so we
        # can hide --help / env dumps instead of flashing a tool card.
        if not tool.skip and not (tui and not command):
            self._emit_tool(tool, "executing")

    def _tool_line(self, raw: str) -> None:
        tool = self._open.get(self._tui_id)
        if tool is None:
            return
        if ":" in raw and not tool.out:
            key, _, val = raw.partition(":")
            key = key.strip()
            val = val.strip()
            if key and re.fullmatch(r"[A-Za-z_][\w]*", key) and len(key) < 40:
                tool.args[key] = redact(val)[:500]
                if key == "command":
                    self.last_command = tool.args[key][:80]
                    if SKIP_COMMAND_RE.search(tool.args[key]):
                        tool.skip = True
                if tool.skip:
                    return
                if tool.emitted or key == "command":
                    self._emit_tool(tool, "executing")
                return
        if raw.strip():
            tool.out.append(raw)
            if self._send_accepted(tool):
                self._finish_tool(tool.tool_id, True)

    def _looks_like_send(self, command: str, output: str) -> bool:
        blob = f"{command}\n{output}"
        if not ACCEPTED_RE.search(blob):
            return False
        low = blob.lower()
        if "reaction" in low:
            return False
        if "messages send" in low:
            return True
        # Goose sometimes emits only the accepted JSON, with no command on that line.
        if command.strip():
            return False
        return "event_id" in low or '"id"' in low

    def _send_accepted(self, tool: _OpenTool) -> bool:
        command = tool.args.get("command") or ""
        output = "\n".join(tool.out)
        return self._looks_like_send(command, output)

    def _mark_replied(self) -> None:
        if self.replied:
            return
        self.replied = True
        if self._on_reply:
            self._on_reply()

    def _finish_tui(self, completed: bool) -> None:
        if self._tui_id:
            self._finish_tool(self._tui_id, completed)

    def _finish_tool(self, tool_id: str, completed: bool) -> None:
        tool = self._open.pop(tool_id, None)
        if tool is None:
            return
        if self._tui_id == tool_id:
            self._tui_id = ""
        if tool.skip:
            return
        self._emit_tool(tool, "completed" if completed else "failed")
        if self._send_accepted(tool):
            self._mark_replied()

    def _emit_tool(self, tool: _OpenTool, status: str) -> None:
        command = tool.args.get("command") or ""
        if SKIP_COMMAND_RE.search(command) or SKIP_COMMAND_RE.search(" ".join(tool.out)):
            result = "[redacted]"
        else:
            result = redact("\n".join(tool.out)).strip()
            if len(result) > MAX_RESULT:
                result = result[:MAX_RESULT].rstrip() + "…"
        title = tool.name
        # Prefer the namespaced tool name. Shell commands are the useful title;
        # a generic "search" card hides GitHub vs Extension Manager.
        if command:
            title = command if len(command) < 80 else command[:77] + "…"
        payload = {
            "toolCallId": tool.tool_id,
            "title": title,
            "toolName": tool.name,
            "kind": _acp_kind(tool.name, command),
            "status": status,
            "rawInput": dict(tool.args),
        }
        kind = "tool_call" if not tool.emitted else "tool_call_update"
        tool.emitted = True
        if status in {"completed", "failed"}:
            payload["rawOutput"] = result
        self._emit_update(kind, payload)

    def _flush_prose(self) -> None:
        text = redact("\n".join(self._prose)).strip()
        self._prose = []
        if not text:
            return
        if not self.last_reply:
            self._store_reply(text)
        if len(text) > MAX_THOUGHT:
            text = text[:MAX_THOUGHT].rstrip() + "…"
        self._emit_update(
            "agent_thought_chunk",
            {"content": {"type": "text", "text": text}},
        )

    def _emit_update(self, session_update: str, update: dict[str, Any]) -> None:
        self._emit(
            "acp_read",
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {"sessionUpdate": session_update, **update},
                },
            },
        )
        if self._on_activity:
            self._on_activity()
