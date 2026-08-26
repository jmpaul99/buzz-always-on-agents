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
from observer import ObserverPublisher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("goose-worker")

PORT = int(os.environ.get("PORT", "8080"))
GOOSE_TIMEOUT = int(os.environ.get("GOOSE_TIMEOUT_SECS", "1500"))
GOOSE_IDLE_TIMEOUT = int(os.environ.get("GOOSE_IDLE_TIMEOUT_SECS", "180"))
REPLY_GRACE_SECS = 20  # idle after last activity once the channel reply landed
LIVENESS_SECS = 10
GOOSE_MAX_PARALLEL = max(1, int(os.environ.get("GOOSE_MAX_PARALLEL", "2")))
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


def build_goose_cmd(
    prompt: str,
    recipe: str = "",
    *,
    recipe_root: pathlib.Path | None = None,
) -> list[str]:
    cmd = [
        "goose",
        "run",
        "--no-session",
        "--quiet",
        "--output-format",
        "stream-json",
    ]
    slug = (recipe or "").strip().lower()
    root = recipe_root
    if root is None:
        root = pathlib.Path(os.environ.get("GOOSE_RECIPE_PATH", "/home/goose/recipes"))
    recipe_file = root / slug / "recipe.yaml" if slug else None
    if recipe_file is not None and recipe_file.is_file():
        cmd.extend(["--recipe", str(recipe_file), "--params", f"message={prompt}"])
        return cmd
    cmd.extend(["-t", prompt])
    return cmd


def _spawn_goose(env: dict[str, str], home: pathlib.Path) -> tuple[subprocess.Popen[bytes], Any]:
    cmd = build_goose_cmd(
        env["PROMPT"],
        env.get("GOOSE_RECIPE", ""),
        recipe_root=pathlib.Path(env["GOOSE_RECIPE_PATH"]) if env.get("GOOSE_RECIPE_PATH") else None,
    )
    env = dict(env)
    env.setdefault("TERM", "dumb")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("CI", "1")
    env.setdefault("COLUMNS", "512")
    env.setdefault("LINES", "24")
    proc = subprocess.Popen(
        cmd,
        env=env,
        cwd=str(home),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
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
    env["GOOSE_DISABLE_SESSION_NAMING"] = "true"
    env["GOOSE_DISABLE_KEYRING"] = "1"
    home = _agent_home(turn.agent)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["npm_config_cache"] = str(home / ".npm")
    env.setdefault("GOOSE_RECIPE_PATH", "/home/goose/recipes")
    guardrails = home / ".config" / "goose" / "guardrails.md"
    if guardrails.is_file():
        env["GOOSE_MOIM_MESSAGE_FILE"] = str(guardrails)
    started = time.monotonic()
    activity = _Activity()
    observer = ObserverPublisher(env)
    observer.emit_turn_started()
    log.info("goose start agent=%s recipe=%s", turn.agent, env.get("GOOSE_RECIPE") or "-")
    replied = threading.Event()
    parser = GooseActivityParser(
        observer.emit,
        on_activity=activity.bump,
        on_reply=replied.set,
    )
    try:
        proc, out = _spawn_goose(env, home)
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
            if now - last_status >= 30:
                last_status = now
                log.info(
                    "goose wait agent=%s secs=%.0f replied=%s llm=%s idle=%.0f tool=%s cmd=%s json=%s bytes=%s",
                    turn.agent,
                    now - started,
                    replied.is_set(),
                    _llm_in_flight(),
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
            if replied.is_set() and now - activity.last() > REPLY_GRACE_SECS:
                _kill_proc(proc)
                turn.returncode = 0
                log.info("goose stop after reply idle agent=%s", turn.agent)
                break
            if not replied.is_set() and now - activity.last() > GOOSE_IDLE_TIMEOUT:
                _kill_proc(proc)
                turn.returncode = 124
                turn.error = "idle timeout"
                log.error("goose idle timeout agent=%s", turn.agent)
                break
            time.sleep(1)
    finally:
        if proc.poll() is None:
            _kill_proc(proc)
        reader.join(timeout=2)
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        observer.close(turn.error)
    if not turn.error:
        log.info(
            "goose done agent=%s code=%s secs=%.1f",
            turn.agent,
            turn.returncode,
            time.monotonic() - started,
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
