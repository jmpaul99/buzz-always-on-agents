"""Long-lived Goose worker. Parallel turns across agents; isolate by HOME.

The HTTP server stays up so a follow-up DM can reuse the container.
Never logs nsecs or prompts.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from activity import GooseActivityParser, redact
from agenthome import sync_agent_home
from memory import collect_sections, ensure_workspace, write_tom_md
from observer import ObserverPublisher, WAIT_READY_SECS, warm_observer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("goose-worker")

PORT = int(os.environ.get("PORT", "8080"))
GOOSE_TIMEOUT = int(os.environ.get("GOOSE_TIMEOUT_SECS", "1500"))
GOOSE_IDLE_TIMEOUT = int(os.environ.get("GOOSE_IDLE_TIMEOUT_SECS", "180"))
LIVENESS_SECS = 10
SEND_TIMEOUT_SECS = 45
FALLBACK_REPLY = (
    "I finished this turn but did not post a channel reply. "
    "Ask again if you still need an answer."
)
GOOSE_MAX_PARALLEL = max(1, int(os.environ.get("GOOSE_MAX_PARALLEL", "2")))
DEFAULT_RECIPE = "reply"
MAX_PROMPT = 20000
BASE_HOME = pathlib.Path("/home/goose")
LLM_ACTIVITY_URL = "http://127.0.0.1:4000/activity"
PASS_ENV = (
    "AGENT_NAME",
    "BUZZ_PRIVATE_KEY",
    "BUZZ_AUTH_TAG",
    "BUZZ_RELAY_URL",
    "BUZZ_CHANNEL_ID",
    "BUZZ_EVENT_ID",
    "REPLY_TO",
    "PROMPT",
    "BUZZ_OWNER_PUBKEY",
    "BUZZ_AUTHOR_PUBKEY",
    "BUZZ_MESSAGE",
    "BUZZ_IDENTITY",
    "BUZZ_SEND_CMD",
    "BUZZ_TEAM_INSTRUCTIONS",
    "BUZZ_WORKSPACE",
    "GOOSE_RECIPE",
)


class Turn:
    def __init__(self, agent: str, env: dict[str, str], prompt: str) -> None:
        self.agent = agent
        self.env = env
        self.prompt = prompt
        self.returncode = 1
        self.error = ""
        self.done = threading.Event()


class _Activity:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._at = time.monotonic()

    def bump(self) -> None:
        with self._lock:
            self._at = time.monotonic()

    def last(self) -> float:
        with self._lock:
            return self._at


_sched_lock = threading.Lock()
_agent_queues: dict[str, deque[Turn]] = {}
_agent_order: deque[str] = deque()
_running_agents: set[str] = set()
_pool: ThreadPoolExecutor | None = None


def _safe_agent(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (name or "agent").lower())
    cleaned = "-".join(p for p in cleaned.split("-") if p)
    return (cleaned[:32] or "agent")


def _rewrite_relay(env: dict[str, str]) -> None:
    relay = env.get("BUZZ_RELAY_URL", "")
    if relay.startswith("wss://"):
        env["BUZZ_RELAY_URL"] = "https://" + relay[6:]
    elif relay.startswith("ws://"):
        env["BUZZ_RELAY_URL"] = "http://" + relay[5:]


def _agent_home(agent: str) -> pathlib.Path:
    home = pathlib.Path("/tmp") / f"goose-{_safe_agent(agent)}"
    sync_agent_home(BASE_HOME, home)
    return home


def _llm_in_flight() -> bool:
    try:
        with urllib.request.urlopen(LLM_ACTIVITY_URL, timeout=1) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return int(data.get("in_flight") or 0) > 0
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError):
        return False


def _drain_output(
    source: Any,
    parser: GooseActivityParser,
) -> None:
    try:
        while True:
            if isinstance(source, int):
                try:
                    chunk = os.read(source, 4096)
                except OSError:
                    break
            else:
                chunk = source.read(4096)
            if not chunk:
                break
            parser.feed(chunk.decode("utf-8", errors="replace"))
        parser.close()
    except OSError:
        parser.close()
    finally:
        if isinstance(source, int):
            try:
                os.close(source)
            except OSError:
                pass


def build_send_argv(channel: str, content: str, reply_to: str = "") -> list[str]:
    cmd = ["buzz", "messages", "send", "--channel", channel, "--content", content]
    if reply_to:
        cmd.extend(["--reply-to", reply_to])
    return cmd


def _channel_send(env: dict[str, str], content: str) -> tuple[int, str]:
    channel = (env.get("BUZZ_CHANNEL_ID") or "").strip()
    if not channel:
        return 1, "missing channel"
    cmd = build_send_argv(channel, content[:8000], (env.get("REPLY_TO") or "").strip())
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            cwd=env.get("HOME") or None,
            capture_output=True,
            timeout=SEND_TIMEOUT_SECS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, type(exc).__name__
    out = (proc.stdout or b"").decode("utf-8", errors="replace")
    err = (proc.stderr or b"").decode("utf-8", errors="replace")
    return int(proc.returncode), redact((out + "\n" + err).strip())[:500]


def _fallback_send(env: dict[str, str], parser: GooseActivityParser) -> bool:
    text = (parser.last_reply or "").strip() or FALLBACK_REPLY
    log.info("fallback send agent=%s chars=%s", env.get("AGENT_NAME") or "-", len(text))
    code, output = _channel_send(env, text)
    channel = (env.get("BUZZ_CHANNEL_ID") or "").strip()
    command = f"buzz messages send --channel {channel} --content '...'"
    if (env.get("REPLY_TO") or "").strip():
        command += f" --reply-to {env['REPLY_TO'].strip()}"
    parser.record_external_send(command, output, ok=code == 0)
    if code == 0:
        return True
    log.error("fallback send failed code=%s out=%s", code, output[:160])
    return False


def _strip_controls(text: str) -> str:
    """YAML forbids C0/C1/DEL in every scalar, including `|` blocks after Jinja render."""
    return "".join(
        " " if (ord(ch) < 32 or ord(ch) == 127 or 0x80 <= ord(ch) <= 0x9F) else ch
        for ch in text
    )


def one_line(text: str, limit: int = 8000) -> str:
    """Goose Jinja-renders --params into the recipe YAML, then parses it again."""
    return " ".join(_strip_controls(text or "").split()).replace('"', "'")[:limit]


def recipe_params(env: dict[str, str], prompt: str = "") -> dict[str, str]:
    channel = (env.get("BUZZ_CHANNEL_ID") or "").strip()
    reply_to = (env.get("REPLY_TO") or "").strip()
    send_cmd = (env.get("BUZZ_SEND_CMD") or "").strip()
    if not send_cmd and channel:
        send_cmd = f"buzz messages send --channel {channel} --content '...'"
        if reply_to:
            send_cmd += f" --reply-to {reply_to}"
    message = (env.get("BUZZ_MESSAGE") or prompt or "").strip()
    return {
        "identity": (env.get("BUZZ_IDENTITY") or "").strip() or "You are a Buzz cloud agent.",
        "message": message,
        "send_cmd": send_cmd or "buzz messages send --content '...'",
    }


def build_goose_cmd(
    prompt: str,
    recipe: str = "",
    *,
    recipe_root: pathlib.Path | None = None,
    params: dict[str, str] | None = None,
) -> list[str]:
    cmd = [
        "goose",
        "run",
        "--no-session",
        "--quiet",
        "--output-format",
        "stream-json",
    ]
    slug = (recipe or "").strip().lower() or DEFAULT_RECIPE
    root = recipe_root
    if root is None:
        root = pathlib.Path(os.environ.get("GOOSE_RECIPE_PATH", "/home/goose/recipes"))
    recipe_file = root / slug / "recipe.yaml"
    if recipe_file.is_file():
        cmd.extend(["--recipe", str(recipe_file)])
        values = dict(params or {})
        values.setdefault("message", prompt)
        values.setdefault("send_cmd", "buzz messages send --content '...'")
        for key in ("identity", "message", "send_cmd"):
            val = values.get(key)
            if val is None:
                continue
            cmd.extend(["--params", f"{key}={one_line(str(val))}"])
        return cmd
    cmd.extend(["-t", prompt])
    return cmd


def prepare_turn(env: dict[str, str], agent: str, home: pathlib.Path) -> pathlib.Path:
    """Write tom.md, point Top of Mind at it, mkdir the GCS workspace, return cwd."""
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["npm_config_cache"] = str(home / ".npm")
    guardrails_path = home / ".config" / "goose" / "guardrails.md"
    guardrails = ""
    if guardrails_path.is_file():
        try:
            guardrails = guardrails_path.read_text(encoding="utf-8")
        except OSError:
            guardrails = ""
    sections = collect_sections(env, agent)
    tom = write_tom_md(home, guardrails, sections)
    env["GOOSE_MOIM_MESSAGE_FILE"] = str(tom)
    cwd = ensure_workspace(agent, env)
    return cwd or home


def _spawn_goose(
    env: dict[str, str],
    home: pathlib.Path,
    cwd: pathlib.Path | None = None,
) -> tuple[subprocess.Popen[bytes], Any]:
    cmd = build_goose_cmd(
        env.get("PROMPT") or env.get("BUZZ_MESSAGE") or "",
        env.get("GOOSE_RECIPE", ""),
        recipe_root=pathlib.Path(env["GOOSE_RECIPE_PATH"]) if env.get("GOOSE_RECIPE_PATH") else None,
        params=recipe_params(env, env.get("PROMPT") or ""),
    )
    env = dict(env)
    env.setdefault("TERM", "dumb")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("CI", "1")
    env.setdefault("COLUMNS", "512")
    env.setdefault("LINES", "24")
    kw: dict[str, Any] = {
        "env": env,
        "cwd": str(cwd or home),
        "stdin": subprocess.DEVNULL,
        "start_new_session": True,
    }
    # A pipe is fully buffered. Goose then runs extra LLM turns after the
    # channel send, and Agent Activity dumps only when the process exits.
    if os.name == "posix":
        import pty

        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(cmd, stdout=slave, stderr=slave, **kw)
        except Exception:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
        return proc, master
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
    return proc, proc.stdout


def _kill_proc(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _run_goose(turn: Turn) -> None:
    env = os.environ.copy()
    env.update(turn.env)
    _rewrite_relay(env)
    prompt = turn.prompt or env.get("PROMPT") or "Reply that the Goose worker received an empty prompt."
    env["PROMPT"] = prompt[:MAX_PROMPT]
    # Socket was warmed at /run. Home sync overlaps leftover AUTH. Then wait
    # so activity is live before Goose can send — wait_ms should be ~0 when
    # the shared socket is already ready.
    observer = ObserverPublisher(env)
    observer.emit_turn_started()
    started = time.monotonic()
    activity = _Activity()
    env["GOOSE_DISABLE_SESSION_NAMING"] = "true"
    env["GOOSE_DISABLE_KEYRING"] = "1"
    env.setdefault("GOOSE_MAX_TURNS", "25")
    env.setdefault("GOOSE_CLI_SHOW_THINKING", "1")
    env.setdefault("GOOSE_THINKING_EFFORT", "low")
    home = _agent_home(turn.agent)
    env.setdefault("GOOSE_RECIPE_PATH", "/home/goose/recipes")
    if not (env.get("GOOSE_RECIPE") or "").strip():
        env["GOOSE_RECIPE"] = DEFAULT_RECIPE
    cwd = prepare_turn(env, turn.agent, home)
    wait_t = time.monotonic()
    ready = observer.wait_ready(WAIT_READY_SECS)
    log.info(
        "observer wait ready=%s wait_ms=%.0f agent=%s",
        ready,
        (time.monotonic() - wait_t) * 1000,
        turn.agent,
    )
    log.info("goose start agent=%s recipe=%s", turn.agent, env.get("GOOSE_RECIPE") or "-")
    replied = threading.Event()
    parser = GooseActivityParser(
        observer.emit,
        on_activity=activity.bump,
        on_reply=replied.set,
    )
    out: Any = None
    try:
        proc, out = _spawn_goose(env, home, cwd)
    except OSError as exc:
        turn.returncode = 1
        turn.error = type(exc).__name__
        observer.close(turn.error)
        log.exception("goose spawn failed agent=%s", turn.agent)
        return
    reader = threading.Thread(
        target=_drain_output,
        args=(out, parser),
        name=f"goose-out-{turn.agent}",
        daemon=True,
    )
    reader.start()
    last_status = started
    last_live = started
    try:
        while True:
            code = proc.poll()
            if code is not None:
                turn.returncode = int(code)
                break
            now = time.monotonic()
            llm = _llm_in_flight()
            busy = llm or parser.tools_open()
            if busy:
                activity.bump()
            if now - last_status >= 30:
                last_status = now
                log.info(
                    "goose wait agent=%s secs=%.0f replied=%s llm=%s idle=%.0f tool=%s cmd=%s json=%s bytes=%s",
                    turn.agent,
                    now - started,
                    replied.is_set(),
                    llm,
                    now - activity.last(),
                    parser.last_tool or "-",
                    redact(parser.last_command)[:80] or "-",
                    parser.json_events,
                    parser.stdout_bytes,
                )
            if now - last_live >= LIVENESS_SECS:
                last_live = now
                observer.emit("turn_liveness", {"alive": True})
            if now - started > GOOSE_TIMEOUT:
                _kill_proc(proc)
                turn.returncode = 124
                turn.error = "goose timed out"
                log.error("goose timeout agent=%s", turn.agent)
                break
            if not busy and now - activity.last() > GOOSE_IDLE_TIMEOUT:
                _kill_proc(proc)
                if replied.is_set() or parser.replied:
                    turn.returncode = 0
                    log.info("goose stop idle agent=%s", turn.agent)
                else:
                    turn.returncode = 124
                    turn.error = "idle timeout"
                    log.error("goose idle timeout agent=%s", turn.agent)
                break
            time.sleep(0.2)
    finally:
        if proc.poll() is None:
            _kill_proc(proc)
        if isinstance(out, int):
            try:
                os.close(out)
            except OSError:
                pass
        reader.join(timeout=2)
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        if not parser.replied:
            if _fallback_send(env, parser):
                if not turn.error:
                    turn.returncode = 0
            elif not turn.error:
                turn.returncode = 1
                turn.error = "no channel reply"
        observer.close(turn.error)
    log.info(
        "goose done agent=%s code=%s secs=%.1f replied=%s json=%s bytes=%s tool=%s err=%s",
        turn.agent,
        turn.returncode,
        time.monotonic() - started,
        parser.replied,
        parser.json_events,
        parser.stdout_bytes,
        parser.last_tool or "-",
        turn.error or "-",
    )


def _health_payload() -> dict[str, Any]:
    with _sched_lock:
        queued = {name: len(q) for name, q in _agent_queues.items() if q}
        running = sorted(_running_agents)
        return {
            "ok": True,
            "max_parallel": GOOSE_MAX_PARALLEL,
            "running": running,
            "running_count": len(running),
            "queued": queued,
            "queued_count": sum(queued.values()),
        }


def _pick_idle_agent_locked() -> str | None:
    n = len(_agent_order)
    if n == 0:
        return None
    for _ in range(n):
        agent = _agent_order.popleft()
        _agent_order.append(agent)
        if agent in _running_agents:
            continue
        q = _agent_queues.get(agent)
        if q:
            return agent
    return None


def _fill_slots_locked() -> None:
    if _pool is None:
        return
    while len(_running_agents) < GOOSE_MAX_PARALLEL:
        agent = _pick_idle_agent_locked()
        if not agent:
            return
        q = _agent_queues.get(agent)
        if not q:
            return
        turn = q.popleft()
        _running_agents.add(agent)
        _pool.submit(_run_and_release, turn)


def _run_and_release(turn: Turn) -> None:
    try:
        _run_goose(turn)
    finally:
        turn.done.set()
        with _sched_lock:
            _running_agents.discard(turn.agent)
            _fill_slots_locked()


def _submit(turn: Turn, wait_secs: int) -> bool:
    with _sched_lock:
        q = _agent_queues.setdefault(turn.agent, deque())
        q.append(turn)
        if turn.agent not in _agent_order:
            _agent_order.append(turn.agent)
        queued = sum(len(item) for item in _agent_queues.values())
        log.info("enqueue agent=%s queue=%s running=%s", turn.agent, queued, len(_running_agents))
        _fill_slots_locked()
    return turn.done.wait(timeout=wait_secs)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] in {"/health", "/healthz", "/"}:
            self._send(200, _health_payload())
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/run":
            self._send(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 512_000:
            self._send(413, {"ok": False, "error": "payload too large"})
            return
        try:
            req = json.loads(self.rfile.read(length) if length else b"{}")
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "error": "invalid json"})
            return
        if not isinstance(req, dict):
            self._send(400, {"ok": False, "error": "invalid json"})
            return
        agent = str(req.get("agent_name") or req.get("AGENT_NAME") or "agent")[:32]
        prompt = str(req.get("prompt") or "")[:MAX_PROMPT]
        raw_env = req.get("env") if isinstance(req.get("env"), dict) else {}
        env: dict[str, str] = {}
        for key in PASS_ENV:
            val = raw_env.get(key)
            if val is None:
                val = req.get(key)
            if val is not None:
                env[key] = str(val)
        env["AGENT_NAME"] = agent
        if prompt:
            env["PROMPT"] = prompt
        recipe = str(req.get("recipe") or env.get("GOOSE_RECIPE") or "")[:64]
        if recipe:
            env["GOOSE_RECIPE"] = recipe
        warm_observer(env)
        turn = Turn(agent, env, prompt or env.get("PROMPT", ""))
        if not _submit(turn, GOOSE_TIMEOUT + 30):
            self._send(504, {"ok": False, "error": "queue wait timed out", "agent": agent})
            return
        if turn.error in {"idle timeout", "goose timed out"}:
            code = 504
        elif turn.returncode == 0:
            code = 200
        else:
            code = 500
        self._send(
            code,
            {"ok": turn.returncode == 0, "agent": agent, "returncode": turn.returncode, "error": turn.error},
        )


def main() -> None:
    global _pool
    _pool = ThreadPoolExecutor(max_workers=GOOSE_MAX_PARALLEL, thread_name_prefix="goose")
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("goose worker listening on %s max_parallel=%s", PORT, GOOSE_MAX_PARALLEL)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
