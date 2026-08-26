"""Task MCP catalog: parse goose/config.yaml, match mentions, emit recipes."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

TASK_TYPES = {"stdio", "streamable_http"}
ALWAYS_ON = (
    {
        "type": "platform",
        "name": "developer",
        "description": "Write and edit files, and execute shell commands",
        "bundled": True,
    },
    {
        "type": "platform",
        "name": "tom",
        "description": "Inject persistent instructions every turn",
        "bundled": True,
    },
    {
        "type": "platform",
        "name": "todo",
        "description": "Todo list for multi-step work",
        "bundled": True,
    },
)


def parse_goose_extensions(text: str) -> dict[str, dict[str, Any]]:
    in_ext = False
    slug = ""
    block: dict[str, Any] = {}
    list_key = ""
    map_key = ""
    out: dict[str, dict[str, Any]] = {}

    def flush() -> None:
        nonlocal slug, block, list_key, map_key
        if slug:
            out[slug] = block
        slug = ""
        block = {}
        list_key = ""
        map_key = ""

    def unquote(val: str) -> str:
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
            return val[1:-1]
        return val

    def assign(key: str, raw: str) -> None:
        val = unquote(raw)
        if val.lower() in {"true", "false"}:
            block[key] = val.lower() == "true"
        elif val.lower() in {"null", "~"}:
            block[key] = None
        elif re.fullmatch(r"-?\d+", val):
            block[key] = int(val)
        else:
            block[key] = val

    for line in text.splitlines():
        if line.startswith("extensions:"):
            in_ext = True
            continue
        if not in_ext:
            continue
        if line and not line[:1].isspace() and not line.startswith("#"):
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key_m = re.match(r"^  ([A-Za-z0-9_]+):\s*$", line)
        if key_m:
            flush()
            slug = key_m.group(1)
            continue
        if not slug:
            continue
        list_item = re.match(r"^    - (.+)$", line)
        if list_item and list_key:
            block.setdefault(list_key, []).append(unquote(list_item.group(1)))
            continue
        nested = re.match(r"^      ([A-Za-z0-9_]+):\s*(.*)$", line)
        if nested and map_key:
            nk, nv = nested.group(1), nested.group(2).strip()
            block.setdefault(map_key, {})[nk] = unquote(nv) if nv else ""
            continue
        field = re.match(r"^    ([A-Za-z0-9_]+):\s*(.*)$", line)
        if not field:
            continue
        key, rest = field.group(1), field.group(2).strip()
        list_key = ""
        map_key = ""
        if rest == "":
            if key in {"args", "env_keys"}:
                list_key = key
                block[key] = []
            else:
                map_key = key
                block[key] = {}
            continue
        assign(key, rest)
    flush()
    return out


def task_mcps(extensions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for slug, spec in extensions.items():
        if spec.get("enabled") is True:
            continue
        if str(spec.get("type") or "") not in TASK_TYPES:
            continue
        out[slug] = spec
    return out


def mcp_keywords(slug: str, spec: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for raw in (slug, spec.get("name"), spec.get("display_name")):
        token = str(raw or "").strip().strip("'\"").lower()
        if not token or len(token) < 3:
            continue
        if token not in found:
            found.append(token)
        for part in re.split(r"[^a-z0-9]+", token):
            if part and len(part) >= 3 and part not in found:
                found.append(part)
    return found


def catalog_records(extensions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for slug, spec in task_mcps(extensions).items():
        rows.append(
            {
                "slug": slug,
                "keywords": mcp_keywords(slug, spec),
            }
        )
    return rows


def _word_in(text: str, keyword: str) -> bool:
    kw = keyword.lower()
    if " " in kw:
        return kw in text
    return bool(re.search(r"\b" + re.escape(kw) + r"\b", text))


def match_task_recipe(message: str, catalog: list[dict[str, Any]]) -> str | None:
    text = (message or "").lower()
    hits: list[str] = []
    for row in catalog:
        slug = str(row.get("slug") or "")
        if not slug:
            continue
        keys = row.get("keywords") or []
        if any(_word_in(text, str(k)) for k in keys):
            hits.append(slug)
    if len(hits) == 1:
        return hits[0]
    return None


def load_catalog(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def default_catalog_path() -> Path:
    return Path(__file__).resolve().parent / "task-mcps.json"
