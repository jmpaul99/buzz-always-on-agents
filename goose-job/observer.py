"""Publish Buzz Desktop Agent Activity (kind 24200) over the relay WebSocket.

Kind 24200 is ephemeral — HTTP publish is rejected. Desktop decrypts NIP-44
telemetry tagged agent=<pubkey> frame=telemetry p=<owner>.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from websockets.sync.client import connect

from activity import redact
from nip44 import conversation_key
from nip44 import encrypt as nip44_encrypt
from nostrutil import nip42_auth_event, nsec_to_secret, pubkey_hex, sign_event

log = logging.getLogger("goose-observer")

KIND_OBSERVER = 24200
FLUSH_SECS = 0.15
MAX_PLAINTEXT = 60_000


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


class ObserverPublisher:
    def __init__(self, env: dict[str, str]) -> None:
        self.enabled = False
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._out: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._pending: list[dict[str, Any]] = []
        self._seq = 0
        self._lock = threading.RLock()
        nsec = env.get("BUZZ_PRIVATE_KEY") or ""
        try:
            self._secret = nsec_to_secret(nsec)
        except ValueError:
            log.warning("observer disabled: invalid agent key")
            return
        self._agent_pub = pubkey_hex(self._secret)
        auth_raw = env.get("BUZZ_AUTH_TAG") or ""
        auth_tags = parse_auth_tags(auth_raw)
        self._auth_tags = auth_tags
        self._owner = (env.get("BUZZ_OWNER_PUBKEY") or owner_from_auth_tags(auth_tags)).lower()
        if len(self._owner) != 64:
            log.warning("observer disabled: no owner pubkey")
            return
        self._relay = _wss_url(env.get("BUZZ_RELAY_URL") or "")
        if not self._relay.startswith("ws"):
            log.warning("observer disabled: no relay url")
            return
        self._conv = conversation_key(self._secret, self._owner)
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
        self._thread = threading.Thread(target=self._run, name="buzz-observer", daemon=True)
        self._thread.start()

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
            if not self.enabled:
                return
            payload = _redact_json(payload)
            if kind == "acp_read" and isinstance(payload, dict):
                params = payload.setdefault("params", {})
                if isinstance(params, dict):
                    params.setdefault("sessionId", self.session_id)
            item = self._envelope(kind, payload)
        self._out.put(item)

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
            if not self.enabled:
                return
            item = self._envelope("turn_completed", {"outcome": "success"})
            item["turnId"] = None
        self._out.put(item)

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

    def emit_turn_error(self, error: str) -> None:
        self.emit("turn_error", {"outcome": "error", "error": error[:1000]})

    def emit_turn_completed(self) -> None:
        self.emit("turn_completed", {"outcome": "success"})

    def finish(self, error: str = "") -> None:
        """Enqueue the terminal frame and stop further events. Does not join.

        Put the frame and the None sentinel *before* setting _stop. The WS
        thread must drain those items; exiting on _stop while they still sit
        in the queue drops turn_completed and leaves Desktop's working badge
        spinning until restart.
        """
        with self._lock:
            if not self.enabled:
                return
            if error:
                kind, payload = "turn_error", {"outcome": "error", "error": error[:1000]}
            else:
                kind, payload = "turn_completed", {"outcome": "success"}
            item = self._envelope(kind, payload)
            self.enabled = False
        self._out.put(item)
        self._out.put(None)
        self._stop.set()

    def close(self, error: str = "") -> None:
        self.finish(error)
        try:
            self._out.put_nowait(None)
        except Exception:
            pass
        thread = getattr(self, "_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=8)

    def _run(self) -> None:
        try:
            with connect(self._relay, open_timeout=15, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
                if not self._authenticate(ws):
                    log.warning("observer auth failed")
                    return
                self._ready.set()
                log.info("observer connected channel=%s", (self.channel_id or "")[:8])
                while True:
                    try:
                        item = self._out.get_nowait()
                    except queue.Empty:
                        break
                    if item is None:
                        self._flush(ws)
                        return
                    self._pending.append(item)
                self._flush(ws)
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
                        # finish() sets _stop after enqueueing the terminal
                        # frame. Exiting here while the queue still holds it
                        # is what leaves a ghost working badge on Desktop.
                        if observer_should_stop(self._stop, self._out, self._pending):
                            return
                        continue
                    if item is None:
                        self._flush(ws)
                        return
                    if item.get("kind") in {"turn_completed", "turn_error"}:
                        log.info("observer send %s seq=%s", item["kind"], item.get("seq"))
                    self._pending.append(item)
                    self._flush(ws)
                    last_flush = time.monotonic()
        except Exception:
            log.exception("observer socket failed")
        finally:
            self.enabled = False

    def _authenticate(self, ws: Any) -> bool:
        deadline = time.monotonic() + 20
        auth_id = ""
        while time.monotonic() < deadline:
            try:
                raw = ws.recv(timeout=max(0.1, deadline - time.monotonic()))
            except TimeoutError:
                return False
            except Exception:
                return False
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, list) or not msg:
                continue
            if msg[0] == "AUTH" and len(msg) >= 2:
                ev = nip42_auth_event(
                    self._secret,
                    self._relay,
                    str(msg[1]),
                    extra_tags=self._auth_tags,
                )
                auth_id = str(ev.get("id") or "")
                ws.send(json.dumps(["AUTH", ev]))
                continue
            if msg[0] == "OK" and len(msg) >= 3 and str(msg[1]) == auth_id:
                return msg[2] is True
        return False

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
                "channelId": self.channel_id,
                "sessionId": self.session_id,
                "turnId": self.turn_id,
                "startedAt": self.started_at,
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
        cipher = nip44_encrypt(plaintext, self._conv)
        tags = [
            ["p", self._owner],
            ["agent", self._agent_pub],
            ["frame", "telemetry"],
        ]
        ev = sign_event(self._secret, KIND_OBSERVER, tags, cipher)
        ws.send(json.dumps(["EVENT", ev]))
