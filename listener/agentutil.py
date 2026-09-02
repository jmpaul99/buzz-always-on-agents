"""Agent env records, talk-to permissions, and Desktop roster merge. No nsecs logged."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any

DEFAULT_RELAY = (os.environ.get("BUZZ_RELAY_URL") or "").strip() or "wss://your-community.communities.buzz.xyz"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
PUBKEY_RE = re.compile(r"^[0-9a-f]{64}$")
OWNER_MODES = {"owner-only", "owner"}
ALLOWLIST_MODES = {"allowlist"}

LOCAL_RUNTIME_FIELDS = {
    "is_active",
    "runtime_pid",
    "start_on_app_launch",
    "agent_command",
    "agent_command_override",
    "agent_args",
    "acp_command",
    "mcp_command",
    "last_started_at",
    "last_stopped_at",
    "last_exit_code",
    "last_error",
    "last_error_code",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def slug_name(name: str) -> str:
    s = "".join(ch.lower() if ch.isalnum() else "-" for ch in (name or "agent"))
    s = "-".join(p for p in s.split("-") if p)
    return s[:32] or "agent"


def display_name_key(row: dict[str, Any]) -> str:
    return str(row.get("display_name") or row.get("name") or "").strip().lower()


def allocate_slug(agents_dir: pathlib.Path, slug: str, pubkey: str) -> str:
    """Keep slug if free or already this pubkey; otherwise suffix so we never clobber."""
    slug = slug_name(slug)
    pubkey = (pubkey or "").lower()
    path = agents_dir / f"{slug}.env"
    if path.is_file():
        env = load_env_file(path)
        existing = (env.get("BUZZ_PUBKEY") or "").lower()
        if existing and existing != pubkey:
            alt = slug_name(f"{slug}-{pubkey[:8]}")
            if alt == slug:
                alt = slug_name(f"a-{pubkey[:8]}")
            return alt
    return slug


def compact_desktop_records(records: list) -> tuple[list, list[dict[str, Any]]]:
    """One keyed agent per display name. Drop empty-pubkey drafts that duplicate a real card.

    Builtin persona stubs (empty pubkey + is_builtin) are kept. Custom empty-pubkey
    stubs that duplicate a keyed card are dropped, and that card is detached from
    the missing persona so Desktop lists it under Custom agents instead of Unknown.
    Duplicate identities that share a name keep the oldest created_at row;
    team_id/persona/avatar copy over if the keeper is missing them.
    """
    if not isinstance(records, list):
        return [], []
    keyed: dict[str, dict[str, Any]] = {}
    empty: list[dict[str, Any]] = []
    for row in records:
        if not isinstance(row, dict):
            continue
        pk = str(row.get("pubkey") or "").lower()
        if PUBKEY_RE.match(pk):
            prev = keyed.get(pk)
            if prev is None or str(row.get("updated_at") or "") >= str(prev.get("updated_at") or ""):
                keyed[pk] = row
        else:
            empty.append(row)

    by_name: dict[str, list[str]] = {}
    for pk, row in keyed.items():
        by_name.setdefault(display_name_key(row) or pk, []).append(pk)

    dropped: list[dict[str, Any]] = []
    drop_pk: set[str] = set()
    for pks in by_name.values():
        if len(pks) <= 1:
            continue
        pks.sort(key=lambda p: str(keyed[p].get("created_at") or "9999"))
        keep = pks[0]
        dst = keyed[keep]
        for pk in pks[1:]:
            src = keyed[pk]
            dropped.append(src)
            drop_pk.add(pk)
            for field in ("team_id", "persona_id", "avatar_url", "display_name"):
                if not dst.get(field) and src.get(field):
                    dst[field] = src[field]
    for pk in drop_pk:
        keyed.pop(pk, None)

    names = {display_name_key(row) for row in keyed.values()}
    personas = {str(row.get("persona_id") or "") for row in keyed.values() if row.get("persona_id")}
    slugs = {str(row.get("slug") or "") for row in keyed.values() if row.get("slug")}
    keep_empty: list[dict[str, Any]] = []
    for row in empty:
        if row.get("is_builtin"):
            keep_empty.append(row)
            continue
        name = display_name_key(row)
        persona = str(row.get("persona_id") or "")
        slug = str(row.get("slug") or "")
        if (name and name in names) or (persona and persona in personas) or (slug and slug in slugs):
            dropped.append(row)
            continue
        keep_empty.append(row)

    catalog: set[str] = set()
    for row in keep_empty:
        for key in ("slug", "persona_id"):
            val = str(row.get(key) or "")
            if val:
                catalog.add(val)
    for row in keyed.values():
        pid = str(row.get("persona_id") or "")
        if pid and pid not in catalog:
            row["persona_id"] = None
            if "persona_source_version" in row:
                row["persona_source_version"] = None

    seen: set[str] = set()
    keep_empty_ids = {id(row) for row in keep_empty}
    out: list[dict[str, Any]] = []
    for row in records:
        if not isinstance(row, dict):
            continue
        pk = str(row.get("pubkey") or "").lower()
        if PUBKEY_RE.match(pk):
            if pk in keyed and pk not in seen:
                out.append(keyed[pk])
                seen.add(pk)
            continue
        if id(row) in keep_empty_ids:
            out.append(row)
    return out, dropped


def parse_allowlist(raw: str | list | None) -> list[str]:
    if isinstance(raw, list):
        items = [str(x).strip().lower() for x in raw]
    else:
        items = [p.strip().lower() for p in str(raw or "").split(",")]
    out: list[str] = []
    for item in items:
        if PUBKEY_RE.match(item) and item not in out:
            out.append(item)
    return out


def format_allowlist(pubkeys: list[str] | None) -> str:
    return ",".join(parse_allowlist(pubkeys or []))


def load_env_file(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


GCP_PLACEHOLDER = "your-gcp-project"


def _first_gcp_value(*values: str) -> str:
    for raw in values:
        text = (raw or "").strip()
        if text and text not in {"-", GCP_PLACEHOLDER, "(unset)"}:
            return text
    return ""


def resolve_gcp_target(
    environ: dict[str, str] | None = None,
    file_env: dict[str, str] | None = None,
    gcloud_project: str = "",
) -> tuple[str, str, str]:
    """project, zone, instance for IAP. Skip the install placeholder."""
    environ = environ or {}
    file_env = file_env or {}
    project = _first_gcp_value(
        environ.get("BUZZ_GCP_PROJECT", ""),
        environ.get("GCP_PROJECT", ""),
        environ.get("GOOGLE_CLOUD_PROJECT", ""),
        file_env.get("BUZZ_GCP_PROJECT", ""),
        file_env.get("GCP_PROJECT", ""),
        file_env.get("GOOGLE_CLOUD_PROJECT", ""),
        gcloud_project,
    ) or GCP_PLACEHOLDER
    zone = _first_gcp_value(
        environ.get("BUZZ_GCP_ZONE", ""),
        environ.get("GCP_ZONE", ""),
        file_env.get("BUZZ_GCP_ZONE", ""),
        file_env.get("GCP_ZONE", ""),
    ) or "us-central1-a"
    instance = _first_gcp_value(
        environ.get("BUZZ_GCP_INSTANCE", ""),
        environ.get("LISTENER_INSTANCE", ""),
        file_env.get("BUZZ_GCP_INSTANCE", ""),
        file_env.get("LISTENER_INSTANCE", ""),
    ) or "buzz-listener"
    return project, zone, instance


def parse_auth_tags(raw: str) -> list:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], str):
        return [parsed]
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], list):
        return [t for t in parsed if isinstance(t, list)]
    return []


def owner_from_auth_tags(auth_tags: list) -> str:
    for t in auth_tags:
        if isinstance(t, list) and len(t) >= 2 and t[0] == "auth":
            return str(t[1]).lower()
    return ""


def owner_auth_tag(owner: str) -> str:
    owner = (owner or "").lower()
    if not PUBKEY_RE.match(owner):
        raise ValueError("invalid owner pubkey")
    return json.dumps(["auth", owner])


def env_fingerprint(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_mtime_iso(path: pathlib.Path) -> str:
    if not path.is_file():
        return ""
    ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def public_record(
    agent: dict[str, Any],
    instructions: str = "",
    updated_at: str = "",
    *,
    include_secrets: bool = False,
) -> dict[str, Any]:
    rec = {
        "pubkey": agent.get("pubkey") or "",
        "name": agent.get("display") or agent.get("name") or "",
        "slug": agent.get("name") or "",
        "system_prompt": instructions,
        "respond_to": agent.get("respond_to") or "owner-only",
        "respond_to_allowlist": list(agent.get("respond_to_allowlist") or []),
        "team_id": agent.get("team_id") or "",
        "team_instructions": str(agent.get("team_instructions") or ""),
        "updated_at": updated_at or agent.get("updated_at") or "",
        "relay_url": agent.get("relay") or "",
        "channel_allowlist": list(agent.get("channel_allowlist") or []),
        "owner": agent.get("owner") or "",
    }
    if include_secrets:
        rec["nsec"] = agent.get("nsec") or ""
        rec["auth_tag"] = agent.get("auth_tag_raw") or agent.get("auth_tag") or ""
    return rec


def write_env_file(path: pathlib.Path, fields: dict[str, str]) -> None:
    lines = [f"{k}={v}" for k, v in fields.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _try_parse_json(path: pathlib.Path) -> tuple[Any, bool]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None, False
    if not raw.strip():
        return None, False
    try:
        return json.loads(raw), True
    except json.JSONDecodeError:
        return None, False


def read_json_file(path: pathlib.Path, default: Any = None) -> Any:
    """Parse JSON at path. Empty/corrupt files fall back to a parseable sibling tmp.

    Desktop and older sidecars share `stem.tmp` (e.g. managed-agents.tmp). Never treat
    a 0-byte live file as an empty roster while a tmp still has valid JSON.
    """
    parsed, ok = _try_parse_json(path)
    if ok:
        return parsed
    parent = path.parent
    if not parent.is_dir():
        return default
    fallbacks: list[pathlib.Path] = []
    shared = path.with_suffix(".tmp")
    if shared.is_file():
        fallbacks.append(shared)
    try:
        extras = sorted(
            (p for p in parent.glob(path.name + ".*.tmp") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        extras = []
    fallbacks.extend(extras)
    for candidate in fallbacks:
        parsed, ok = _try_parse_json(candidate)
        if ok:
            return parsed
    return default


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        offset += os.write(fd, data[offset:])


def _write_in_place(path: pathlib.Path, data: bytes) -> None:
    """Write then truncate so a crash cannot leave a 0-byte destination."""
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(path), flags)
    try:
        _write_all(fd, data)
        os.ftruncate(fd, len(data))
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_text(
    path: pathlib.Path,
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    """Write text without using Desktop's `stem.tmp` name, then replace.

    On Windows, Buzz Desktop often holds managed-agents.json open, so os.replace
    raises PermissionError. Fall back to in-place write-then-truncate.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode(encoding)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(tmp), flags, 0o600 if mode is None else mode)
    replaced = False
    try:
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        try:
            os.replace(str(tmp), str(path))
            replaced = True
        except OSError:
            _write_in_place(path, data)
    finally:
        if not replaced:
            try:
                tmp.unlink()
            except OSError:
                pass


