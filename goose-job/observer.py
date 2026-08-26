"""Publish Buzz Desktop Agent Activity (kind 24200) over the relay WebSocket.

Kind 24200 is ephemeral — HTTP publish is rejected. Desktop decrypts NIP-44
telemetry tagged agent=<pubkey> frame=telemetry p=<owner>.

The WSS + NIP-42 AUTH is per agent, not per turn. Goose still waits until
AUTH so activity is on the wire before the channel reply — the wait is kept
cheap by starting the socket at `/run` and reusing it across DMs.
"""
from __future__ import annotations

import json
import logging
import queue
import socket
import ssl
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from websockets.sync.client import connect

from activity import redact
from nip44 import conversation_key
from nip44 import encrypt as nip44_encrypt
from nostrutil import nip42_auth_event, nsec_to_secret, pubkey_hex, sign_event

log = logging.getLogger("goose-observer")

KIND_OBSERVER = 24200
FLUSH_SECS = 0.15
MAX_PLAINTEXT = 60_000
AUTH_DEADLINE_SECS = 20
CONNECT_OPEN_SECS = 8
WAIT_READY_SECS = 8
# Cloud Run stops CPU between requests; a "ready" socket that sat idle is
# usually half-open. Reconnect on the next /run instead of discovering it
# after Goose has already replied.
IDLE_RECONNECT_SECS = 25

_pool_lock = threading.Lock()
_sockets: dict[str, "ObserverSocket"] = {}
_ssl_ctx = ssl.create_default_context()


def observer_should_stop(stop: threading.Event, out: queue.Queue, pending: list) -> bool:
    """True only when finish() ran *and* the terminal frame has been flushed."""
    return stop.is_set() and out.empty() and not pending


def _rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _wss_url(relay: str) -> str:
    url = (relay or "").strip().rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[8:]
    if url.startswith("http://"):
        return "ws://" + url[7:]
    return url


def _tcp4(relay: str, timeout: float) -> socket.socket | None:
    """IPv4-first TCP. Happy-Eyeballs IPv6 stalls can add seconds on Cloud Run."""
    parsed = urlparse(relay)
    host = parsed.hostname
    if not host:
        return None
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None
    if not infos:
        return None
    sock = socket.create_connection((infos[0][4][0], port), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


def _connect(relay: str, **kwargs: Any) -> Any:
    try:
        return connect(relay, compression=None, **kwargs)
    except TypeError:
        return connect(relay, **kwargs)


def _open_ws(relay: str) -> Any:
    kwargs: dict[str, Any] = {
        "open_timeout": CONNECT_OPEN_SECS,
        "ping_interval": 20,
        "ping_timeout": 20,
        "max_size": 2**22,
    }
    if relay.startswith("wss"):
        kwargs["ssl"] = _ssl_ctx
    sock = None
    try:
        sock = _tcp4(relay, CONNECT_OPEN_SECS)
    except OSError:
        sock = None
    if sock is not None:
        try:
            return _connect(relay, sock=sock, **kwargs)
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
    return _connect(relay, **kwargs)


def parse_auth_tags(raw: str) -> list:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], str):
        return [parsed]
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], list):
        return [t for t in parsed if isinstance(t, list)]
    return []


def owner_from_auth_tags(auth_tags: list) -> str:
    for t in auth_tags:
        if isinstance(t, list) and len(t) >= 2 and t[0] == "auth":
            return str(t[1]).lower()
    return ""


def _redact_json(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_json(v) for v in value]
    return value


@dataclass(frozen=True)
class SocketCreds:
    secret: bytes
    agent_pub: str
    owner: str
    relay: str
    auth_tags: list
    conv: bytes

    @property
    def key(self) -> str:
        return f"{self.agent_pub}:{self.owner}:{self.relay}"


class _FlushWait:
    def __init__(self) -> None:
        self.done = threading.Event()


def socket_creds(env: dict[str, str]) -> SocketCreds | None:
    nsec = env.get("BUZZ_PRIVATE_KEY") or ""
    try:
        secret = nsec_to_secret(nsec)
    except ValueError:
        return None
    auth_tags = parse_auth_tags(env.get("BUZZ_AUTH_TAG") or "")
    owner = (env.get("BUZZ_OWNER_PUBKEY") or owner_from_auth_tags(auth_tags)).lower()
    if len(owner) != 64:
        return None
    relay = _wss_url(env.get("BUZZ_RELAY_URL") or "")
    if not relay.startswith("ws"):
        return None
    return SocketCreds(
        secret=secret,
        agent_pub=pubkey_hex(secret),
        owner=owner,
        relay=relay,
        auth_tags=auth_tags,
        conv=conversation_key(secret, owner),
    )


