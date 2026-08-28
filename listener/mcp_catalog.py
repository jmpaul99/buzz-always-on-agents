"""MCP catalog for native buzz-acp. Always-on buzz-dev-mcp; extras stay off in git.

Runtime enablement is per-agent (mcp-enabled.json). Agent-registered extras live in
an overlay file so deploy-listener can overwrite mcp-catalog.json without wiping them.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

SKIP = {"true", "false", "null", "none", "builtin", "platform", "stdio"}
BROWSER_SLUGS = {"playwright", "chromedevtools", "goosedocs"}
MAX_ENABLED = 2
EXTRA_PAGE_SIZE = 12
ALLOWED_COMMANDS = {"npx", "uv", "uvx", "python", "python3", "github-mcp-server"}
_ENV_BRACE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
NPX_PACKAGE_RE = re.compile(r"^(@[A-Za-z0-9._-]+/)?[A-Za-z0-9._-]+$")
# Withheld from untrusted extras. Same identity set as block/buzz#6651
# (`is_buzz_identity_env`) plus LiteLLM/sync keys this VM also holds.
BUZZ_IDENTITY_ENV = (
    "BUZZ_PRIVATE_KEY",
    "NOSTR_PRIVATE_KEY",
    "BUZZ_RELAY_URL",
    "BUZZ_AUTH_TAG",
)
SECRET_ENV = {
    *BUZZ_IDENTITY_ENV,
    "LITELLM_MASTER_KEY",
    "OPENAI_COMPAT_API_KEY",
    "BUZZ_SYNC_TOKEN",
}
PASSTHROUGH_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "SystemRoot",
    "COMSPEC",
    "PATHEXT",
)

_ARG_BAD = re.compile(r"[\x00\r\n]")


def default_catalog_path() -> Path:
    return Path(os.environ.get("MCP_CATALOG_PATH") or Path(__file__).resolve().parent / "mcp-catalog.json")


def default_overlay_path() -> Path:
    return Path(os.environ.get("MCP_OVERLAY_PATH") or "/etc/buzz/_mcp-overlay.json")


def default_workspace() -> Path:
    return Path(os.environ.get("BUZZ_WORKSPACE") or "/var/lib/buzz-listener")


def enabled_path(agent_name: str, workspace: Path | None = None) -> Path:
    return (workspace or default_workspace()) / "agents" / agent_name / "mcp-enabled.json"


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    raw = json.loads((path or default_catalog_path()).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("mcp catalog must be an object")
    return raw


def load_overlay(path: Path | None = None) -> dict[str, Any]:
    overlay_path = path or default_overlay_path()
    if not overlay_path.is_file():
        return {"extras": []}
    raw = json.loads(overlay_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("mcp overlay must be an object")
    extras = raw.get("extras")
    if extras is None:
        raw["extras"] = []
    elif not isinstance(extras, list):
        raise ValueError("mcp overlay extras must be a list")
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


def always_on_slugs(catalog: dict[str, Any]) -> set[str]:
    return {str(item.get("slug") or "") for item in entries(catalog, "always_on") if item.get("slug")}


def always_on_spec(catalog: dict[str, Any]) -> dict[str, Any]:
    items = entries(catalog, "always_on")
    if items:
        return items[0]
    return {"slug": "buzz-dev-mcp", "command": "buzz-dev-mcp", "args": [], "env_keys": []}


def merge_extras(catalog: dict[str, Any], overlay: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Shipped extras first; overlay extras with new slugs appended. Catalog wins on collision."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in entries(catalog, "extras"):
        slug = str(item.get("slug") or "")
        row = dict(item)
        row["_source"] = "catalog"
        result.append(row)
        if slug:
            seen.add(slug)
    for item in entries(overlay or {}, "extras"):
        slug = str(item.get("slug") or "")
        if not slug or slug in seen:
            continue
        row = dict(item)
        row["_source"] = "overlay"
        result.append(row)
        seen.add(slug)
    return result


def extras_by_slug(catalog: dict[str, Any], overlay: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    return {str(item.get("slug") or ""): item for item in merge_extras(catalog, overlay) if item.get("slug")}


def find_extra(slug: str, catalog: dict[str, Any], overlay: dict[str, Any] | None = None) -> dict[str, Any] | None:
    return extras_by_slug(catalog, overlay).get(slug)


def load_kv_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key:
            out[key] = val.strip().strip('"').strip("'")
    return out


def infer_agent_name(environ: dict[str, str], cwd: Path | None = None) -> str:
    name = str(environ.get("AGENT_NAME") or "").strip()
    if name and SLUG_RE.match(name):
        return name
    here = cwd if cwd is not None else Path(environ.get("PWD") or Path.cwd())
    if here.parent.name == "agents" and SLUG_RE.match(here.name):
        return here.name
    return name


def fill_missing_from_runtime(
    environ: dict[str, str],
    runtime_path: Path | None = None,
    *,
    cwd: Path | None = None,
) -> dict[str, str]:
    """Copy extra MCP credentials from the VM runtime file.

    buzz-agent only forwards Buzz identity into the MCP child (block/buzz#6651).
    GitHub/Tavily/Google keys live in ``/etc/buzz/_runtime.env``.
    """
    out = dict(environ)
    path = runtime_path if runtime_path is not None else Path(
        out.get("BUZZ_RUNTIME_ENV") or "/etc/buzz/_runtime.env"
    )
    for key, val in load_kv_env(path).items():
        if key in SECRET_ENV or is_buzz_identity_env(key):
            continue
        if str(out.get(key) or "").strip():
            continue
        if str(val or "").strip():
            out[key] = val
    if not str(out.get("AGENT_NAME") or "").strip():
        inferred = infer_agent_name(out, cwd)
        if inferred:
            out["AGENT_NAME"] = inferred
    return out


def missing_env_keys(item: dict[str, Any], env: dict[str, str] | None = None) -> list[str]:
    parent = env if env is not None else dict(os.environ)
    keys = item.get("env_keys") or []
    if not isinstance(keys, list):
        return []
    missing: list[str] = []
    for key in keys:
        if not isinstance(key, str) or not key:
            continue
        if not str(parent.get(key) or "").strip():
            missing.append(key)
    return missing


def extra_status(
    item: dict[str, Any],
    enabled_slugs: list[str],
    env: dict[str, str] | None = None,
    *,
    running: bool = False,
    starting: bool = False,
    tool_count: int = 0,
    last_error: str | None = None,
) -> dict[str, Any]:
    slug = str(item.get("slug") or "")
    if running:
        status = "running"
    elif starting or (slug in enabled_slugs and not last_error):
        status = "starting"
    elif last_error:
        status = "failed"
    else:
        status = "off"
    return {
        "slug": slug,
        "name": str(item.get("display_name") or item.get("name") or slug),
        "enabled": status in {"running", "starting"},
        "status": status,
        "running": running,
        "tool_count": tool_count,
        "last_error": last_error,
        "source": str(item.get("_source") or "catalog"),
        "missing_env_keys": missing_env_keys(item, env),
        "command": str(item.get("command") or ""),
    }


def load_enabled(path: Path) -> list[str]:
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return []
    items = raw.get("enabled") or []
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for item in items:
        if isinstance(item, str) and SLUG_RE.match(item) and item not in out:
            out.append(item)
    return out


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def save_enabled(path: Path, slugs: list[str]) -> None:
    cleaned: list[str] = []
    for slug in slugs:
        if isinstance(slug, str) and SLUG_RE.match(slug) and slug not in cleaned:
            cleaned.append(slug)
    _write_json(path, {"enabled": cleaned})


def save_overlay(path: Path, overlay: dict[str, Any]) -> None:
    extras = overlay.get("extras") or []
    if not isinstance(extras, list):
        extras = []
    _write_json(path, {"extras": [item for item in extras if isinstance(item, dict)]})


def enable_slug(enabled: list[str], slug: str) -> list[str]:
    if slug in enabled:
        return list(enabled)
    if len(enabled) >= MAX_ENABLED:
        raise ValueError(f"at most {MAX_ENABLED} extras enabled per agent")
    return list(enabled) + [slug]


def disable_slug(enabled: list[str], slug: str) -> list[str]:
    return [item for item in enabled if item != slug]


def page_tools(
    tools: list[dict[str, Any]],
    cursor: str | int | None = None,
    size: int = EXTRA_PAGE_SIZE,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return one page of tools and the next cursor (offset string), or None."""
    start = 0
    if cursor not in (None, ""):
        try:
            start = max(0, int(cursor))
        except (TypeError, ValueError):
            start = 0
    page = [dict(item) for item in tools[start : start + size]]
    nxt = start + size
    if nxt >= len(tools):
        return page, None
    return page, str(nxt)


def extra_tool_name(slug: str, tool: str) -> str:
    # Slugs cannot contain `_` (SLUG_RE). buzz-agent rejects bare tool names
    # that contain `__` (it uses that as server__tool itself).
    return f"{slug}_{tool}"


def split_extra_tool(name: str, slugs: list[str]) -> tuple[str, str] | None:
    for slug in sorted(slugs, key=len, reverse=True):
        prefix = f"{slug}_"
        if name.startswith(prefix) and name != prefix:
            return slug, name[len(prefix) :]
    return None


def is_buzz_identity_env(key: str) -> bool:
    """True for Buzz signing/relay/attestation vars (block/buzz#6651)."""
    return key in BUZZ_IDENTITY_ENV or key.endswith("_PRIVATE_KEY")


def child_env(
    env_keys: list[str],
    parent: dict[str, str] | None = None,
    *,
    trusted: bool = False,
) -> dict[str, str]:
    """Env for a child MCP.

    ``trusted=True`` is buzz-dev-mcp only: it may receive Buzz identity so
    ``buzz`` CLI works. Extras are untrusted (block/buzz#6651): PATH/HOME/GOOGLE_*
    and declared ``env_keys``, never identity.
    """
    src = parent if parent is not None else dict(os.environ)
    out: dict[str, str] = {}
    for key in PASSTHROUGH_ENV:
        if key in src and src[key] != "":
            out[key] = src[key]
    for key, val in src.items():
        if key.startswith("GOOGLE_") and not is_buzz_identity_env(key) and key not in SECRET_ENV:
            out[key] = val
    if trusted:
        for key in (*BUZZ_IDENTITY_ENV, "BUZZ_ACP_DISPLAY_NAME", "BUZZ_PUBKEY"):
            if key in src and src[key] != "":
                out[key] = src[key]
    for key in env_keys:
        if not isinstance(key, str) or not key:
            continue
        if (not trusted) and (key in SECRET_ENV or is_buzz_identity_env(key)):
            continue
        if key in src:
            out[key] = src[key]
    if not trusted:
        for key in list(out):
            if key in SECRET_ENV or is_buzz_identity_env(key):
                out.pop(key, None)
    return out


def extra_child_env(item: dict[str, Any], parent: dict[str, str] | None = None) -> dict[str, str]:
    """Untrusted extra env: declared keys plus catalog ``env`` (non-secret literals)."""
    out = child_env(list(item.get("env_keys") or []), parent, trusted=False)
    static = item.get("env") or {}
    if not isinstance(static, dict):
        return out
    for key, val in static.items():
        if not isinstance(key, str) or not isinstance(val, str) or not key:
            continue
        if key in SECRET_ENV or is_buzz_identity_env(key):
            continue
        out[key] = val
    return out


def expand_args(args: list[str], env: dict[str, str]) -> list[str]:
    """Replace ``${NAME}`` from env. Do not log the result (may contain tokens)."""

    def repl(match: re.Match[str]) -> str:
        return str(env.get(match.group(1) or "") or "")

    return [_ENV_BRACE.sub(repl, item) for item in args]


def _check_arg(value: str, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if _ARG_BAD.search(value) or "\x00" in value:
        raise ValueError(f"{label} contains a newline or null")
    if ".." in value.replace("\\", "/").split("/"):
        raise ValueError(f"{label} must not contain ..")


def _validate_npx_args(args: list[str]) -> None:
    if len(args) < 2 or args[0] != "-y":
        raise ValueError("npx extras must start with -y")
    if args[1] == "mcp-remote":
        if len(args) < 3 or not args[2].startswith("https://"):
            raise ValueError("npx mcp-remote requires an https:// URL")
        return
    if not NPX_PACKAGE_RE.match(args[1]):
        raise ValueError("npx package name is invalid")


def _validate_python_args(args: list[str]) -> None:
    scripts = [item for item in args if item.endswith(".py")]
    if not scripts:
        raise ValueError("python extras need a .py script path")
    script = scripts[-1]
    path = Path(script)
    if not path.is_absolute():
        raise ValueError("python script path must be absolute")


def validate_register(
    spec: dict[str, Any],
    *,
    catalog: dict[str, Any],
    overlay: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a cleaned extra spec or raise ValueError."""
    slug = str(spec.get("slug") or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise ValueError("slug must match [a-z0-9][a-z0-9-]{0,31}")
    if slug in BROWSER_SLUGS:
        raise ValueError(f"{slug} is not allowed")
    if slug in always_on_slugs(catalog):
        raise ValueError(f"{slug} is always-on and cannot be registered")
    if slug in extras_by_slug(catalog, overlay):
        raise ValueError(f"{slug} already exists; use mcp_enable")

    command = str(spec.get("command") or "").strip()
    if command not in ALLOWED_COMMANDS:
        raise ValueError(f"command must be one of {sorted(ALLOWED_COMMANDS)}")
    if "/" in command or "\\" in command:
        raise ValueError("command must be a bare executable name")

    args = spec.get("args")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError as exc:
            raise ValueError("args_json must be a JSON array of strings") from exc
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ValueError("args must be a list of strings")
    for item in args:
        _check_arg(item, "arg")
    if command == "npx":
        _validate_npx_args(args)
    elif command == "github-mcp-server":
        if args != ["stdio"]:
            raise ValueError("github-mcp-server args must be [\"stdio\"]")
    elif command in {"python", "python3"}:
        _validate_python_args(args)

    env_keys = spec.get("env_keys")
    if isinstance(env_keys, str):
        try:
            env_keys = json.loads(env_keys)
        except json.JSONDecodeError as exc:
            raise ValueError("env_keys_json must be a JSON array of strings") from exc
    if env_keys is None:
        env_keys = []
    if not isinstance(env_keys, list) or not all(isinstance(item, str) for item in env_keys):
        raise ValueError("env_keys must be a list of strings")
    parent = env if env is not None else dict(os.environ)
    for key in env_keys:
        if not key or key in SECRET_ENV:
            raise ValueError(f"env key {key!r} is not allowed")
        if not str(parent.get(key) or "").strip():
            raise ValueError(f"env key {key} is not set on this VM")

    name = str(spec.get("name") or slug).strip() or slug
    display = str(spec.get("display_name") or name).strip() or name
    return {
        "slug": slug,
        "enabled": False,
        "name": name,
        "display_name": display,
        "command": command,
        "args": list(args),
        "env_keys": list(env_keys),
    }


def append_overlay(
    spec: dict[str, Any],
    *,
    overlay_path: Path,
    catalog: dict[str, Any],
    overlay: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    current = overlay if overlay is not None else load_overlay(overlay_path)
    cleaned = validate_register(spec, catalog=catalog, overlay=current, env=env)
    extras = current.get("extras")
    if not isinstance(extras, list):
        extras = []
        current["extras"] = extras
    extras.append(cleaned)
    save_overlay(overlay_path, current)
    return cleaned
