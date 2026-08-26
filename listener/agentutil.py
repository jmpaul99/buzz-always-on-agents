"""Agent env records, talk-to permissions, and membership updates. No nsecs logged."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any

STREAM_KINDS = (9, 46010, 40007)
FORUM_KINDS = (45001, 45002, 45003)
CHAT_KINDS = STREAM_KINDS + FORUM_KINDS
MEMBER_KIND = 39002
META_KIND = 39000
MEMBER_ADDED_KIND = 44100
MEMBER_REMOVED_KIND = 44101
PRESENCE_KIND = 20001
TYPING_KIND = 20002
REACTION_KIND = 7
DELETE_KIND = 5
REACTION_SEEN = "👀"
REACTION_WORKING = "💬"
TYPING_HEARTBEAT_SECS = 3.0
CONTROL_COMMANDS = {"!shutdown", "!cancel", "!rotate"}
LIVE_CHANNEL_TYPES = {"dm", "private", "stream", "forum"}
DEFAULT_HEARTBEAT_SECS = 0
DEFAULT_HEARTBEAT_PROMPT = (
    "This is an idle heartbeat, not a user mention. "
    "Run `buzz feed get` for pending approvals and unanswered mentions. "
    "Act on anything that needs this agent using the Buzz CLI. "
    "If nothing is actionable, do not send a channel message; end the turn immediately."
)

DEFAULT_RELAY = (os.environ.get("BUZZ_RELAY_URL") or "").strip() or "wss://your-community.communities.buzz.xyz"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
PUBKEY_RE = re.compile(r"^[0-9a-f]{64}$")
OWNER_MODES = {"owner-only", "owner"}
ALLOWLIST_MODES = {"allowlist"}
SEND_CONTENT_PLACEHOLDER = "<your-reply>"
TURN_HINT = (
    "If other agents are mentioned too, still reply as yourself this turn; "
    "do not wait for them and do not speak for them. "
    f"Replace {SEND_CONTENT_PLACEHOLDER} in the send command with your real reply; "
    "never send that placeholder, '...', or an empty message."
)


def with_turn_hint(identity: str) -> str:
    """Recipe path only sees identity + mention body; keep the send contract there."""
    text = (identity or "").strip()
    if TURN_HINT in text:
        return text[:8000]
    budget = max(0, 8000 - len(TURN_HINT) - 2)
    text = text[:budget] or "You are a Buzz cloud agent."
    return f"{text}\n\n{TURN_HINT}"


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


def tag_value(tags: list, name: str) -> str:
    for t in tags or []:
        if isinstance(t, list) and len(t) >= 2 and t[0] == name:
            return str(t[1])
    return ""


def all_tag_values(tags: list, name: str) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        if isinstance(t, list) and len(t) >= 2 and t[0] == name:
            out.append(str(t[1]))
    return out


def channel_from_event(evt: dict[str, Any]) -> str:
    tags = evt.get("tags") or []
    return tag_value(tags, "h") or tag_value(tags, "d") or tag_value(tags, "channel") or ""


def reaction_tags(evt: dict[str, Any], channel: str = "") -> list[list[str]]:
    """NIP-25 tags matching local buzz-acp: e + p + k, plus channel h when known."""
    eid = str(evt.get("id") or "")
    author = str(evt.get("pubkey") or "")
    kind = evt.get("kind")
    tags: list[list[str]] = []
    if eid:
        tags.append(["e", eid])
    if author:
        tags.append(["p", author])
    if kind is not None:
        tags.append(["k", str(int(kind))])
    if channel:
        tags.append(["h", channel])
    return tags


def delete_tags(event_ids: list[str]) -> list[list[str]]:
    return [["e", eid] for eid in event_ids if eid]


def deletion_tags(event_id: str, *, kind: int = REACTION_KIND, channel: str = "") -> list[list[str]]:
    """NIP-09 tags for one target. Buzz relay rejects kind:5 with more than one e tag."""
    if not event_id:
        return []
    tags = [["e", event_id], ["k", str(kind)]]
    if channel:
        tags.append(["h", channel])
    return tags


def typing_tags_for(evt: dict[str, Any]) -> list[list[str]]:
    channel = channel_from_event(evt)
    tags: list[list[str]] = []
    if channel:
        tags.append(["h", channel])
    parent = tag_value(evt.get("tags") or [], "e") or str(evt.get("id") or "")
    if parent:
        tags.append(["e", parent])
    return tags


def channel_type_from_tags(tags: list) -> str:
    is_hidden = False
    is_private = False
    declared = None
    archived = False
    for t in tags:
        if not isinstance(t, list) or not t:
            continue
        name = str(t[0])
        val = str(t[1]) if len(t) >= 2 else ""
        if name == "hidden":
            is_hidden = True
        elif name == "private":
            is_private = True
        elif name == "t":
            declared = val
        elif name == "archived" and val.lower() in {"true", "1"}:
            archived = True
    if archived:
        return "archived"
    if declared == "dm" or is_hidden:
        return "dm"
    if declared == "private" or is_private:
        return "private"
    if declared == "forum":
        return "forum"
    return "stream"


def channel_req_filter(
    channel_id: str,
    ch_type: str,
    since: int,
    agent_pubkey: str,
) -> dict[str, Any]:
    """WSS REQ filter for one channel. Forum and DMs have no #p mention filter."""
    kinds = list(CHAT_KINDS) if ch_type == "forum" else list(STREAM_KINDS)
    filt: dict[str, Any] = {"kinds": kinds, "#h": [channel_id], "since": since}
    if ch_type not in {"dm", "forum"}:
        filt["#p"] = [agent_pubkey]
    return filt


