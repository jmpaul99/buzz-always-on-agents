"""Generate one Goose recipe per disabled stdio/streamable_http MCP.

Run at image build so adding an MCP in goose/config.yaml does not need a
hand-written recipe. Also writes listener/task-mcps.json for mention routing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "listener"))

from taskmcp import ALWAYS_ON, catalog_records, parse_goose_extensions, task_mcps  # noqa: E402

REPLY_SLUG = "reply"
MAX_TURNS = 25
SEND_INSTRUCTIONS = """{{ identity }}

You are a Buzz cloud agent. The user only sees the Buzz channel.

Always:
1. Do the requested work (tools, lookups, mem/canvas/file writes).
2. Post anything the user should see with: {{ send_cmd }}
   Replace <your-reply> with the actual text. Never send that placeholder, "...",
   or an empty message. A text-only assistant answer is not delivered.
3. If other agents are also mentioned, still reply as yourself this turn. Do not
   wait for them and do not speak for them.
4. Stop when the work is finished. A later user message is a new turn.

You are a Buzz CLI power user. `buzz --help` and `buzz <group> --help` are allowed.

messages  send, get, thread, search
          multiline: buzz messages send --channel <uuid> --content -
mem       ls / get / set / patch / rm. Never `buzz mem rm core`.
          multiline: printf '...' | buzz mem set mem/<topic> -
canvas    get / set --channel <uuid>
channels  list / join / leave / get
dms       list / get
users     get / search
huddle    get (owner-signed guidelines for this channel)
workflows / feed / social / repos / issues / pr / upload / projects
agents    buzz-cloud-agents propose / apply / cancel
          two-turn chat confirm: propose full instructions, ask the owner
          to reply confirm (or cancel), then apply. Never use
          buzz agents draft-create / draft-update. After apply the agent
          is live (no Desktop Save).

Core memory is already in Top of Mind when the [Agent Memory - core] section
is present. Follow it unless the user overrides. Keep core small (~10 KB);
durable detail goes to mem/<topic>. Memory is `buzz mem` only - this image
has no Goose memory extension. If that section is missing, do not create or
overwrite core this turn.

Paste buzz:// `link` fields verbatim in channel replies.

Never:
- A status ping or "working on it" send (typing and Agent Activity cover that)
- The todo extension
- buzz reactions unless the user asked to react
- Narrate channel ids, event ids, or recipe parameters
- Dump env or secrets
- Enable Code Mode
- Quoted newline escapes in send content (use --content -)"""


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    text = str(value)
    if (
        text == ""
        or text.lower() in {"true", "false", "null", "yes", "no"}
        or text[:1] in "-&*!|>%@`'\""
        or any(ch in text for ch in ":#{}[],\n")
        or " " in text
    ):
        return json.dumps(text)
    return text


def emit_extension(lines: list[str], spec: dict[str, Any], *, name: str) -> None:
    ext_type = str(spec.get("type") or "stdio")
    lines.append(f"  - type: {yaml_scalar(ext_type)}")
    lines.append(f"    name: {yaml_scalar(name)}")
    desc = spec.get("description")
    if desc:
        lines.append(f"    description: {yaml_scalar(desc)}")
    if spec.get("bundled") is True:
        lines.append("    bundled: true")
    cmd = spec.get("cmd")
    if cmd:
        lines.append(f"    cmd: {yaml_scalar(cmd)}")
    args = spec.get("args")
    if isinstance(args, list) and args:
        lines.append("    args:")
        for item in args:
            lines.append(f"      - {yaml_scalar(item)}")
    uri = spec.get("uri")
    if uri:
        lines.append(f"    uri: {yaml_scalar(uri)}")
    env_keys = spec.get("env_keys")
    if isinstance(env_keys, list) and env_keys:
        lines.append("    env_keys:")
        for item in env_keys:
            lines.append(f"      - {yaml_scalar(item)}")
    headers = spec.get("headers")
    if isinstance(headers, dict) and headers:
        lines.append("    headers:")
        for key, val in headers.items():
            lines.append(f"      {key}: {yaml_scalar(val)}")
    timeout = spec.get("timeout")
    if isinstance(timeout, int):
        lines.append(f"    timeout: {timeout}")


def _instruction_block(extra: list[str] | None = None) -> list[str]:
    lines = ["instructions: |"]
    for line in SEND_INSTRUCTIONS.splitlines():
        lines.append(f"  {line}" if line else "  ")
    for line in extra or []:
        lines.append(f"  {line}" if line else "  ")
    return lines


def render_recipe(slug: str, spec: dict[str, Any] | None = None) -> str:
    if spec is None:
        title = "Buzz reply"
        desc = "Default Buzz mention: do the work, then send on the channel"
        extra = []
    else:
        title = str(spec.get("name") or spec.get("display_name") or slug)
        desc = str(spec.get("description") or f"Buzz session with {slug} already enabled")
        extra = [
            f"This session already has {slug} enabled. Discover its tools with",
            "list_functions, list_resources, or the Available tools list on a -32002.",
            "Goose names tools extension__tool. Never invent names.",
        ]
    lines = [
        'version: "1.0.0"',
        f"title: {yaml_scalar(title)}",
        f"description: {yaml_scalar(desc)}",
        *_instruction_block(extra),
        "prompt: |",
        "  {{ message }}",
        "parameters:",
        "  - key: identity",
        "    input_type: string",
        "    requirement: optional",
        '    default: "You are a Buzz cloud agent."',
        "    description: Agent system identity",
        "  - key: message",
        "    input_type: string",
        "    requirement: required",
        "    description: Mention body only, not the full Goose prompt",
        "  - key: send_cmd",
        "    input_type: string",
        "    requirement: required",
        "    description: Exact buzz messages send command",
        "settings:",
        f"  max_turns: {MAX_TURNS}",
        "extensions:",
    ]
    for always in ALWAYS_ON:
        if str(always.get("name") or "") == "todo":
            continue
        emit_extension(lines, always, name=str(always["name"]))
    if spec is not None:
        emit_extension(lines, spec, name=slug)
    lines.append("")
    return "\n".join(lines)


def write_recipes(config_text: str, recipes_dir: Path, catalog_path: Path | None = None) -> list[str]:
    extensions = parse_goose_extensions(config_text)
    mcps = task_mcps(extensions)
    recipes_dir.mkdir(parents=True, exist_ok=True)
    slugs: list[str] = []
    reply_dir = recipes_dir / REPLY_SLUG
    reply_dir.mkdir(parents=True, exist_ok=True)
    (reply_dir / "recipe.yaml").write_text(
        render_recipe(REPLY_SLUG), encoding="utf-8", newline="\n"
    )
    for slug, spec in mcps.items():
        dest = recipes_dir / slug
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "recipe.yaml").write_text(
            render_recipe(slug, spec), encoding="utf-8", newline="\n"
        )
        slugs.append(slug)
    if catalog_path is not None:
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_text(
            json.dumps(catalog_records(extensions), indent=2) + "\n",
            encoding="utf-8",
        )
    return slugs


def main(argv: list[str]) -> int:
    if len(argv) not in {3, 4}:
        print(
            "usage: generate_recipes.py CONFIG.yaml RECIPES_DIR [TASK_MCPS.json]",
            file=sys.stderr,
        )
        return 2
    config_text = Path(argv[1]).read_text(encoding="utf-8")
    catalog = Path(argv[3]) if len(argv) == 4 else None
    slugs = write_recipes(config_text, Path(argv[2]), catalog)
    print(f"wrote {len(slugs)} recipes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
