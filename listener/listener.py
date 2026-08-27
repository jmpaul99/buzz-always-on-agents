#!/usr/bin/env python3
"""Thin control API for Desktop roster/nsec sync. Mentions are handled by
stock buzz-acp@<slug> on this VM. Never prints nsecs.

Token-auth control API on BUZZ_CONTROL_HOST:BUZZ_CONTROL_PORT (default
0.0.0.0:8743; firewall IAP range). Sidecar uses `_sync.token`; chat apply uses
a Google ID token from the listener SA (POST/PUT, plus GET /agents/index).
Never GET /agents (roster includes nsecs).
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import secrets
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import agentutil as au
from nostrutil import generate_nsec, nsec_to_secret, pubkey_hex

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("buzz-listener")

AGENTS_DIR = pathlib.Path(os.environ.get("BUZZ_AGENTS_DIR", "/etc/buzz"))
STATE_DIR = pathlib.Path(os.environ.get("BUZZ_STATE_DIR", "/var/lib/buzz-listener"))
RELAY_URL = os.environ.get("BUZZ_RELAY_URL", au.DEFAULT_RELAY)
CONTROL_HOST = os.environ.get("BUZZ_CONTROL_HOST", "0.0.0.0")
CONTROL_PORT = int(os.environ.get("BUZZ_CONTROL_PORT", "8743"))
APPLY_SA = (os.environ.get("APPLY_SA") or "").strip().lower()
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# Tests assign a callable(token) -> bool. Production uses Google ID tokens.
_worker_token_checker = None


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
        agents.append(au.parse_loaded_agent(path, env, derived))
        log.info("loaded agent %s pubkey=%s", path.stem, derived[:12])
    return agents


def sync_acp_unit(slug: str, *, running: bool) -> None:
    """Enable or disable buzz-acp@<slug>. No-op in tests or without systemd."""
    if os.environ.get("BUZZ_SKIP_ACP_UNIT") == "1":
        return
    if not SLUG_RE.match(slug or ""):
        return
    unit = f"buzz-acp@{slug}.service"
    try:
        if running:
            subprocess.run(["systemctl", "enable", unit], check=False, capture_output=True, timeout=30)
            subprocess.run(["systemctl", "restart", unit], check=False, capture_output=True, timeout=30)
        else:
            subprocess.run(
                ["systemctl", "disable", "--now", unit],
                check=False,
                capture_output=True,
                timeout=30,
            )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        log.warning("acp unit %s failed: %s", unit, type(exc).__name__)
        return
    log.info("acp unit %s running=%s", unit, running)


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
    """Owner/actor check failed for a chat apply."""


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


def list_agent_index() -> list[dict[str, Any]]:
    """Name/slug/pubkey only. Never include nsec."""
    out = []
    for agent in load_agents():
        out.append(
            {
                "pubkey": agent.get("pubkey") or "",
                "slug": agent.get("name") or "",
                "name": agent.get("display") or agent.get("name") or "",
            }
        )
    return out


def worker_token_ok(token: str) -> bool:
    if _worker_token_checker is not None:
        return bool(_worker_token_checker(token))
    email = (os.environ.get("APPLY_SA") or APPLY_SA or "").strip().lower()
    audience = (os.environ.get("LISTENER_CONTROL_URL") or "").strip()
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
    author, actor = _require_owner_actor(body)
    display = str(body.get("name") or body.get("display") or "").strip() or "agent"
    prompt = body.get("system_prompt")
    if prompt is None:
        raise ValueError("system_prompt required")
    nsec, pubkey = generate_nsec()
    relay = str(body.get("relay_url") or body.get("relay") or actor.get("relay") or RELAY_URL)
    return upsert_from_api(
        pubkey,
        {
            "nsec": nsec,
            "name": display,
            "slug": str(body.get("slug") or display),
            "system_prompt": str(prompt),
            "respond_to": "owner-only",
            "auth_tag": au.owner_auth_tag(author),
            "relay_url": relay,
            "updated_at": au.utc_now(),
        },
    )


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
    if previous_slug and previous_slug != slug:
        sync_acp_unit(previous_slug, running=False)
    sync_acp_unit(slug, running=True)
    return {"ok": True, "agent_id": slug, "pubkey": pubkey}


def delete_from_api(pubkey: str) -> dict[str, Any]:
    pubkey = (pubkey or "").lower()
    path = find_agent_path(pubkey)
    if path is None:
        return {"ok": True, "deleted": False}
    slug = path.stem
    au.delete_agent_files(AGENTS_DIR, slug)
    sync_acp_unit(slug, running=False)
    return {"ok": True, "deleted": True, "agent_id": slug}


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
            log.exception("apply failed")
            self._send(500, {"ok": False, "error": "internal"})

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/health", "/healthz"}:
            self._send(200, {"ok": True})
            return
        if path == "/agents/index":
            if not self._worker_auth():
                self._send(401, {"ok": False, "error": "unauthorized"})
                return
            self._send(200, {"ok": True, "agents": list_agent_index()})
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


def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    token = ensure_sync_token()
    start_control_api(token)
    log.info("control api ready; mentions are handled by buzz-acp@")
    threading.Event().wait()


if __name__ == "__main__":
    main()
