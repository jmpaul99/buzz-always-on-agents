"""MCP catalog for native buzz-acp. Always-on buzz-dev-mcp; extras stay off."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SKIP = {"true", "false", "null", "none", "builtin", "platform", "stdio"}
BROWSER_SLUGS = {"playwright", "chromedevtools", "goosedocs"}


def default_catalog_path() -> Path:
    return Path(__file__).resolve().parent / "mcp-catalog.json"


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    raw = json.loads((path or default_catalog_path()).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("mcp catalog must be an object")
    return raw


def entries(catalog: dict[str, Any], group: str) -> list[dict[str, Any]]:
    items = catalog.get(group) or []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def extra_keywords(catalog: dict[str, Any]) -> list[str]:
    """Disabled extra slugs/names for LiteLLM COMPLEX routing."""
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

    for item in entries(catalog, "extras"):
        if item.get("enabled") is True:
            continue
        add(str(item.get("slug") or ""))
        add(str(item.get("name") or ""))
        add(str(item.get("display_name") or ""))
    return found