def upsert_agent_files(
    agents_dir: pathlib.Path,
    *,
    slug: str,
    nsec: str,
    display: str,
    relay: str,
    auth_tag: str,
    pubkey: str,
    respond_to: str,
    respond_to_allowlist: list[str],
    team_id: str,
    updated_at: str,
    system_prompt: str | None,
    channel_allowlist: list[str] | None = None,
    previous_slug: str | None = None,
    team_instructions: str | None = None,
) -> pathlib.Path:
    if not SLUG_RE.match(slug):
        raise ValueError("invalid agent slug")
    agents_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(agents_dir, 0o700)
    except OSError:
        pass
    env_path = agents_dir / f"{slug}.env"
    fields = {
        "BUZZ_RELAY_URL": relay or DEFAULT_RELAY,
        "BUZZ_PRIVATE_KEY": nsec,
        "BUZZ_AUTH_TAG": auth_tag or "",
        "BUZZ_ACP_DISPLAY_NAME": display or slug,
        "BUZZ_PUBKEY": pubkey or "",
        "BUZZ_ACP_RESPOND_TO": (respond_to or "owner-only").lower(),
        "BUZZ_ACP_RESPOND_TO_ALLOWLIST": format_allowlist(respond_to_allowlist),
        "BUZZ_TEAM_ID": team_id or "",
        "BUZZ_UPDATED_AT": updated_at or utc_now(),
        "BUZZ_CHANNEL_ALLOWLIST": ",".join(channel_allowlist or []),
    }
    write_env_file(env_path, fields)
    if system_prompt is not None:
        inst = agents_dir / f"{slug}.instructions"
        inst.write_text(system_prompt.rstrip() + "\n", encoding="utf-8")
        try:
            os.chmod(inst, 0o600)
        except OSError:
            pass
    if team_instructions is not None:
        write_team_file(agents_dir, slug, team_instructions)
    if previous_slug and previous_slug != slug and SLUG_RE.match(previous_slug):
        delete_agent_files(agents_dir, previous_slug)
    return env_path


