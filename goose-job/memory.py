"""Fetch ACP-style standing context for a Goose turn via the Buzz CLI.

Never logs profile text, message bodies, nsecs, or auth tags. Omit a section
on fetch error rather than inventing empty state.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess
import sys
from typing import Any

log = logging.getLogger("goose-memory")

CORE_TIMEOUT_SECS = 8
FETCH_TIMEOUT_SECS = 12
TEAM_CAP = 8000
THREAD_CAP = 4000
THREAD_MAX_MESSAGES = 12
# clap: --format is on the root Cli, not the subcommand. After `get` it is exit 1.
_COMPACT = ("--format", "compact")
# Listener chat kinds plus CLI messages-get defaults (9 + stream v2/diff).
CONTEXT_KINDS = "9,40002,40007,40008,45001,45002,45003,46010"
CORE_NUDGE = (
    "[Agent Memory — core]\n"
    "No core memory is stored yet. After you learn durable identity, write it with:\n"
    'buzz mem set core "…"\n'
    "Keep core small (~10 KB). Durable detail goes to mem/<topic>."
)
AGENT_DIRS = (
    "RESEARCH",
    "PLANS",
    "GUIDES",
    "WORK_LOGS",
    "OUTBOX",
    "REPOS",
    ".scratch",
)
_ABSENT_MARKERS = (
    "not found",
    "no such",
    "does not exist",
    "missing",
    "no core",
    "unknown memory",
    "empty",
)


def _safe_agent(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (name or "agent").lower())
    cleaned = "-".join(p for p in cleaned.split("-") if p)
    return (cleaned[:32] or "agent")


def _safe_channel(channel: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (channel or "").lower())
    cleaned = "-".join(p for p in cleaned.split("-") if p)
    return cleaned[:64]


def _buzz_argv(args: list[str], env: dict[str, str]) -> list[str]:
    binary = (env.get("BUZZ_BIN") or os.environ.get("BUZZ_BIN") or "buzz").strip() or "buzz"
    if binary.endswith(".py"):
        return [sys.executable, binary, *args]
    return [binary, *args]


def run_buzz(
    args: list[str],
    env: dict[str, str],
    *,
    timeout: float = FETCH_TIMEOUT_SECS,
) -> subprocess.CompletedProcess[str]:
    """Run buzz. Never log stdout/stderr (may contain profiles or messages)."""
    try:
        return subprocess.run(
            _buzz_argv(args, env),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        proc = subprocess.CompletedProcess(_buzz_argv(args, env), 127, "", "not found")
        return proc
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(_buzz_argv(args, env), 124, "", "timeout")
    except OSError:
        return subprocess.CompletedProcess(_buzz_argv(args, env), 1, "", "os error")


def _blob(proc: subprocess.CompletedProcess[str]) -> str:
    return f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()


def _looks_absent(proc: subprocess.CompletedProcess[str]) -> bool:
    if proc.returncode == 0:
        return False
    low = _blob(proc).lower()
    return any(marker in low for marker in _ABSENT_MARKERS)


def _cli_error_category(proc: subprocess.CompletedProcess[str]) -> str:
    """Buzz prints {error, message} on stderr. Log only the category, never message bodies."""
    raw = (proc.stderr or "").strip()
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return ""
        try:
            obj = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return ""
    if isinstance(obj, dict):
        return str(obj.get("error") or "")[:40]
    return ""


def fetch_core(env: dict[str, str]) -> str:
    """ACP fail-closed core memory.

    success → [Agent Memory — core]\\n<profile>
    confirmed CLI absence → onboarding nudge
    missing owner / missing buzz / transport / decrypt → omit
    """
    owner = (env.get("BUZZ_OWNER_PUBKEY") or "").strip()
    if not owner:
        log.info("core omitted reason=missing-owner")
        return ""
    args = ["mem", "get", "core", "--owner", owner]
    proc = run_buzz(args, env, timeout=CORE_TIMEOUT_SECS)
    if proc.returncode == 127:
        log.info("core omitted reason=missing-cli")
        return ""
    if proc.returncode == 124:
        log.info("core omitted reason=timeout")
        return ""
    if proc.returncode == 0:
        profile = (proc.stdout or "").strip()
        if not profile:
            log.info("core omitted reason=empty")
            return ""
        return "[Agent Memory — core]\n" + profile
    if _looks_absent(proc):
        log.info("core absent; injecting onboarding nudge")
        return CORE_NUDGE
    log.info("core omitted reason=fetch-error code=%s", proc.returncode)
    return ""


def _json_meta(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


def fetch_canvas(env: dict[str, str]) -> str:
    channel = (env.get("BUZZ_CHANNEL_ID") or "").strip()
    if not channel:
        return ""
    proc = run_buzz([*_COMPACT, "canvas", "get", "--channel", channel], env)
    if proc.returncode != 0:
        log.info(
            "canvas omitted reason=fetch-error code=%s error=%s",
            proc.returncode,
            _cli_error_category(proc) or "-",
        )
        return ""
    if not (proc.stdout or "").strip() and not (proc.stderr or "").strip():
        return ""
    meta = _json_meta(proc.stdout or "")
    lines = ["[Channel Canvas]"]
    if meta:
        event_id = meta.get("event_id") or meta.get("id") or meta.get("eventId") or ""
        mtime = meta.get("mtime") or meta.get("updated_at") or meta.get("updatedAt") or ""
        if event_id:
            lines.append(f"event_id: {event_id}")
        if mtime:
            lines.append(f"mtime: {mtime}")
    else:
        lines.append("A channel canvas is present.")
    lines.append(f"Read or update with: buzz canvas get --channel {channel}")
    lines.append("Write updates with buzz canvas set before the channel send.")
    return "\n".join(lines)


def fetch_huddle(env: dict[str, str]) -> str:
    channel = (env.get("BUZZ_CHANNEL_ID") or "").strip()
    if not channel:
        return ""
    proc = run_buzz(["huddle", "get", "--channel", channel], env)
    if proc.returncode != 0:
        log.info(
            "huddle omitted reason=fetch-error code=%s error=%s",
            proc.returncode,
            _cli_error_category(proc) or "-",
        )
        return ""
    text = (proc.stdout or "").strip()
    if not text:
        return ""
    return "[Huddle Instructions]\n" + text[:TEAM_CAP]


def _parse_message_list(text: str) -> list[dict[str, Any]]:
    """Parse buzz messages get/thread JSON. Omit on junk rather than inventing lines."""
    raw = (text or "").strip()
    if not raw:
        return []
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("[")
        end = raw.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            obj = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return []
    if isinstance(obj, list):
        return [item for item in obj if isinstance(item, dict)]
    if isinstance(obj, dict):
        for key in ("events", "messages", "data"):
            val = obj.get(key)
            if isinstance(val, list):
                return [item for item in val if isinstance(item, dict)]
        return [obj]
    return []


def _format_context_messages(events: list[dict[str, Any]], skip_id: str) -> str:
    rows: list[str] = []
    for ev in events:
        eid = str(ev.get("id") or "").strip()
        if skip_id and eid == skip_id:
            continue
        content = str(ev.get("content") or "").strip()
        if not content:
            continue
        pubkey = str(ev.get("pubkey") or "").strip()
        actor = (pubkey[:8] if pubkey else eid[:8]) or "msg"
        created = ev.get("created_at")
        ts = str(created) if created not in (None, "") else ""
        rows.append(f"{actor} ({ts}): {content}" if ts else f"{actor}: {content}")
    if len(rows) > THREAD_MAX_MESSAGES:
        rows = rows[-THREAD_MAX_MESSAGES:]
    return "\n".join(rows)[:THREAD_CAP]


def fetch_thread(env: dict[str, str]) -> str:
    """Short-term chat for this channel: recent get, or thread when the event has an e tag."""
    channel = (env.get("BUZZ_CHANNEL_ID") or "").strip()
    if not channel:
        log.info("thread omitted reason=missing-channel")
        return ""
    reply_to = (env.get("REPLY_TO") or "").strip()
    skip_id = (env.get("BUZZ_EVENT_ID") or "").strip()
    limit = str(THREAD_MAX_MESSAGES)
    if reply_to:
        kind = "thread"
        label = "Thread Context"
        proc = run_buzz(
            [
                *_COMPACT,
                "messages",
                "thread",
                "--channel",
                channel,
                "--event",
                reply_to,
                "--limit",
                limit,
            ],
            env,
        )
    else:
        kind = "get"
        label = "Conversation Context"
        proc = run_buzz(
            [
                *_COMPACT,
                "messages",
                "get",
                "--channel",
                channel,
                "--limit",
                limit,
                "--kinds",
                CONTEXT_KINDS,
            ],
            env,
        )
    if proc.returncode != 0:
        log.info(
            "%s omitted reason=fetch-error code=%s error=%s",
            kind,
            proc.returncode,
            _cli_error_category(proc) or "-",
        )
        return ""
    body = _format_context_messages(_parse_message_list(proc.stdout or ""), skip_id)
    if not body:
        log.info("%s omitted reason=empty", kind)
        return ""
    log.info("%s injected messages=%s chars=%s", kind, body.count("\n") + 1, len(body))
    return f"[{label}]\n" + body


def team_section(env: dict[str, str]) -> str:
    text = (env.get("BUZZ_TEAM_INSTRUCTIONS") or "").strip()[:TEAM_CAP]
    if not text:
        return ""
    return "[Team Instructions]\n" + text


def _mkdir_tree(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for name in AGENT_DIRS:
        (path / name).mkdir(exist_ok=True)


def workspace_root(env: dict[str, str] | None = None) -> pathlib.Path:
    env = env or {}
    raw = (env.get("BUZZ_WORKSPACE") or os.environ.get("BUZZ_WORKSPACE") or "/mnt/buzz").strip()
    return pathlib.Path(raw)


def ensure_workspace(slug: str, env: dict[str, str] | None = None) -> pathlib.Path | None:
    """Create shared/, agents/<slug>/, and channels/<id>/ trees. None if the mount is absent."""
    env = env or {}
    root = workspace_root(env)
    if not root.is_dir():
        return None
    agent_dir = root / "agents" / _safe_agent(slug)
    channel = _safe_channel(env.get("BUZZ_CHANNEL_ID") or "")
    try:
        (root / "shared").mkdir(parents=True, exist_ok=True)
        _mkdir_tree(agent_dir)
        if channel:
            _mkdir_tree(root / "channels" / channel)
    except OSError:
        log.info("workspace omitted reason=mkdir-failed")
        return None
    return agent_dir


def workspace_section(slug: str, env: dict[str, str] | None = None) -> str:
    env = env or {}
    root = workspace_root(env)
    if not root.is_dir():
        return ""
    agent = _safe_agent(slug)
    channel = _safe_channel(env.get("BUZZ_CHANNEL_ID") or "")
    lines = [
        "[Workspace]",
        f"This agent: {root / 'agents' / agent}",
    ]
    if channel:
        lines.append(f"This channel: {root / 'channels' / channel}")
    lines.append(f"Cross-channel only: {root / 'shared'}")
    lines.append("Working directory is the agent prefix. Goose HOME stays under /tmp.")
    lines.append(
        "Put channel work in channels/<id>/ so shared/ stays small. "
        "Agent-only notes go under agents/<slug>/. "
        "Use shared/ only for files every agent in every channel should see. "
        "Do not store nsecs, .env files, or keys in this bucket. "
        "Identity lives in buzz mem; this workspace is notes and artifacts."
    )
    return "\n".join(lines)


def collect_sections(env: dict[str, str], slug: str = "") -> list[str]:
    agent = slug or (env.get("AGENT_NAME") or "agent")
    parts = [
        fetch_core(env),
        team_section(env),
        fetch_huddle(env),
        fetch_canvas(env),
        fetch_thread(env),
        workspace_section(agent, env),
    ]
    return [p for p in parts if p.strip()]


def write_tom_md(
    home: pathlib.Path,
    guardrails: str,
    sections: list[str],
) -> pathlib.Path:
    dest = home / ".config" / "goose" / "tom.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    chunks = [guardrails.rstrip()]
    for section in sections:
        text = (section or "").strip()
        if text:
            chunks.append(text)
    dest.write_text("\n\n".join(chunks).rstrip() + "\n", encoding="utf-8")
    return dest
