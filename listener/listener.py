#!/usr/bin/env python3
"""Always-on Buzz WSS listener. One connection per /etc/buzz/*.env agent.

Buzz delivers chat only on channel-scoped (#h) subscriptions. After NIP-42 AUTH,
discover membership, then REQ each channel. Mentions use #p; DMs do not.
On matching events, add 👀/💬 reactions and a typing heartbeat, POST to the
Goose Cloud Run service, then retract the reactions. Never prints nsecs.

Hot-reloads /etc/buzz/*.env without restarting the process. Token-auth control
API on BUZZ_CONTROL_HOST:BUZZ_CONTROL_PORT (default 0.0.0.0:8743; firewall IAP
range plus Cloud Run Direct VPC). Sidecar uses `_sync.token`; Goose apply uses
a Google ID token from the goose-job SA (POST/PUT only — never GET /agents).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import websockets

import agentutil as au
import taskmcp
from nostrutil import (
    generate_nsec,
    nip42_auth_event,
    nip98_authorization,
    nsec_to_secret,
    pubkey_hex,
    relay_http_base,
    sign_event,
)
from seen import SeenStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("buzz-listener")

AGENTS_DIR = pathlib.Path(os.environ.get("BUZZ_AGENTS_DIR", "/etc/buzz"))
STATE_DIR = pathlib.Path(os.environ.get("BUZZ_STATE_DIR", "/var/lib/buzz-listener"))
RELAY_URL = os.environ.get("BUZZ_RELAY_URL", au.DEFAULT_RELAY)
GOOSE_WORKER_URL = os.environ.get("GOOSE_WORKER_URL", "").rstrip("/")
GOOSE_WORKER_TIMEOUT = int(os.environ.get("GOOSE_WORKER_TIMEOUT", "1620"))
CONTROL_HOST = os.environ.get("BUZZ_CONTROL_HOST", "0.0.0.0")
CONTROL_PORT = int(os.environ.get("BUZZ_CONTROL_PORT", "8743"))
GOOSE_WORKER_SA = (os.environ.get("GOOSE_WORKER_SA") or "").strip().lower()
WORKER_APPLY_AUDIENCE = (
    os.environ.get("WORKER_APPLY_AUDIENCE") or os.environ.get("LISTENER_CONTROL_URL") or ""
).strip()
# Tests assign a callable(token) -> bool. Production uses Google ID tokens.
_worker_token_checker = None
CHAT_KINDS = au.CHAT_KINDS
MEMBER_KIND = au.MEMBER_KIND
META_KIND = au.META_KIND
MEMBER_ADDED_KIND = au.MEMBER_ADDED_KIND
MEMBER_REMOVED_KIND = au.MEMBER_REMOVED_KIND
PRESENCE_KIND = au.PRESENCE_KIND
TYPING_KIND = au.TYPING_KIND
REACTION_KIND = au.REACTION_KIND
DELETE_KIND = au.DELETE_KIND

# Re-export for tests that import listener.
should_handle = au.should_handle
channel_from_event = au.channel_from_event


def load_agents() -> list[dict[str, Any]]:
    agents = []
    if not AGENTS_DIR.is_dir():
        log.warning("agents dir missing: %s", AGENTS_DIR)
        return agents
    for path in sorted(AGENTS_DIR.glob("*.env")):
        if path.name.startswith("_"):
            continue
        env = au.load_env_file(path)
        nsec = env.get("BUZZ_PRIVATE_KEY", "")
        if not nsec:
            log.warning("skip %s: no BUZZ_PRIVATE_KEY", path.name)
            continue
        try:
            secret = nsec_to_secret(nsec)
        except ValueError:
            log.warning("skip %s: invalid key", path.name)
            continue
        derived = pubkey_hex(secret)
        declared = (env.get("BUZZ_PUBKEY") or "").lower()
        if declared and declared != derived:
            log.warning("pubkey mismatch for %s; using key-derived pubkey", path.stem)
        agent = au.parse_loaded_agent(path, env, derived)
        agent["secret"] = secret
        agents.append(agent)
        log.info("loaded agent %s pubkey=%s", agent["name"], agent["pubkey"][:12])
    return agents


def execute_job(agent: dict[str, Any], evt: dict[str, Any]) -> None:
    channel = au.channel_from_event(evt)
    event_id = str(evt.get("id") or "")
    content = str(evt.get("content") or "")
    author = str(evt.get("pubkey") or "")
    reply_to = au.tag_value(evt.get("tags") or [], "e") or ""
    send_cmd = (
        f"buzz messages send --channel {channel} "
        f"--content '{au.SEND_CONTENT_PLACEHOLDER}'"
    )
    if reply_to:
        send_cmd += f" --reply-to {reply_to}"
    identity = au.with_turn_hint(
        au.load_instructions(AGENTS_DIR, agent["name"])
        or f"You are the Buzz agent {agent['display']} ({agent['name']})."
    )
    prompt = au.build_goose_prompt(
        identity=identity,
        channel=channel,
        author=author,
        event_id=event_id,
        content=content,
        send_cmd=send_cmd,
    )
    recipe = taskmcp.match_task_recipe(content, taskmcp.load_catalog(taskmcp.default_catalog_path())) or ""
    if not GOOSE_WORKER_URL:
        raise RuntimeError("GOOSE_WORKER_URL is not set")
    payload = json.dumps(
        {
            "agent_name": agent["name"],
            "prompt": prompt[:20000],
            "recipe": recipe,
            "env": {
                "AGENT_NAME": agent["name"],
                "BUZZ_PRIVATE_KEY": agent["nsec"],
                "BUZZ_AUTH_TAG": agent.get("auth_tag_raw") or "",
                "BUZZ_RELAY_URL": agent["relay"],
                "BUZZ_CHANNEL_ID": channel,
                "BUZZ_EVENT_ID": event_id,
                "REPLY_TO": reply_to or "",
                "PROMPT": prompt[:20000],
                "BUZZ_OWNER_PUBKEY": agent.get("owner") or "",
                "BUZZ_AUTHOR_PUBKEY": author,
                "BUZZ_MESSAGE": content[:8000],
                "BUZZ_IDENTITY": identity[:8000],
                "BUZZ_SEND_CMD": send_cmd,
                "BUZZ_TEAM_INSTRUCTIONS": au.load_team_file(AGENTS_DIR, agent["name"]),
                "GOOSE_RECIPE": recipe,
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    log.info("enqueue worker agent=%s channel=%s event=%s recipe=%s", agent["name"], channel[:8], event_id[:12], recipe or "-")
    with _agent_run_lock(agent["name"]):
        _post_worker(payload)


_agent_run_locks: dict[str, threading.Lock] = {}
_agent_run_locks_guard = threading.Lock()


def _agent_run_lock(name: str) -> threading.Lock:
    with _agent_run_locks_guard:
        lock = _agent_run_locks.get(name)
        if lock is None:
            lock = threading.Lock()
            _agent_run_locks[name] = lock
        return lock


_id_token = {"value": "", "exp": 0.0}
_id_token_lock = threading.Lock()


def _worker_id_token() -> str:
    now = time.time()
    with _id_token_lock:
        if _id_token["value"] and _id_token["exp"] > now + 60:
            return _id_token["value"]
    url = (
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        "service-accounts/default/identity?audience="
        + urllib.parse.quote(GOOSE_WORKER_URL, safe="")
    )
    req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        token = resp.read().decode("ascii")
    with _id_token_lock:
        _id_token["value"] = token
        _id_token["exp"] = now + 3000
    return token


def _post_worker(payload: bytes) -> None:
    last_err = ""
    for attempt in range(1, 4):
        req = urllib.request.Request(
            f"{GOOSE_WORKER_URL}/run",
            data=payload,
            headers={
                "Authorization": f"Bearer {_worker_id_token()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=GOOSE_WORKER_TIMEOUT) as resp:
                status = resp.status
                body = resp.read().decode("utf-8", errors="replace")
            log.info("worker ok status=%s body=%s", status, body[:200])
            return
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code}"
            log.warning("worker HTTP %s attempt=%s", exc.code, attempt)
            if exc.code in {429, 502, 503} and attempt < 3:
                time.sleep(2 * attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            last_err = type(exc).__name__
            log.warning("worker %s attempt=%s", last_err, attempt)
            if attempt < 3:
                time.sleep(2 * attempt)
                continue
            raise
    raise RuntimeError(f"worker failed: {last_err}")


def _relay_headers(agent: dict[str, Any], method: str, url: str, body: bytes) -> dict[str, str]:
    headers = {
        "Authorization": nip98_authorization(agent["secret"], method, url, body),
        "Content-Type": "application/json",
    }
    raw_tag = agent.get("auth_tag_raw") or ""
    if raw_tag:
        headers["x-auth-tag"] = raw_tag
    return headers


class WsPublisher:
    """Publish signed events on the agent's live AUTH socket (survives reconnect)."""

    def __init__(self) -> None:
        self._ws: Any = None
        self._lock = asyncio.Lock()

    def attach(self, ws: Any) -> None:
        self._ws = ws

    def detach(self, ws: Any) -> None:
        if self._ws is ws:
            self._ws = None

    async def publish(self, agent: dict[str, Any], kind: int, content: str, tags: list) -> str:
        ev = sign_event(agent["secret"], kind, tags, content)
        async with self._lock:
            ws = self._ws
            if ws is None:
                return ""
            try:
                await ws.send(json.dumps(["EVENT", ev]))
            except Exception:
                log.warning("ws publish failed kind=%s for %s", kind, agent["name"])
                return ""
        return str(ev.get("id") or "")


