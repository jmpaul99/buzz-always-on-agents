"""Minimal NIP-01 / NIP-19 / NIP-42 / NIP-98 helpers. Never log nsecs."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from typing import Any

from bech32 import bech32_decode, bech32_encode, convertbits
from coincurve import PrivateKey


def nsec_to_secret(nsec: str) -> bytes:
    nsec = nsec.strip()
    if nsec.startswith("nsec1"):
        hrp, data = bech32_decode(nsec)
        if hrp != "nsec" or data is None:
            raise ValueError("invalid nsec")
        raw = convertbits(data, 5, 8, False)
        if raw is None or len(raw) < 32:
            raise ValueError("invalid nsec payload")
        return bytes(raw[:32])
    if len(nsec) == 64:
        return bytes.fromhex(nsec)
    raise ValueError("BUZZ_PRIVATE_KEY must be nsec1 or 64-char hex")


def pubkey_hex(secret: bytes) -> str:
    pk = PrivateKey(secret)
    compressed = pk.public_key.format(compressed=True)
    return compressed[1:].hex()


def secret_to_nsec(secret: bytes) -> str:
    if len(secret) != 32:
        raise ValueError("secret must be 32 bytes")
    data = convertbits(list(secret), 8, 5, True)
    if data is None:
        raise ValueError("bech32 convert failed")
    encoded = bech32_encode("nsec", data)
    if not encoded:
        raise ValueError("bech32 encode failed")
    return encoded


def generate_nsec() -> tuple[str, str]:
    """Return (nsec1…, hex pubkey). Never log the nsec."""
    secret = os.urandom(32)
    return secret_to_nsec(secret), pubkey_hex(secret)


def _serialize(pubkey: str, created_at: int, kind: int, tags: list, content: str) -> bytes:
    payload = [0, pubkey, created_at, kind, tags, content]
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_event(secret: bytes, kind: int, tags: list, content: str = "", created_at: int | None = None) -> dict[str, Any]:
    created_at = created_at or int(time.time())
    pubkey = pubkey_hex(secret)
    raw = _serialize(pubkey, created_at, kind, tags, content)
    event_id = hashlib.sha256(raw).digest()
    sig = PrivateKey(secret).sign_schnorr(event_id)
    return {
        "id": event_id.hex(),
        "pubkey": pubkey,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
        "content": content,
        "sig": sig.hex(),
    }


def nip42_auth_event(
    secret: bytes,
    relay_url: str,
    challenge: str,
    extra_tags: list | None = None,
) -> dict[str, Any]:
    tags = [["relay", relay_url], ["challenge", challenge]]
    for t in extra_tags or []:
        if isinstance(t, list) and t:
            tags.append(t)
    return sign_event(secret, 22242, tags, "")


def relay_http_base(relay_url: str) -> str:
    url = (relay_url or "").strip().rstrip("/")
    if url.startswith("wss://"):
        return "https://" + url[6:]
    if url.startswith("ws://"):
        return "http://" + url[5:]
    return url


def nip98_authorization(secret: bytes, method: str, url: str, body: bytes = b"") -> str:
    tags = [
        ["u", url],
        ["method", method],
        ["nonce", str(uuid.uuid4())],
    ]
    if body:
        tags.append(["payload", hashlib.sha256(body).hexdigest()])
    event = sign_event(secret, 27235, tags, "")
    blob = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "Nostr " + base64.b64encode(blob).decode("ascii")


def tag_value(tags: list, name: str) -> str | None:
    for t in tags:
        if isinstance(t, list) and len(t) >= 2 and t[0] == name:
            return str(t[1])
    return None


def all_tag_values(tags: list, name: str) -> list[str]:
    out = []
    for t in tags:
        if isinstance(t, list) and len(t) >= 2 and t[0] == name:
            out.append(str(t[1]))
    return out