def delete_agent_files(agents_dir: pathlib.Path, slug: str) -> None:
    if not SLUG_RE.match(slug):
        raise ValueError("invalid agent slug")
    for suffix in (".env", ".instructions", ".team"):
        path = agents_dir / f"{slug}{suffix}"
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def load_instructions(agents_dir: pathlib.Path, slug: str) -> str:
    path = agents_dir / f"{slug}.instructions"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()[:8000]
    except OSError:
        return ""


def write_team_file(agents_dir: pathlib.Path, slug: str, text: str) -> None:
    path = agents_dir / f"{slug}.team"
    cleaned = (text or "").strip()
    if not cleaned:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.write_text(cleaned[:8000] + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_team_file(agents_dir: pathlib.Path, slug: str) -> str:
    path = agents_dir / f"{slug}.team"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()[:8000]
    except OSError:
        return ""


def teams_records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("teams", "items", "records"):
            raw = data.get(key)
            if isinstance(raw, list):
                return [item for item in raw if isinstance(item, dict)]
    return []


def team_instructions_from_records(data: Any, team_id: str, cap: int = 8000) -> str:
    tid = (team_id or "").strip()
    if not tid:
        return ""
    for item in teams_records(data):
        if str(item.get("id") or "") != tid:
            continue
        return str(item.get("instructions") or "").strip()[:cap]
    return ""


def load_team_instructions(teams_path: pathlib.Path | str, team_id: str, cap: int = 8000) -> str:
    path = pathlib.Path(teams_path)
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return team_instructions_from_records(data, team_id, cap)


def apply_cloud_team_instructions(teams: list[dict[str, Any]], cloud_agents: list[Any]) -> bool:
    """Write cloud team instruction text onto local teams that already exist.

    Does not create teams. Fills empty local instructions, or overwrites when
    the cloud agent updated_at is newer than the team's updated_at.
    """
    by_id = {str(item.get("id") or ""): item for item in teams if item.get("id")}
    changed = False
    for cloud in cloud_agents:
        if not isinstance(cloud, dict):
            continue
        tid = str(cloud.get("team_id") or "").strip()
        text = str(cloud.get("team_instructions") or "").strip()[:8000]
        if not tid or not text or tid not in by_id:
            continue
        team = by_id[tid]
        local = str(team.get("instructions") or "").strip()
        if local == text:
            continue
        if not local or cloud_wins(str(cloud.get("updated_at") or ""), str(team.get("updated_at") or "")):
            team["instructions"] = text
            changed = True
    return changed


CLOUD_MODEL = "goose"
CLOUD_PROVIDER = "litellm"
HARNESS_CLEAR = ("agent_command", "agent_command_override", "acp_command", "mcp_command")
DEFAULT_TURN_TIMEOUT_SECONDS = 320
DEFAULT_PARALLELISM = 1

# Buzz Desktop serde requires these on every card. Fill only when missing.
DESKTOP_CARD_DEFAULTS: dict[str, Any] = {
    "turn_timeout_seconds": DEFAULT_TURN_TIMEOUT_SECONDS,
    "idle_timeout_seconds": None,
    "max_turn_duration_seconds": None,
    "parallelism": DEFAULT_PARALLELISM,
    "start_on_app_launch": False,
    "auto_restart_on_config_change": True,
    "is_builtin": False,
    "provider_policy_pending": False,
    "avatar_url": "",
    "runtime_pid": None,
    "last_started_at": None,
    "last_stopped_at": None,
    "last_exit_code": None,
    "last_error": None,
    "last_error_code": None,
    "persona_id": None,
    "persona_source_version": None,
    "provider_binary_path": None,
}


def ensure_desktop_card_fields(row: dict[str, Any]) -> bool:
    """Add Desktop-required fields without overwriting values already on the card."""
    if not isinstance(row, dict):
        return False
    changed = False
    defaults = dict(DESKTOP_CARD_DEFAULTS)
    if row.get("is_builtin"):
        defaults["parallelism"] = 10
        defaults["is_builtin"] = True
    for key, val in defaults.items():
        if key not in row:
            row[key] = val
            changed = True
    return changed


def apply_cloud_runtime(row: dict[str, Any], slug: str, backend: dict[str, Any]) -> None:
    """Cloud buzz-agent + LiteLLM are source of truth for model and harness."""
    row["backend"] = backend
    row["backend_agent_id"] = slug
    for key in HARNESS_CLEAR:
        row[key] = ""
    row["agent_args"] = []
    row["model"] = CLOUD_MODEL
    row["provider"] = CLOUD_PROVIDER
    row["is_active"] = False
    ensure_desktop_card_fields(row)


def parse_loaded_agent(path: pathlib.Path, env: dict[str, str], pubkey: str) -> dict[str, Any]:
    name = path.stem
    auth_tags = parse_auth_tags(env.get("BUZZ_AUTH_TAG", ""))
    allow = parse_allowlist(env.get("BUZZ_ACP_RESPOND_TO_ALLOWLIST", ""))
    channels = [c for c in (env.get("BUZZ_CHANNEL_ALLOWLIST") or "").split(",") if c]
    return {
        "name": name,
        "nsec": env.get("BUZZ_PRIVATE_KEY", ""),
        "pubkey": pubkey,
        "display": env.get("BUZZ_ACP_DISPLAY_NAME", name),
        "relay": env.get("BUZZ_RELAY_URL", DEFAULT_RELAY),
        "auth_tags": auth_tags,
        "auth_tag_raw": env.get("BUZZ_AUTH_TAG", ""),
        "owner": owner_from_auth_tags(auth_tags),
        "respond_to": (env.get("BUZZ_ACP_RESPOND_TO") or "owner-only").lower(),
        "respond_to_allowlist": allow,
        "team_id": env.get("BUZZ_TEAM_ID", ""),
        "updated_at": env.get("BUZZ_UPDATED_AT", ""),
        "channel_allowlist": channels,
        "fingerprint": env_fingerprint(path),
        "path": str(path),
    }


def find_env_by_pubkey(agents_dir: pathlib.Path, pubkey: str) -> pathlib.Path | None:
    pubkey = (pubkey or "").lower()
    if not PUBKEY_RE.match(pubkey):
        return None
    if not agents_dir.is_dir():
        return None
    for path in sorted(agents_dir.glob("*.env")):
        if path.name.startswith("_"):
            continue
        env = load_env_file(path)
        declared = (env.get("BUZZ_PUBKEY") or "").lower()
        if declared == pubkey:
            return path
    return None


def settings_fingerprint(row: dict[str, Any]) -> str:
    payload = {
        "name": row.get("name") or row.get("display") or "",
        "system_prompt": row.get("system_prompt") or "",
        "respond_to": row.get("respond_to") or "",
        "respond_to_allowlist": parse_allowlist(row.get("respond_to_allowlist")),
        "team_id": row.get("team_id") or "",
        "team_instructions": row.get("team_instructions") or "",
        "relay_url": row.get("relay_url") or row.get("relay") or "",
        "channel_allowlist": row.get("channel_allowlist") or [],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def parse_ts(value: str) -> str:
    return (value or "").strip()


def cloud_wins(cloud_updated: str, local_updated: str) -> bool:
    return parse_ts(cloud_updated) > parse_ts(local_updated)


def merge_cloud_into_row(row: dict[str, Any], cloud: dict[str, Any]) -> bool:
    if not cloud_wins(str(cloud.get("updated_at") or ""), str(row.get("updated_at") or "")):
        return False
    changed = False
    mapping: dict[str, Any] = {
        "system_prompt": cloud.get("system_prompt") or "",
        "respond_to": cloud.get("respond_to") or row.get("respond_to"),
        "respond_to_allowlist": list(cloud.get("respond_to_allowlist") or []),
        "team_id": cloud.get("team_id") or "",
        "name": cloud.get("name") or row.get("name"),
        "updated_at": cloud.get("updated_at") or row.get("updated_at"),
        "channel_allowlist": list(cloud.get("channel_allowlist") or []),
    }
    if cloud.get("name"):
        mapping["display_name"] = cloud.get("name")
    if cloud.get("relay_url") or cloud.get("relay"):
        mapping["relay_url"] = cloud.get("relay_url") or cloud.get("relay")
    for key, val in mapping.items():
        if row.get(key) != val:
            row[key] = val
            changed = True
    return changed


def desktop_row_from_cloud(cloud: dict[str, Any], *, template: dict[str, Any] | None = None) -> dict[str, Any]:
    """Stopped cloud-backed Desktop card. Does not include nsec."""
    template = template if isinstance(template, dict) else {}
    now = utc_now()
    name = str(cloud.get("name") or cloud.get("display") or "agent")
    pk = str(cloud.get("pubkey") or "").lower()
    slug = str(cloud.get("slug") or slug_name(name))
    updated = str(cloud.get("updated_at") or now)
    row: dict[str, Any] = {
        "pubkey": pk,
        "name": name,
        "display_name": name,
        "system_prompt": cloud.get("system_prompt") or "",
        "respond_to": cloud.get("respond_to") or "owner-only",
        "respond_to_allowlist": list(cloud.get("respond_to_allowlist") or []),
        "team_id": cloud.get("team_id") or "",
        "relay_url": cloud.get("relay_url") or cloud.get("relay") or DEFAULT_RELAY,
        "channel_allowlist": list(cloud.get("channel_allowlist") or []),
        "updated_at": updated,
        "created_at": updated,
        "is_active": False,
        "slug": slug,
        "backend_agent_id": slug,
        "auth_tag": cloud.get("auth_tag") or "",
    }
    if "id" in template or not template:
        row["id"] = str(uuid.uuid4())
    ensure_desktop_card_fields(row)
    return row


def _roster_pubkey(row: dict[str, Any]) -> str:
    pk = str(row.get("pubkey") or "").lower()
    return pk if PUBKEY_RE.match(pk) else ""


def agent_owner(row: dict[str, Any]) -> str:
    owner = str(row.get("owner") or "").lower()
    if PUBKEY_RE.match(owner):
        return owner
    tags = parse_auth_tags(str(row.get("auth_tag") or row.get("auth_tag_raw") or ""))
    return owner_from_auth_tags(tags)


def pubkey_from_nsec(nsec: str) -> str:
    """Derive hex pubkey from nsec. Optional; coincurve may be missing on Desktop."""
    try:
        from nostrutil import nsec_to_secret, pubkey_hex

        return pubkey_hex(nsec_to_secret(nsec))
    except Exception:
        return ""


def desktop_user_pubkeys(records: list, identity_nsec: str = "") -> set[str]:
    """Pubkeys for this Desktop user: local auth-tag owners plus identity nsec."""
    out: set[str] = set()
    for row in records or []:
        if not isinstance(row, dict):
            continue
        owner = agent_owner(row)
        if PUBKEY_RE.match(owner):
            out.add(owner)
    derived = pubkey_from_nsec((identity_nsec or "").strip())
    if PUBKEY_RE.match(derived):
        out.add(derived)
    return out


def user_can_access_agent(cloud: dict[str, Any], user_pubkeys: set[str] | list | None) -> bool:
    """True if this user may import the agent: owner, allowlisted, or everyone."""
    if not isinstance(cloud, dict):
        return False
    mode = str(cloud.get("respond_to") or "owner-only").lower()
    users = {str(p or "").lower() for p in (user_pubkeys or []) if PUBKEY_RE.match(str(p or "").lower())}
    if mode not in OWNER_MODES and mode not in ALLOWLIST_MODES:
        return True
    if not users:
        return False
    if mode in OWNER_MODES:
        owner = agent_owner(cloud)
        return bool(owner) and owner in users
    allowed = set(parse_allowlist(cloud.get("respond_to_allowlist")))
    return bool(users & allowed)


def apply_cloud_roster(
    records: list,
    cloud_agents: list,
    tracked: dict[str, Any] | None,
    user_pubkeys: set[str] | list | None = None,
) -> tuple[list, dict[str, Any], list[dict[str, Any]], list[str], list[str]]:
    """Merge cloud roster into Desktop cards.

    Import missing pubkeys that include an nsec and that this Desktop user can
    access (owner, allowlist, or everyone). Drop local keyed cards that were
    previously tracked and vanished from the full cloud roster. Leave untracked
    local drafts so a brand-new card can still push. Inaccessible cloud agents
    are not imported and do not count as vanished.

    Returns (records, tracked, imported, removed_pubkeys, updated_pubkeys).
    imported items are {pubkey, nsec, slug} for the sidecar secret store.
    """
    tracked_out: dict[str, Any] = dict(tracked or {})
    src = [row for row in records if isinstance(row, dict)] if isinstance(records, list) else []

    cloud_by_pk: dict[str, dict[str, Any]] = {}
    for cloud in cloud_agents or []:
        if not isinstance(cloud, dict):
            continue
        pk = str(cloud.get("pubkey") or "").lower()
        if PUBKEY_RE.match(pk):
            cloud_by_pk[pk] = cloud

    removed: list[str] = []
    kept: list[dict[str, Any]] = []
    for row in src:
        pk = _roster_pubkey(row)
        if pk and pk in tracked_out and pk not in cloud_by_pk:
            removed.append(pk)
            tracked_out.pop(pk, None)
            continue
        kept.append(row)

    live: dict[str, dict[str, Any]] = {}
    for row in kept:
        pk = _roster_pubkey(row)
        if pk:
            live[pk] = row

    imported: list[dict[str, Any]] = []
    updated: list[str] = []
    template = next((row for row in kept if _roster_pubkey(row)), None)

    for pk, cloud in cloud_by_pk.items():
        if pk in live:
            if merge_cloud_into_row(live[pk], cloud):
                updated.append(pk)
            continue
        if not user_can_access_agent(cloud, user_pubkeys):
            continue
        nsec = str(cloud.get("nsec") or cloud.get("private_key_nsec") or "").strip()
        if not nsec:
            continue
        row = desktop_row_from_cloud(cloud, template=template)
        kept.append(row)
        live[pk] = row
        slug = str(cloud.get("slug") or row.get("slug") or slug_name(str(row.get("name") or "agent")))
        imported.append({"pubkey": pk, "nsec": nsec, "slug": slug})
        tracked_out[pk] = {
            "fingerprint": settings_fingerprint(row),
            "slug": slug,
            "updated_at": row.get("updated_at") or "",
        }

    return kept, tracked_out, imported, removed, updated
