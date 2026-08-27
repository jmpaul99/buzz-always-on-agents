#!/usr/bin/env python3
"""Minimal newline JSON-RPC MCP server for multiplexer tests."""
from __future__ import annotations

import argparse
import json
import os
import sys

SECRET_KEYS = (
    "BUZZ_PRIVATE_KEY",
    "NOSTR_PRIVATE_KEY",
    "BUZZ_RELAY_URL",
    "BUZZ_AUTH_TAG",
    "LITELLM_MASTER_KEY",
    "OPENAI_COMPAT_API_KEY",
)


def read_msg() -> dict | None:
    header = sys.stdin.buffer.readline()
    if not header:
        return None
    if header.lower().startswith(b"content-length:"):
        length = int(header.split(b":", 1)[1].strip())
        while True:
            extra = sys.stdin.buffer.readline()
            if extra in (b"\r\n", b"\n", b""):
                break
        return json.loads(sys.stdin.buffer.read(length))
    line = header.strip()
    if not line:
        return read_msg()
    return json.loads(line)


def write_msg(msg: dict) -> None:
    sys.stdout.buffer.write((json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="fake")
    parser.add_argument("--tool", default="echo")
    args = parser.parse_args()
    while True:
        msg = read_msg()
        if msg is None:
            return 0
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            write_msg(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": args.name, "version": "0"},
                    },
                }
            )
            continue
        if rid is None:
            continue
        if method == "tools/list":
            write_msg(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "tools": [
                            {
                                "name": args.tool,
                                "description": f"{args.name} {args.tool}",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                },
                            }
                        ]
                    },
                }
            )
            continue
        if method == "tools/call":
            params = msg.get("params") or {}
            call_args = params.get("arguments") or {}
            leaked = [key for key in SECRET_KEYS if key in os.environ]
            body = json.dumps({"text": call_args.get("text") or "ok", "leaked": leaked, "server": args.name})
            write_msg(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"content": [{"type": "text", "text": body}], "isError": False},
                }
            )
            continue
        if method == "ping":
            write_msg({"jsonrpc": "2.0", "id": rid, "result": {}})
            continue
        write_msg({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": str(method)}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