def shared_socket(creds: SocketCreds, *, refresh_stale: bool = False) -> "ObserverSocket":
    with _pool_lock:
        sock = _sockets.get(creds.key)
        if sock is not None and refresh_stale and sock.stale():
            sock.halt()
            sock = None
        if sock is None or sock.dead:
            sock = ObserverSocket(creds)
            _sockets[creds.key] = sock
        return sock


def warm_observer(env: dict[str, str]) -> None:
    """Start TLS + AUTH as soon as /run arrives, before the agent queue."""
    creds = socket_creds(env)
    if creds is None:
        return
    shared_socket(creds, refresh_stale=True)


def reset_observer_sockets() -> None:
    """Drop the process pool. Tests only."""
    with _pool_lock:
        socks = list(_sockets.values())
        _sockets.clear()
    for sock in socks:
        sock.halt()


class ObserverSocket:
    """One authenticated WSS, reused across turns for the same agent."""

    def __init__(self, creds: SocketCreds, *, start: bool = True) -> None:
        self.creds = creds
        self.dead = False
        self._halt = threading.Event()
        self._ready = threading.Event()
        self._out: queue.Queue[Any] = queue.Queue()
        self._pending: list[dict[str, Any]] = []
        self._started = time.monotonic()
        self._last_ok = 0.0
        self._thread: threading.Thread | None = None
        if start:
            self._thread = threading.Thread(target=self._run, name="buzz-observer", daemon=True)
            self._thread.start()

    def stale(self) -> bool:
        if self.dead:
            return True
        if not self._ready.is_set() or self._last_ok <= 0:
            return False
        return time.monotonic() - self._last_ok > IDLE_RECONNECT_SECS

    def wait_ready(self, timeout: float = WAIT_READY_SECS) -> bool:
        return self._ready.wait(timeout)

    def publish(self, item: Any) -> None:
        self._out.put(item)

    def halt(self) -> None:
        self._halt.set()
        self._out.put(None)
        self.dead = True

    def _run(self) -> None:
        backoff = 0.5
        try:
            while not self._halt.is_set():
                t_open = time.monotonic()
                try:
                    with _open_ws(self.creds.relay) as ws:
                        tls_ms = (time.monotonic() - t_open) * 1000
                        t_auth = time.monotonic()
                        if not self._authenticate(ws):
                            log.warning(
                                "observer auth failed tls_ms=%.0f auth_ms=%.0f",
                                tls_ms,
                                (time.monotonic() - t_auth) * 1000,
                            )
                            self._ready.clear()
                            time.sleep(backoff)
                            backoff = min(backoff * 2, 8)
                            continue
                        auth_ms = (time.monotonic() - t_auth) * 1000
                        ready_ms = (time.monotonic() - self._started) * 1000
                        self._last_ok = time.monotonic()
                        self._ready.set()
                        backoff = 0.5
                        log.info(
                            "observer ready tls_ms=%.0f auth_ms=%.0f ready_ms=%.0f",
                            tls_ms,
                            auth_ms,
                            ready_ms,
                        )
                        self._serve(ws)
                except Exception:
                    log.exception("observer socket failed")
                self._ready.clear()
                if self._halt.is_set():
                    return
                time.sleep(backoff)
                backoff = min(backoff * 2, 8)
        finally:
            self.dead = True
            self._ready.clear()

    def _authenticate(self, ws: Any) -> bool:
        deadline = time.monotonic() + AUTH_DEADLINE_SECS
        auth_id = ""
        t0 = time.monotonic()
        while time.monotonic() < deadline:
            try:
                raw = ws.recv(timeout=min(2.0, max(0.1, deadline - time.monotonic())))
            except TimeoutError:
                log.info(
                    "observer waiting for AUTH elapsed_ms=%.0f",
                    (time.monotonic() - t0) * 1000,
                )
                continue
            except Exception:
                log.warning(
                    "observer AUTH recv failed elapsed_ms=%.0f",
                    (time.monotonic() - t0) * 1000,
                )
                return False
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, list) or not msg:
                continue
            if msg[0] == "AUTH" and len(msg) >= 2:
                log.info(
                    "observer AUTH challenge elapsed_ms=%.0f",
                    (time.monotonic() - t0) * 1000,
                )
                ev = nip42_auth_event(
                    self.creds.secret,
                    self.creds.relay,
                    str(msg[1]),
                    extra_tags=self.creds.auth_tags,
                )
                auth_id = str(ev.get("id") or "")
                ws.send(json.dumps(["AUTH", ev]))
                continue
            if msg[0] == "OK" and len(msg) >= 3 and str(msg[1]) == auth_id:
                ok = msg[2] is True
                log.info(
                    "observer AUTH %s elapsed_ms=%.0f",
                    "ok" if ok else "rejected",
                    (time.monotonic() - t0) * 1000,
                )
                return ok
            log.info("observer pre-auth %s elapsed_ms=%.0f", msg[0], (time.monotonic() - t0) * 1000)
        return False

    def _serve(self, ws: Any) -> None:
        first_flush = True
        ready_at = time.monotonic()

        def note_flush() -> None:
            nonlocal first_flush
            if not first_flush:
                return
            first_flush = False
            log.info(
                "observer first_flush lag_ms=%.0f",
                (time.monotonic() - ready_at) * 1000,
            )

        while True:
            try:
                item = self._out.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self._flush(ws)
                return
            if isinstance(item, _FlushWait):
                self._flush(ws)
                item.done.set()
                continue
            self._pending.append(item)
        if self._pending:
            self._flush(ws)
            note_flush()
        last_flush = time.monotonic()
        while True:
            timeout = max(0.05, FLUSH_SECS - (time.monotonic() - last_flush))
            try:
                item = self._out.get(timeout=timeout)
            except queue.Empty:
                self._drain(ws)
                if self._pending:
                    self._flush(ws)
                    last_flush = time.monotonic()
                if observer_should_stop(self._halt, self._out, self._pending):
                    return
                continue
            if item is None:
                self._flush(ws)
                return
            if isinstance(item, _FlushWait):
                self._flush(ws)
                item.done.set()
                last_flush = time.monotonic()
                continue
            if isinstance(item, dict) and item.get("kind") in {"turn_completed", "turn_error"}:
                log.info("observer send %s seq=%s", item["kind"], item.get("seq"))
            self._pending.append(item)
            self._flush(ws)
            note_flush()
            last_flush = time.monotonic()

    def _drain(self, ws: Any) -> None:
        try:
            ws.recv(timeout=0.01)
        except TimeoutError:
            return
        except Exception:
            return

    def _flush(self, ws: Any) -> None:
        if not self._pending:
            return
        events = self._pending
        self._pending = []
        if len(events) == 1:
            payload = events[0]
        else:
            payload = {
                "seq": events[-1]["seq"],
                "timestamp": _rfc3339(),
                "kind": "batch",
                "agentIndex": 0,
                "channelId": events[-1].get("channelId"),
                "sessionId": events[-1].get("sessionId"),
                "turnId": events[-1].get("turnId"),
                "startedAt": events[-1].get("startedAt"),
                "payload": {"events": events},
            }
        try:
            plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            if len(plaintext) > MAX_PLAINTEXT:
                for ev in events:
                    self._publish(ws, ev)
                return
            self._publish(ws, payload)
        except Exception:
            log.exception("observer flush failed")

    def _publish(self, ws: Any, payload: dict[str, Any]) -> None:
        plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        if len(plaintext) > MAX_PLAINTEXT:
            plaintext = plaintext[:MAX_PLAINTEXT]
        cipher = nip44_encrypt(plaintext, self.creds.conv)
        tags = [
            ["p", self.creds.owner],
            ["agent", self.creds.agent_pub],
            ["frame", "telemetry"],
        ]
        ev = sign_event(self.creds.secret, KIND_OBSERVER, tags, cipher)
        ws.send(json.dumps(["EVENT", ev]))
        self._last_ok = time.monotonic()


