"""Forward Goose's localhost LiteLLM calls to Cloud Run with a Google ID token.

Goose's LiteLLM client is non-streaming: it POSTs and waits for one JSON body.
Copying upstream hop-by-hop headers (chunked / keep-alive, no Content-Length)
makes urllib wait for EOF that Cloud Run never sends, so Goose blocks after a
tool call and never reaches `buzz messages send`. Always close-delimit a
buffered JSON response.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("litellm-proxy")

UPSTREAM = os.environ["LITELLM_URL"].rstrip("/")
MASTER = os.environ.get("LITELLM_MASTER_KEY", "")
AUDIENCE = os.environ.get("LITELLM_AUDIENCE", UPSTREAM)
UPSTREAM_TIMEOUT = int(os.environ.get("LITELLM_PROXY_TIMEOUT_SECS", "120"))
METADATA = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/identity?audience="
)
DROP_HEADERS = {
    "host",
    "authorization",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
    "accept",
}
_in_flight = 0
_in_flight_lock = threading.Lock()


def _token() -> str:
    url = METADATA + urllib.parse.quote(AUDIENCE, safe="")
    req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("ascii")


def _disable_stream(body: bytes, content_type: str) -> bytes:
    if "json" not in (content_type or "").lower() or not body:
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(payload, dict) or payload.get("stream") is not True:
        return body
    payload["stream"] = False
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _tool_spec_name(spec: Any) -> str:
    if not isinstance(spec, dict):
        return ""
    fn = spec.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return str(fn["name"])
    return str(spec.get("name") or "")


def offered_tool_names(payload: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for spec in payload.get("tools") or []:
        name = _tool_spec_name(spec)
        if name:
            names.add(name)
    return names


def unique_prefixed_name(offered: set[str], name: str) -> str | None:
    """Map get_me → github__get_me when that suffix is unique among offered tools."""
    if not name or name in offered:
        return None
    suffix = "__" + name
    matches = [item for item in offered if item.endswith(suffix)]
    if len(matches) != 1:
        return None
    return matches[0]


def _rewrite_name_field(obj: Any, offered: set[str]) -> bool:
    if not isinstance(obj, dict):
        return False
    current = obj.get("name")
    if not isinstance(current, str):
        return False
    rewritten = unique_prefixed_name(offered, current)
    if not rewritten:
        return False
    obj["name"] = rewritten
    return True


def rewrite_response_tool_names(payload: dict[str, Any], offered: set[str]) -> bool:
    if not offered:
        return False
    changed = False
    for choice in payload.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        msg = choice.get("message") or choice.get("delta")
        if not isinstance(msg, dict):
            continue
        if _rewrite_name_field(msg.get("function_call"), offered):
            changed = True
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function")
            if isinstance(fn, dict):
                if _rewrite_name_field(fn, offered):
                    changed = True
            elif _rewrite_name_field(call, offered):
                changed = True
        content = msg.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") in {"tool_use", "tool_call"}:
                    if _rewrite_name_field(item, offered):
                        changed = True
    return changed


def apply_tool_name_rewrite(request_body: bytes, response_body: bytes, content_type: str) -> bytes:
    if "json" not in (content_type or "").lower() or not response_body:
        return response_body
    try:
        req = json.loads(request_body.decode("utf-8")) if request_body else {}
        resp = json.loads(response_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return response_body
    if not isinstance(req, dict) or not isinstance(resp, dict):
        return response_body
    if not rewrite_response_tool_names(resp, offered_tool_names(req)):
        return response_body
    return json.dumps(resp, separators=(",", ":")).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def _health(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b"ok")

    def _activity(self) -> None:
        with _in_flight_lock:
            n = _in_flight
        body = json.dumps({"in_flight": n}, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _reply(self, status: int, content_type: str, raw: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def _proxy(self) -> None:
        global _in_flight
        with _in_flight_lock:
            _in_flight += 1
        started = time.monotonic()
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            if self.command == "POST":
                body = _disable_stream(body, self.headers.get("Content-Type", ""))
            url = UPSTREAM + self.path
            headers = {
                k: v
                for k, v in self.headers.items()
                if k.lower() not in DROP_HEADERS
            }
            headers["Authorization"] = f"Bearer {_token()}"
            headers["Connection"] = "close"
            headers["Accept"] = "application/json"
            if body:
                headers["Content-Length"] = str(len(body))
            if MASTER:
                headers["x-litellm-api-key"] = MASTER
            req = urllib.request.Request(
                url, data=body or None, headers=headers, method=self.command
            )
            with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
                raw = resp.read()
                status = resp.status
                ctype = resp.headers.get("Content-Type", "application/json")
            if self.command == "POST":
                raw = apply_tool_name_rewrite(body, raw, ctype)
            self._reply(status, ctype, raw)
            log.info(
                "proxy %s %s status=%s bytes=%s secs=%.1f",
                self.command,
                self.path.split("?", 1)[0],
                status,
                len(raw),
                time.monotonic() - started,
            )
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            self._reply(exc.code, exc.headers.get("Content-Type", "text/plain"), raw)
            log.warning(
                "proxy HTTP %s %s %s secs=%.1f",
                exc.code,
                self.command,
                self.path.split("?", 1)[0],
                time.monotonic() - started,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            payload = json.dumps(
                {"error": {"message": "upstream timeout", "type": type(exc).__name__}}
            ).encode("utf-8")
            self._reply(504, "application/json", payload)
            log.warning(
                "proxy timeout %s %s err=%s secs=%.1f",
                self.command,
                self.path.split("?", 1)[0],
                type(exc).__name__,
                time.monotonic() - started,
            )
        finally:
            with _in_flight_lock:
                _in_flight -= 1

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.startswith("/health"):
            self._health()
            return
        if path == "/activity":
            self._activity()
            return
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802
        self._proxy()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._proxy()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 4000), Handler).serve_forever()