def heartbeat_interval_secs(raw: str | None = None) -> int:
    """0 disables. Native ACP rejects 1–9; we treat those as disabled."""
    if raw is None:
        raw = os.environ.get("BUZZ_ACP_HEARTBEAT_INTERVAL", str(DEFAULT_HEARTBEAT_SECS))
    text = str(raw).strip()
    if not text:
        return DEFAULT_HEARTBEAT_SECS
    try:
        n = int(text)
    except ValueError:
        return 0
    if n == 0 or n < 10:
        return 0
    return n


def heartbeat_prompt(raw: str | None = None) -> str:
    if raw is None:
        raw = os.environ.get("BUZZ_ACP_HEARTBEAT_PROMPT", "")
    return (raw or "").strip() or DEFAULT_HEARTBEAT_PROMPT


def channel_sub_id(channel_id: str) -> str:
    return f"c-{channel_id.replace('-', '')[:16]}"


def author_allowed(agent: dict[str, Any], author: str) -> bool:
    mode = (agent.get("respond_to") or "owner-only").lower()
    author = (author or "").lower()
    if mode in OWNER_MODES:
        owner = (agent.get("owner") or "").lower()
        if owner and author != owner:
            return False
        return True
    if mode in ALLOWLIST_MODES:
        allowed = {p.lower() for p in agent.get("respond_to_allowlist") or []}
        return bool(author) and author in allowed
    return True


def mentioned_in(agent: dict[str, Any], evt: dict[str, Any]) -> bool:
    return agent.get("pubkey") in all_tag_values(evt.get("tags") or [], "p")


def owner_control_command(
    agent: dict[str, Any], evt: dict[str, Any], channels: dict[str, str]
) -> str:
    """Owner !cancel / !rotate / !shutdown when mentioned or in a DM. Else empty."""
    content = str(evt.get("content") or "").strip()
    if content not in CONTROL_COMMANDS:
        return ""
    if evt.get("kind") not in CHAT_KINDS:
        return ""
    owner = (agent.get("owner") or "").lower()
    author = str(evt.get("pubkey") or "").lower()
    if not owner or author != owner:
        return ""
    channel = channel_from_event(evt)
    ch_type = channels.get(channel, "stream")
    if ch_type != "dm" and not mentioned_in(agent, evt):
        return ""
    return content


