#!/usr/bin/env python3
"""Minimal Content-Length JSON-RPC MCP server for multiplexer tests."""
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
    body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="fake")
    parser.add_argument("--tool", default="echo")
    parser.add_argument("--count", type=int, default=1)
    args = parser.parse_args()
    if args.count <= 1:
        tool_names = [args.tool]
    else:
        tool_names = [f"{args.tool}{i:02d}" for i in range(args.count)]
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
                                "name": tool_name,
                                "description": f"{args.name} {tool_name}",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                },
                            }
                            for tool_name in tool_names
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