class TurnTracker:
    """In-flight 👀/💬 + typing for a channel. Retract once (own reply or job end)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._by_channel: dict[str, dict[str, Any]] = {}

    async def register(self, channel: str, reaction_ids: list[str], stop: asyncio.Event) -> None:
        async with self._lock:
            prev = self._by_channel.get(channel)
            if prev:
                prev["stop"].set()
            self._by_channel[channel] = {
                "ids": [eid for eid in reaction_ids if eid],
                "stop": stop,
            }

    async def take(self, channel: str) -> dict[str, Any] | None:
        async with self._lock:
            return self._by_channel.pop(channel, None)


async def _retract_reactions(
    agent: dict[str, Any], pub: WsPublisher, event_ids: list[str], channel: str = ""
) -> None:
    for eid in event_ids:
        tags = au.deletion_tags(eid, channel=channel)
        if not tags:
            continue
        deleted = await pub.publish(agent, DELETE_KIND, "", tags)
        if deleted:
            log.info("retract reaction %s for %s", eid[:12], agent["name"])
        else:
            log.warning("retract reaction failed %s for %s", eid[:12], agent["name"])


def _http_query(agent: dict[str, Any], filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    base = relay_http_base(agent["relay"])
    url = f"{base}/query"
    body = json.dumps(filters, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=_relay_headers(agent, "POST", url, body), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        log.warning("channel query HTTP %s for %s", exc.code, agent["name"])
        return []
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.warning("channel query failed for %s: %s", agent["name"], type(exc).__name__)
        return []
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]
    return []


def discover_channels(agent: dict[str, Any]) -> dict[str, str]:
    members = _http_query(agent, [{"kinds": [MEMBER_KIND], "#p": [agent["pubkey"]]}])
    ids: list[str] = []
    for ev in members:
        for d_val in au.all_tag_values(ev.get("tags") or [], "d"):
            if d_val and d_val not in ids:
                ids.append(d_val)
    types: dict[str, str] = {}
    if ids:
        metas = _http_query(agent, [{"kinds": [META_KIND], "#d": ids}])
        for ev in metas:
            d_val = au.tag_value(ev.get("tags") or [], "d")
            if not d_val:
                continue
            ch_type = au.channel_type_from_tags(ev.get("tags") or [])
            if ch_type != "archived":
                types[d_val] = ch_type
        for channel_id in ids:
            types.setdefault(channel_id, "stream")
    if not types:
        log.warning("channel discovery empty for %s", agent["name"])
        return {}
    return types


async def subscribe_channel(
    ws: Any,
    agent: dict[str, Any],
    channel_id: str,
    ch_type: str,
    since: int,
    subscribed: set[str],
) -> None:
    if not channel_id or channel_id in subscribed:
        return
    sub_id = au.channel_sub_id(channel_id)
    filt: dict[str, Any] = {"kinds": list(CHAT_KINDS), "#h": [channel_id], "since": since}
    if ch_type != "dm":
        filt["#p"] = [agent["pubkey"]]
    await ws.send(json.dumps(["REQ", sub_id, filt]))
    subscribed.add(channel_id)
    log.info(
        "subscribed %s channel=%s type=%s mention_filter=%s",
        agent["name"],
        channel_id[:8],
        ch_type,
        ch_type != "dm",
    )


async def unsubscribe_channel(ws: Any, channel_id: str, subscribed: set[str]) -> None:
    if not channel_id:
        return
    sub_id = au.channel_sub_id(channel_id)
    try:
        await ws.send(json.dumps(["CLOSE", sub_id]))
    except Exception:
        log.exception("close sub failed channel=%s", channel_id[:8])
    subscribed.discard(channel_id)
    log.info("unsubscribed channel=%s", channel_id[:8])


async def after_auth(ws: Any, agent: dict[str, Any], since: int) -> dict[str, str]:
    channels = await asyncio.to_thread(discover_channels, agent)
    subscribed: set[str] = set()
    for channel_id, ch_type in channels.items():
        await subscribe_channel(ws, agent, channel_id, ch_type, since, subscribed)
    await ws.send(
        json.dumps(
            [
                "REQ",
                f"n-{agent['name']}"[:20],
                {"kinds": [MEMBER_ADDED_KIND, MEMBER_REMOVED_KIND], "#p": [agent["pubkey"]], "since": since},
            ]
        )
    )
    log.info("subscribed membership notifications for %s channels=%s", agent["name"], len(channels))
    await ws.send(
        json.dumps(
            [
                "REQ",
                f"mb-{agent['name']}"[:20],
                {"kinds": [MEMBER_KIND], "#p": [agent["pubkey"]]},
            ]
        )
    )
    await ws.send(
        json.dumps(
            [
                "REQ",
                f"own-{agent['name']}"[:20],
                {"kinds": list(CHAT_KINDS), "authors": [agent["pubkey"]], "since": since},
            ]
        )
    )
    return channels


async def _typing_heartbeat(
    agent: dict[str, Any], tags: list, stop: asyncio.Event, pub: WsPublisher
) -> None:
    while not stop.is_set():
        try:
            await pub.publish(agent, TYPING_KIND, "", tags)
        except Exception:
            log.exception("typing heartbeat failed for %s", agent["name"])
        try:
            await asyncio.wait_for(stop.wait(), timeout=au.TYPING_HEARTBEAT_SECS)
        except asyncio.TimeoutError:
            continue


async def _run_job(
    agent: dict[str, Any], evt: dict[str, Any], pub: WsPublisher, tracker: TurnTracker
) -> None:
    reaction_ids: list[str] = []
    stop_typing = asyncio.Event()
    channel = au.channel_from_event(evt)
    react_tags = au.reaction_tags(evt, channel)
    typing_task = asyncio.create_task(
        _typing_heartbeat(agent, au.typing_tags_for(evt), stop_typing, pub),
        name=f"typing-{agent['name']}",
    )
    try:
        seen_id = await pub.publish(agent, REACTION_KIND, au.REACTION_SEEN, react_tags)
        if seen_id:
            reaction_ids.append(seen_id)
        working_id = await pub.publish(agent, REACTION_KIND, au.REACTION_WORKING, react_tags)
        if working_id:
            reaction_ids.append(working_id)
        await tracker.register(channel, reaction_ids, stop_typing)
        await asyncio.to_thread(execute_job, agent, evt)
    except Exception:
        log.exception("job execute failed for %s", agent["name"])
    finally:
        job = await tracker.take(channel)
        stop_typing.set()
        try:
            await typing_task
        except Exception:
            log.exception("typing heartbeat task failed for %s", agent["name"])
        if job:
            try:
                await _retract_reactions(agent, pub, job.get("ids") or [], channel)
            except Exception:
                log.exception("reaction cleanup failed for %s", agent["name"])


async def agent_loop(agent: dict[str, Any], seen: SeenStore) -> None:
    relay = agent["relay"]
    pub = WsPublisher()
    tracker = TurnTracker()
    while True:
        try:
            async with websockets.connect(relay, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
                log.info("connected %s -> %s", agent["name"], relay)
                since = int(time.time()) - 120
                auth_id = ""
                authed = False
                channels: dict[str, str] = {}
                subscribed: set[str] = set()
                reconnect = False
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        if not isinstance(msg, list) or not msg:
                            continue
                        typ = msg[0]
                        if typ == "AUTH" and len(msg) >= 2:
                            challenge = str(msg[1])
                            ev = nip42_auth_event(
                                agent["secret"],
                                relay,
                                challenge,
                                extra_tags=agent.get("auth_tags") or [],
                            )
                            auth_id = str(ev.get("id") or "")
                            await ws.send(json.dumps(["AUTH", ev]))
                            continue
                        if typ == "OK" and len(msg) >= 3:
                            eid = str(msg[1])
                            ok = msg[2] is True
                            reason = str(msg[3]) if len(msg) > 3 else ""
                            if eid == auth_id:
                                if ok:
                                    authed = True
                                    log.info("auth ok %s", agent["name"])
                                    pub.attach(ws)
                                    channels = await after_auth(ws, agent, since)
                                    subscribed = set(channels)
                                    await pub.publish(agent, PRESENCE_KIND, "online", [])
                                    log.info("presence online %s", agent["name"])
                                else:
                                    log.warning("auth rejected %s: %s", agent["name"], reason)
                                    reconnect = True
                                    break
                            elif not ok:
                                log.warning("ok false for %s: %s", agent["name"], reason)
                            continue
                        if typ == "NOTICE":
                            log.info("notice %s: %s", agent["name"], msg[1] if len(msg) > 1 else msg)
                            continue
                        if typ == "CLOSED":
                            log.warning("closed %s: %s", agent["name"], msg[1:] if len(msg) > 1 else msg)
                            reconnect = True
                            break
                        if typ == "EOSE":
                            continue
                        if typ == "EVENT" and len(msg) >= 3:
                            evt = msg[2]
                            if not isinstance(evt, dict):
                                continue
                            kind = evt.get("kind")
                            if kind in {MEMBER_KIND, MEMBER_ADDED_KIND, MEMBER_REMOVED_KIND}:
                                _ch, subscribed, to_close, to_sub = au.apply_membership_event(
                                    int(kind), evt, channels, subscribed
                                )
                                for channel_id in to_close:
                                    await unsubscribe_channel(ws, channel_id, subscribed)
                                for channel_id, ch_type in to_sub:
                                    await subscribe_channel(
                                        ws, agent, channel_id, ch_type, since, subscribed
                                    )
                                continue
                            eid = str(evt.get("id") or "")
                            if not eid or seen.has(agent["pubkey"], eid):
                                continue
                            if (
                                str(evt.get("pubkey") or "") == agent["pubkey"]
                                and kind in CHAT_KINDS
                            ):
                                seen.add(agent["pubkey"], eid)
                                channel = au.channel_from_event(evt)
                                job = await tracker.take(channel)
                                if job:
                                    job["stop"].set()
                                    try:
                                        await _retract_reactions(agent, pub, job.get("ids") or [], channel)
                                    except Exception:
                                        log.exception("reaction retract on reply failed for %s", agent["name"])
                                continue
                            if not au.should_handle(agent, evt, channels):
                                continue
                            seen.add(agent["pubkey"], eid)
                            channel = au.channel_from_event(evt)
                            log.info(
                                "mention %s kind=%s channel=%s event=%s",
                                agent["name"],
                                kind,
                                channel[:8],
                                eid[:12],
                            )
                            asyncio.create_task(_run_job(agent, evt, pub, tracker))
                finally:
                    pub.detach(ws)
                if not authed:
                    log.warning("socket ended before auth for %s", agent["name"])
                if reconnect:
                    await asyncio.sleep(5)
                    continue
        except asyncio.CancelledError:
            log.info("cancelled %s", agent["name"])
            raise
        except Exception:
            log.exception("socket error %s; reconnect in 5s", agent["name"])
            await asyncio.sleep(5)


async def supervise(seen: SeenStore) -> None:
    running: dict[str, tuple[asyncio.Task[None], str]] = {}
    while True:
        try:
            agents = load_agents()
            wanted = {a["pubkey"]: a for a in agents}
            for pk, (task, fp) in list(running.items()):
                agent = wanted.get(pk)
                if agent is None or agent.get("fingerprint") != fp:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                    del running[pk]
                    log.info("stopped agent loop pubkey=%s", pk[:12])
            for pk, agent in wanted.items():
                if pk in running:
                    continue
                task = asyncio.create_task(agent_loop(agent, seen), name=f"agent-{agent['name']}")
                running[pk] = (task, str(agent.get("fingerprint") or ""))
                log.info("started agent loop %s", agent["name"])
        except Exception:
            log.exception("supervise tick failed")
        await asyncio.sleep(1)


def ensure_sync_token() -> str:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = AGENTS_DIR / "_sync.token"
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    path.write_text(token + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return token


def find_agent_path(pubkey: str) -> pathlib.Path | None:
    pubkey = (pubkey or "").lower()
    found = au.find_env_by_pubkey(AGENTS_DIR, pubkey)
    if found is not None:
        return found
    if not AGENTS_DIR.is_dir() or not au.PUBKEY_RE.match(pubkey):
        return None
    for path in sorted(AGENTS_DIR.glob("*.env")):
        if path.name.startswith("_"):
            continue
        env = au.load_env_file(path)
        nsec = env.get("BUZZ_PRIVATE_KEY") or ""
        if not nsec:
            continue
        try:
            if pubkey_hex(nsec_to_secret(nsec)) == pubkey:
                return path
        except ValueError:
            continue
    return None


def _existing_nsec(pubkey: str) -> tuple[str, str | None]:
    path = find_agent_path(pubkey)
    if path is None:
        return "", None
    env = au.load_env_file(path)
    return env.get("BUZZ_PRIVATE_KEY", ""), path.stem


class ApplyAuthError(Exception):
    """Owner/actor check failed for a worker apply."""


def _agent_from_env_path(path: pathlib.Path) -> dict[str, Any] | None:
    env = au.load_env_file(path)
    nsec = env.get("BUZZ_PRIVATE_KEY") or ""
    declared = (env.get("BUZZ_PUBKEY") or "").lower()
    derived = ""
    if nsec:
        try:
            derived = pubkey_hex(nsec_to_secret(nsec))
        except ValueError:
            derived = ""
    pubkey = derived or declared
    if not au.PUBKEY_RE.match(pubkey):
        return None
    return au.parse_loaded_agent(path, env, pubkey)


def load_agent_record(*, pubkey: str = "", slug: str = "") -> dict[str, Any] | None:
    path = None
    if au.PUBKEY_RE.match((pubkey or "").lower()):
        path = find_agent_path(pubkey)
    elif slug:
        candidate = AGENTS_DIR / f"{au.slug_name(slug)}.env"
        if candidate.is_file():
            path = candidate
    if path is None:
        return None
    return _agent_from_env_path(path)


def worker_token_ok(token: str) -> bool:
    if _worker_token_checker is not None:
        return bool(_worker_token_checker(token))
    email = (os.environ.get("GOOSE_WORKER_SA") or GOOSE_WORKER_SA or "").strip().lower()
    audience = (
        os.environ.get("WORKER_APPLY_AUDIENCE")
        or os.environ.get("LISTENER_CONTROL_URL")
        or WORKER_APPLY_AUDIENCE
        or ""
    ).strip()
    if not token or not email or not audience:
        return False
    try:
        from google.auth.transport import requests as greq
        from google.oauth2 import id_token

        info = id_token.verify_oauth2_token(token, greq.Request(), audience=audience)
    except Exception:
        return False
    got = str(info.get("email") or "").strip().lower()
    return got == email


def _require_owner_actor(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    author = str(body.get("author_pubkey") or "").strip().lower()
    if not au.PUBKEY_RE.match(author):
        raise ValueError("author_pubkey required")
    actor_pk = str(body.get("actor_pubkey") or "").strip().lower()
    actor_slug = str(body.get("actor_slug") or body.get("actor") or "").strip()
    actor = None
    if au.PUBKEY_RE.match(actor_pk):
        actor = load_agent_record(pubkey=actor_pk)
    if actor is None and actor_slug:
        actor = load_agent_record(slug=actor_slug)
    if actor is None:
        raise ValueError("actor agent not found")
    owner = str(actor.get("owner") or "").strip().lower()
    if owner != author:
        raise ApplyAuthError("author is not the actor owner")
    return author, actor


def worker_apply_create(body: dict[str, Any]) -> dict[str, Any]:
    author, _actor = _require_owner_actor(body)
    display = str(body.get("name") or body.get("display") or "").strip() or "agent"
    prompt = body.get("system_prompt")
    if prompt is None:
        raise ValueError("system_prompt required")
    nsec, pubkey = generate_nsec()
    result = upsert_from_api(
        pubkey,
        {
            "nsec": nsec,
            "name": display,
            "slug": str(body.get("slug") or display),
            "system_prompt": str(prompt),
            "respond_to": "owner-only",
            "auth_tag": au.owner_auth_tag(author),
            "relay_url": str(body.get("relay_url") or body.get("relay") or RELAY_URL),
            "updated_at": au.utc_now(),
        },
    )
    return result


def worker_apply_update(pubkey: str, body: dict[str, Any]) -> dict[str, Any]:
    if body.get("nsec") or body.get("private_key_nsec"):
        raise ValueError("worker apply cannot set nsec")
    author, _actor = _require_owner_actor(body)
    pubkey = (pubkey or "").lower()
    path = find_agent_path(pubkey)
    if path is None:
        raise ValueError("agent not found")
    rec = _agent_from_env_path(path)
    if rec is None:
        raise ValueError("agent not found")
    target_owner = str(rec.get("owner") or "").strip().lower()
    if target_owner != author:
        raise ApplyAuthError("author is not the target owner")
    display = str(body.get("name") or body.get("display") or rec.get("display") or path.stem)
    if "system_prompt" in body:
        prompt: str | None = str(body.get("system_prompt") or "")
    else:
        prompt = au.load_instructions(AGENTS_DIR, path.stem)
    return upsert_from_api(
        pubkey,
        {
            "name": display,
            "slug": rec.get("name") or path.stem,
            "system_prompt": prompt,
            "respond_to": rec.get("respond_to") or "owner-only",
            "respond_to_allowlist": rec.get("respond_to_allowlist") or [],
            "team_id": rec.get("team_id") or "",
            "auth_tag": rec.get("auth_tag_raw") or "",
            "relay_url": rec.get("relay") or RELAY_URL,
            "channel_allowlist": rec.get("channel_allowlist") or [],
            "updated_at": au.utc_now(),
        },
    )


def upsert_from_api(pubkey: str, body: dict[str, Any]) -> dict[str, Any]:
    pubkey = (pubkey or "").lower()
    if not au.PUBKEY_RE.match(pubkey):
        raise ValueError("invalid pubkey")
    existing_nsec, previous_slug = _existing_nsec(pubkey)
    nsec = str(body.get("nsec") or body.get("private_key_nsec") or existing_nsec)
    if not nsec:
        raise ValueError("nsec required to create agent")
    derived = pubkey_hex(nsec_to_secret(nsec))
    if derived != pubkey:
        raise ValueError("nsec does not match pubkey")
    display = str(body.get("name") or body.get("display") or previous_slug or "agent")
    slug = au.allocate_slug(AGENTS_DIR, str(body.get("slug") or display), pubkey)
    au.upsert_agent_files(
        AGENTS_DIR,
        slug=slug,
        nsec=nsec,
        display=display,
        relay=str(body.get("relay_url") or body.get("relay") or RELAY_URL),
        auth_tag=str(body.get("auth_tag") or ""),
        pubkey=pubkey,
        respond_to=str(body.get("respond_to") or "owner-only"),
        respond_to_allowlist=au.parse_allowlist(body.get("respond_to_allowlist")),
        team_id=str(body.get("team_id") or ""),
        updated_at=str(body.get("updated_at") or au.utc_now()),
        system_prompt=body.get("system_prompt") if "system_prompt" in body else None,
        channel_allowlist=list(body.get("channel_allowlist") or []),
        previous_slug=previous_slug,
        team_instructions=body.get("team_instructions") if "team_instructions" in body else None,
    )
    return {"ok": True, "agent_id": slug, "pubkey": pubkey}


def delete_from_api(pubkey: str) -> dict[str, Any]:
    pubkey = (pubkey or "").lower()
    path = find_agent_path(pubkey)
    if path is None:
        return {"ok": True, "deleted": False}
    au.delete_agent_files(AGENTS_DIR, path.stem)
    return {"ok": True, "deleted": True, "agent_id": path.stem}


def list_public_agents() -> list[dict[str, Any]]:
    out = []
    for agent in load_agents():
        inst = au.load_instructions(AGENTS_DIR, agent["name"])
        team = au.load_team_file(AGENTS_DIR, agent["name"])
        inst_ts = au.file_mtime_iso(AGENTS_DIR / f"{agent['name']}.instructions")
        updated = agent.get("updated_at") or ""
        if inst_ts > updated:
            updated = inst_ts
        rec_agent = dict(agent)
        rec_agent["team_instructions"] = team
        out.append(au.public_record(rec_agent, inst, updated, include_secrets=True))
    return out


class ControlHandler(BaseHTTPRequestHandler):
    token = ""

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self) -> str:
        header = self.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return (self.headers.get("X-Buzz-Sync-Token") or "").strip()

    def _auth(self) -> bool:
        token = self._bearer()
        return bool(self.token) and token == self.token

    def _worker_auth(self) -> bool:
        header = self.headers.get("Authorization") or ""
        if not header.lower().startswith("bearer "):
            return False
        token = header[7:].strip()
        if not token or (self.token and token == self.token):
            return False
        return worker_token_ok(token)

    def _read_json(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 512_000:
            self._send(413, {"ok": False, "error": "payload too large"})
            return None
        try:
            body = json.loads(self.rfile.read(length) if length else b"{}")
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "error": "invalid json"})
            return None
        if not isinstance(body, dict):
            self._send(400, {"ok": False, "error": "invalid json"})
            return None
        return body

    def _send_apply(self, fn, *args) -> None:
        try:
            self._send(200, fn(*args))
        except ApplyAuthError as exc:
            self._send(403, {"ok": False, "error": str(exc)})
        except ValueError as exc:
            self._send(400, {"ok": False, "error": str(exc)})
        except Exception:
            log.exception("worker apply failed")
            self._send(500, {"ok": False, "error": "internal"})

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/health", "/healthz"}:
            self._send(200, {"ok": True})
            return
        if not self._auth():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        if path == "/agents":
            self._send(200, {"ok": True, "agents": list_public_agents()})
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path != "/agents":
            self._send(404, {"ok": False, "error": "not found"})
            return
        if not self._worker_auth():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        body = self._read_json()
        if body is None:
            return
        self._send_apply(worker_apply_create, body)

    def do_PUT(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        prefix = "/agents/"
        if not path.startswith(prefix):
            self._send(404, {"ok": False, "error": "not found"})
            return
        pubkey = path[len(prefix) :].strip().lower()
        sync = self._auth()
        worker = False if sync else self._worker_auth()
        if not sync and not worker:
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        body = self._read_json()
        if body is None:
            return
        if worker:
            self._send_apply(worker_apply_update, pubkey, body)
            return
        try:
            self._send(200, upsert_from_api(pubkey, body))
        except ValueError as exc:
            self._send(400, {"ok": False, "error": str(exc)})
        except Exception:
            log.exception("put agent failed")
            self._send(500, {"ok": False, "error": "internal"})

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._auth():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        path = self.path.split("?", 1)[0]
        prefix = "/agents/"
        if not path.startswith(prefix):
            self._send(404, {"ok": False, "error": "not found"})
            return
        pubkey = path[len(prefix) :].strip().lower()
        try:
            self._send(200, delete_from_api(pubkey))
        except ValueError as exc:
            self._send(400, {"ok": False, "error": str(exc)})
        except Exception:
            log.exception("delete agent failed")
            self._send(500, {"ok": False, "error": "internal"})


def start_control_api(token: str) -> ThreadingHTTPServer:
    ControlHandler.token = token
    httpd = ThreadingHTTPServer((CONTROL_HOST, CONTROL_PORT), ControlHandler)
    thread = threading.Thread(target=httpd.serve_forever, name="buzz-control", daemon=True)
    thread.start()
    log.info("control api on %s:%s", CONTROL_HOST, CONTROL_PORT)
    return httpd


async def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    token = ensure_sync_token()
    start_control_api(token)
    seen = SeenStore(STATE_DIR / "seen.json")
    await supervise(seen)


if __name__ == "__main__":
    asyncio.run(main())