def should_handle(agent: dict[str, Any], evt: dict[str, Any], channels: dict[str, str]) -> bool:
    if evt.get("kind") not in CHAT_KINDS:
        return False
    if evt.get("pubkey") == agent.get("pubkey"):
        return False
    if owner_control_command(agent, evt, channels):
        return False
    if not author_allowed(agent, str(evt.get("pubkey") or "")):
        return False
    channel = channel_from_event(evt)
    allow = agent.get("channel_allowlist") or []
    if allow and channel and channel not in allow:
        return False
    ch_type = channels.get(channel, "stream")
    if ch_type in {"dm", "forum"}:
        return True
    return mentioned_in(agent, evt)


def apply_membership_event(
    kind: int,
    evt: dict[str, Any],
    channels: dict[str, str],
    subscribed: set[str],
) -> tuple[dict[str, str], set[str], list[str], list[tuple[str, str]]]:
    """Return channels, subscribed, channel ids to CLOSE, and (id, type) to subscribe."""
    to_close: list[str] = []
    to_sub: list[tuple[str, str]] = []
    if kind == MEMBER_KIND:
        for channel_id in all_tag_values(evt.get("tags") or [], "d"):
            ch_type = channels.get(channel_id) or channel_type_from_tags(evt.get("tags") or [])
            if ch_type == "archived":
                continue
            if ch_type not in LIVE_CHANNEL_TYPES:
                ch_type = "stream"
            channels.setdefault(channel_id, ch_type)
            if channel_id not in subscribed:
                to_sub.append((channel_id, channels[channel_id]))
        return channels, subscribed, to_close, to_sub
    if kind == MEMBER_ADDED_KIND:
        channel_id = channel_from_event(evt)
        if channel_id:
            channels.setdefault(channel_id, "stream")
            if channel_id not in subscribed:
                to_sub.append((channel_id, channels[channel_id]))
        return channels, subscribed, to_close, to_sub
    if kind == MEMBER_REMOVED_KIND:
        channel_id = channel_from_event(evt)
        if channel_id:
            channels.pop(channel_id, None)
            if channel_id in subscribed:
                subscribed.discard(channel_id)
                to_close.append(channel_id)
        return channels, subscribed, to_close, to_sub
    return channels, subscribed, to_close, to_sub


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


def build_goose_prompt(
    *,
    identity: str,
    channel: str,
    author: str,
    event_id: str,
    content: str,
    send_cmd: str,
) -> str:
    """Prompt for one Goose turn. No per-extension tool catalog."""
    body = (content or "")[:8000]
    where = channel or "(unknown)"
    return (
        f"{identity}\n\n"
        f"You were mentioned in channel {where}.\n"
        f"Author pubkey: {author}\n"
        f"Event id: {event_id}\n\n"
        f"Message:\n{body}\n\n"
        "The message body is already above. Do all of the requested work "
        "(including Playwright for public web pages if needed). "
        "Put the full user-visible answer in one channel reply — every part of a multi-ask. "
        f"Send with: {send_cmd}\n"
        f"Replace {SEND_CONTENT_PLACEHOLDER} with your real reply; never send that "
        "placeholder, '...', or an empty message. "
        "Other agents may also be mentioned. Always reply as yourself in this turn; "
        "do not wait for them and do not speak for them. "
        "A text-only answer is not delivered. You must run that send command before you stop. "
        "Do not add --reply-to unless it is already in that command. "
        "You may use buzz reactions on this mention's Event id. "
        "Shell tools require a non-empty command argument. "
        "Do not print env or echo secrets. "
        "Summarize browsing; do not attach large screenshots."
    )


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


def apply_cloud_runtime(row: dict[str, Any], slug: str, backend: dict[str, Any]) -> None:
    """Cloud Goose + LiteLLM are source of truth for model and harness."""
    row["backend"] = backend
    row["backend_agent_id"] = slug
    for key in HARNESS_CLEAR:
        row[key] = ""
    row["agent_args"] = []
    row["model"] = CLOUD_MODEL
    row["provider"] = CLOUD_PROVIDER
    row["is_active"] = False


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