class ObserverPublisher:
    def __init__(self, env: dict[str, str]) -> None:
        self.enabled = False
        self._lock = threading.RLock()
        self._seq = 0
        self._sock: ObserverSocket | None = None
        creds = socket_creds(env)
        if creds is None:
            try:
                nsec_to_secret(env.get("BUZZ_PRIVATE_KEY") or "")
            except ValueError:
                log.warning("observer disabled: invalid agent key")
                return
            owner = (env.get("BUZZ_OWNER_PUBKEY") or owner_from_auth_tags(
                parse_auth_tags(env.get("BUZZ_AUTH_TAG") or "")
            )).lower()
            if len(owner) != 64:
                log.warning("observer disabled: no owner pubkey")
            else:
                log.warning("observer disabled: no relay url")
            return
        self._sock = shared_socket(creds)
        self.channel_id = env.get("BUZZ_CHANNEL_ID") or None
        self.session_id = str(uuid.uuid4())
        self.turn_id = str(uuid.uuid4())
        self.started_at = _rfc3339()
        self.event_id = env.get("BUZZ_EVENT_ID") or ""
        self.author = env.get("BUZZ_AUTHOR_PUBKEY") or ""
        self.message = (env.get("BUZZ_MESSAGE") or "").strip()
        if not self.message:
            prompt = env.get("PROMPT") or ""
            marker = "Message:\n"
            if marker in prompt:
                self.message = prompt.split(marker, 1)[1]
                self.message = self.message.split("\n\nThe message body", 1)[0][:8000].strip()
        self.enabled = True

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _envelope(self, kind: str, payload: Any) -> dict[str, Any]:
        return {
            "seq": self._next_seq(),
            "timestamp": _rfc3339(),
            "kind": kind,
            "agentIndex": 0,
            "channelId": self.channel_id,
            "sessionId": self.session_id,
            "turnId": self.turn_id,
            "startedAt": self.started_at,
            "payload": payload,
        }

    def emit(self, kind: str, payload: Any) -> None:
        with self._lock:
            if not self.enabled or self._sock is None:
                return
            payload = _redact_json(payload)
            if kind == "acp_read" and isinstance(payload, dict):
                params = payload.setdefault("params", {})
                if isinstance(params, dict):
                    params.setdefault("sessionId", self.session_id)
            item = self._envelope(kind, payload)
        self._sock.publish(item)

    def _prompt_text(self) -> str:
        lines = ["[Buzz event]"]
        if self.event_id:
            lines.append(f"Event ID: {self.event_id}")
        if self.channel_id:
            lines.append(f"Channel: {self.channel_id}")
        if self.author and len(self.author) == 64:
            lines.append(f"From: hex: {self.author.lower()}")
        body = self.message or "New turn"
        lines.append(f"Content: {body}")
        return "\n".join(lines)

    def emit_channel_clear(self) -> None:
        """End leftover Desktop working badges for this DM (null turnId)."""
        with self._lock:
            if not self.enabled or self._sock is None:
                return
            item = self._envelope("turn_completed", {"outcome": "success"})
            item["turnId"] = None
        self._sock.publish(item)

    def emit_turn_started(self) -> None:
        self.emit_channel_clear()
        ids = [self.event_id] if self.event_id else []
        self.emit("turn_started", {"triggeringEventIds": ids})
        self.emit(
            "acp_write",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/prompt",
                "params": {
                    "sessionId": self.session_id,
                    "prompt": [{"type": "text", "text": self._prompt_text()}],
                },
            },
        )

    def wait_ready(self, timeout: float = WAIT_READY_SECS) -> bool:
        if not self.enabled or self._sock is None:
            return False
        return self._sock.wait_ready(timeout)

    def emit_thought(self, text: str) -> None:
        cleaned = redact(text).strip()
        if not cleaned:
            return
        self.emit(
            "acp_read",
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": cleaned[:400]},
                    },
                },
            },
        )

    def emit_turn_error(self, error: str) -> None:
        self.emit("turn_error", {"outcome": "error", "error": error[:1000]})

    def emit_turn_completed(self) -> None:
        self.emit("turn_completed", {"outcome": "success"})

    def finish(self, error: str = "") -> _FlushWait | None:
        """Enqueue the terminal frame. Does not close the shared socket."""
        with self._lock:
            if not self.enabled or self._sock is None:
                return None
            if error:
                kind, payload = "turn_error", {"outcome": "error", "error": error[:1000]}
            else:
                kind, payload = "turn_completed", {"outcome": "success"}
            item = self._envelope(kind, payload)
            self.enabled = False
            sock = self._sock
        sock.publish(item)
        waiter = _FlushWait()
        sock.publish(waiter)
        return waiter

    def close(self, error: str = "") -> None:
        waiter = self.finish(error)
        if waiter is not None:
            waiter.done.wait(timeout=8)
