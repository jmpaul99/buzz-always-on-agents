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


def render_recipe(slug: str, spec: dict[str, Any]) -> str:
    title = str(spec.get("name") or spec.get("display_name") or slug)
    desc = str(spec.get("description") or f"Buzz session with {slug} already enabled")
    lines = [
        "version: 1.0.0",
        f"title: {yaml_scalar(title)}",
        f"description: {yaml_scalar(desc)}",
        "instructions: |",
        f"  This session already has {slug} enabled. Discover its tools with",
        "  list_functions, list_resources, or the Available tools list on a -32002.",
        "  Goose names tools extension__tool. Never invent names.",
        "  Do not enable Code Mode. Do not dump env or secrets.",
        "prompt: |",
        "  {{ message }}",
        "parameters:",
        "  - key: message",
        "    input_type: string",
        "    requirement: required",
        "    description: Full Buzz prompt for this mention",
        "extensions:",
    ]
    for always in ALWAYS_ON:
        emit_extension(lines, always, name=str(always["name"]))
    emit_extension(lines, spec, name=slug)
    lines.append("")
    return "\n".join(lines)


def write_recipes(config_text: str, recipes_dir: Path, catalog_path: Path | None = None) -> list[str]:
    extensions = parse_goose_extensions(config_text)
    mcps = task_mcps(extensions)
    recipes_dir.mkdir(parents=True, exist_ok=True)
    slugs: list[str] = []
    for slug, spec in mcps.items():
        dest = recipes_dir / slug
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "recipe.yaml").write_text(render_recipe(slug, spec), encoding="utf-8")
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
