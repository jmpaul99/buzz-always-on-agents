#!/usr/bin/env python3
"""Propose/apply cloud agent create or instruction update after chat confirm.

Goose must not curl the listener. Pending JSON keeps turn-2 apply identical to
the proposed text. Never prints nsecs.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

PENDING_NAME = "pending-agent-apply.json"
PUBKEY_LEN = 64
RequestFn = Callable[..., Any]


def _env(env: dict[str, str] | None = None) -> dict[str, str]:
    return dict(os.environ if env is None else env)


def workspace_root(env: dict[str, str]) -> pathlib.Path:
    raw = (env.get("BUZZ_WORKSPACE") or os.environ.get("BUZZ_WORKSPACE") or "/mnt/buzz").strip()
    return pathlib.Path(raw or "/mnt/buzz")


def pending_path(env: dict[str, str]) -> pathlib.Path:
    slug = "".join(
        ch if ch.isalnum() or ch in "-_" else "-"
        for ch in (env.get("AGENT_NAME") or "agent").lower()
    )
    slug = "-".join(p for p in slug.split("-") if p) or "agent"
    return workspace_root(env) / "agents" / slug[:32] / PENDING_NAME


def _words(text: str) -> list[str]:
    out: list[str] = []
    for raw in (text or "").split():
        word = raw.strip(".,!?;:").lower()
        if word.startswith("@"):
            continue
        if word:
            out.append(word)
    return out


def is_confirm(text: str) -> bool:
    words = _words(text)
    return bool(words) and words[0] in {"confirm", "yes", "y"}


def is_cancel(text: str) -> bool:
    words = _words(text)
    return bool(words) and words[0] in {"cancel", "no", "n"}


def require_owner(env: dict[str, str]) -> tuple[str, str]:
    author = (env.get("BUZZ_AUTHOR_PUBKEY") or "").strip().lower()
    owner = (env.get("BUZZ_OWNER_PUBKEY") or "").strip().lower()
    if not author or not owner or author != owner:
        raise SystemExit("owner only: author is not the agent owner")
    return author, owner


def load_pending(env: dict[str, str]) -> dict[str, Any] | None:
    path = pending_path(env)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_pending(env: dict[str, str], payload: dict[str, Any]) -> pathlib.Path:
    path = pending_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def drop_pending(env: dict[str, str]) -> bool:
    path = pending_path(env)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def fetch_index(
    env: dict[str, str],
    *,
    request: RequestFn = urllib.request.urlopen,
    token: str = "",
) -> list[dict[str, Any]]:
    data = _control_call(env, "GET", "/agents/index", None, request=request, token=token)
    agents = data.get("agents") if isinstance(data.get("agents"), list) else []
    return [a for a in agents if isinstance(a, dict)]


def propose(
    env: dict[str, str],
    *,
    name: str,
    system_prompt: str,
    pubkey: str = "",
    create: bool = False,
) -> dict[str, Any]:
    require_owner(env)
    name = (name or "").strip() or "agent"
    prompt = (system_prompt or "").strip()
    if not prompt:
        raise SystemExit("instructions are required")
    pk = (pubkey or "").strip().lower()
    if create and pk:
        raise SystemExit("pass --pubkey to update or --create, not both")
    if not create and not pk:
        raise SystemExit(
            "update requires --pubkey (64 hex); run list then propose --pubkey …; --create for a new identity"
        )
    if pk and (len(pk) != PUBKEY_LEN or any(ch not in "0123456789abcdef" for ch in pk)):
        raise SystemExit("pubkey must be 64 hex chars")
    payload = {
        "op": "create" if create else "update",
        "name": name,
        "pubkey": pk,
        "system_prompt": prompt,
    }
    path = write_pending(env, payload)
    out = {"ok": True, "pending": str(path), "op": payload["op"], "name": name}
    if pk:
        out["pubkey"] = pk
    return out


def cancel(env: dict[str, str]) -> dict[str, Any]:
    dropped = drop_pending(env)
    return {"ok": True, "cancelled": dropped}


def _id_token(audience: str, request: RequestFn = urllib.request.urlopen) -> str:
    url = (
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        "service-accounts/default/identity?audience="
        + urllib.parse.quote(audience, safe="")
    )
    req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    with request(req, timeout=10) as resp:
        token = resp.read().decode("ascii")
    return token.strip()


def _control_call(
    env: dict[str, str],
    method: str,
    path: str,
    body: dict[str, Any] | None,
    *,
    request: RequestFn = urllib.request.urlopen,
    token: str = "",
) -> dict[str, Any]:
    base = (env.get("LISTENER_CONTROL_URL") or "").strip().rstrip("/")
    if not base:
        raise SystemExit("LISTENER_CONTROL_URL is not set")
    if not token:
        token = _id_token(base, request=request)
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if method != "GET":
        headers["Content-Type"] = "application/json"
        data = json.dumps(body or {}, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{base}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with request(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        try:
            err = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            err = str(exc)[:500]
        raise SystemExit(f"listener HTTP {exc.code}: {err}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SystemExit(f"listener unreachable: {type(exc).__name__}") from exc
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise SystemExit("listener returned non-json")
    if not isinstance(data, dict):
        raise SystemExit("listener returned non-object")
    if status >= 400 or data.get("ok") is False:
        raise SystemExit(str(data.get("error") or f"listener HTTP {status}"))
    return data


def apply(
    env: dict[str, str],
    *,
    request: RequestFn = urllib.request.urlopen,
    token: str = "",
) -> dict[str, Any]:
    require_owner(env)
    if not is_confirm(env.get("BUZZ_MESSAGE") or ""):
        raise SystemExit("apply requires the user message to start with confirm")
    pending = load_pending(env)
    if not pending:
        raise SystemExit("no pending agent apply")
    author = (env.get("BUZZ_AUTHOR_PUBKEY") or "").strip().lower()
    body = {
        "author_pubkey": author,
        "actor_slug": (env.get("AGENT_NAME") or "").strip(),
        "name": pending.get("name") or "agent",
        "system_prompt": pending.get("system_prompt") or "",
    }
    op = str(pending.get("op") or "create")
    pubkey = str(pending.get("pubkey") or "").strip().lower()
    if op == "update":
        if len(pubkey) != PUBKEY_LEN:
            raise SystemExit("pending update is missing pubkey")
        result = _control_call(
            env, "PUT", f"/agents/{pubkey}", body, request=request, token=token
        )
    else:
        result = _control_call(env, "POST", "/agents", body, request=request, token=token)
    drop_pending(env)
    result["op"] = op
    result["name"] = body["name"]
    return result


def _read_instructions(raw: str) -> str:
    if raw == "-":
        return sys.stdin.read()
    path = pathlib.Path(raw)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return raw


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    env = _env(env)
    parser = argparse.ArgumentParser(prog="buzz-cloud-agents")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_prop = sub.add_parser("propose", help="store pending create/update")
    p_prop.add_argument("--name", required=True, help="display name (label, not the id)")
    p_prop.add_argument(
        "--pubkey",
        default="",
        help="64 hex agent id from list; required to update",
    )
    p_prop.add_argument(
        "--create",
        action="store_true",
        help="mint a new identity (do not pass --pubkey)",
    )
    p_prop.add_argument(
        "--instructions",
        required=True,
        help="instruction text, a file path, or - for stdin",
    )
    sub.add_parser("apply", help="apply pending after user confirms")
    sub.add_parser("cancel", help="drop pending without applying")
    sub.add_parser("list", help="list live cloud agents (name, slug, pubkey)")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "propose":
            result = propose(
                env,
                name=args.name,
                pubkey=args.pubkey,
                create=bool(args.create),
                system_prompt=_read_instructions(args.instructions),
            )
        elif args.cmd == "apply":
            result = apply(env)
        elif args.cmd == "list":
            agents = fetch_index(env)
            result = {"ok": True, "agents": agents}
        else:
            result = cancel(env)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 2
        raise
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
