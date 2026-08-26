"""Merge disabled MCP catalog extras into LiteLLM custom_technical_keywords.

Run at LiteLLM image build so adding an extra in listener/mcp-catalog.json
updates routing without a hand-maintained keyword list.
"""
from __future__ import annotations

import json
import re
import sys

SKIP = {"true", "false", "null", "none", "builtin", "platform", "stdio"}


def catalog_keywords(catalog_json: str) -> list[str]:
    data = json.loads(catalog_json)
    if not isinstance(data, dict):
        raise ValueError("mcp catalog must be an object")
    found: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        token = (token or "").strip().strip("'\"").lower()
        if not token or token in SKIP or len(token) < 3 or token in seen:
            return
        seen.add(token)
        found.append(token)
        for part in re.split(r"[^a-z0-9]+", token):
            if part and part not in SKIP and len(part) >= 3 and part not in seen:
                seen.add(part)
                found.append(part)

    extras = data.get("extras") or []
    if not isinstance(extras, list):
        return found
    for item in extras:
        if not isinstance(item, dict) or item.get("enabled") is True:
            continue
        add(str(item.get("slug") or ""))
        add(str(item.get("name") or ""))
        add(str(item.get("display_name") or ""))
    return found


def merge_keyword_block(litellm_yaml: str, extra: list[str]) -> str:
    match = re.search(
        r"(custom_technical_keywords:\s*)(\[[^\]]*\])",
        litellm_yaml,
    )
    if not match:
        raise ValueError("custom_technical_keywords list not found")
    inner = match.group(2).strip("[]")
    existing = [p.strip().strip("'\"") for p in inner.split(",") if p.strip()]
    seen = {p.lower() for p in existing}
    merged = list(existing)
    for item in extra:
        if item.lower() not in seen:
            merged.append(item)
            seen.add(item.lower())
    rendered = "[" + ", ".join(merged) + "]"
    return litellm_yaml[: match.start(2)] + rendered + litellm_yaml[match.end(2) :]


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: merge_extension_keywords.py CATALOG.json LITELLM.yaml OUT.yaml", file=sys.stderr)
        return 2
    catalog = open(argv[1], encoding="utf-8").read()
    litellm = open(argv[2], encoding="utf-8").read()
    extra = catalog_keywords(catalog)
    out = merge_keyword_block(litellm, extra)
    with open(argv[3], "w", encoding="utf-8") as fh:
        fh.write(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
