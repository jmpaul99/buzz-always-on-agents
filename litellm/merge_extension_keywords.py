"""Merge disabled Goose extension names into LiteLLM custom_technical_keywords.

Run at LiteLLM image build so adding an MCP in goose/config.yaml updates routing
without a GitHub-specific keyword list.
"""
from __future__ import annotations

import re
import sys

SKIP = {"true", "false", "null", "none", "builtin", "platform", "stdio"}


def disabled_extension_keywords(goose_yaml: str) -> list[str]:
    in_ext = False
    current = ""
    enabled: bool | None = None
    names: list[str] = []
    found: list[str] = []

    def flush() -> None:
        nonlocal current, enabled, names
        if current and enabled is False:
            for raw in [current, *names]:
                token = raw.strip().strip("'\"").lower()
                if not token or token in SKIP or len(token) < 3:
                    continue
                if token not in found:
                    found.append(token)
                for part in re.split(r"[^a-z0-9]+", token):
                    if part and part not in SKIP and len(part) >= 3 and part not in found:
                        found.append(part)
        current = ""
        enabled = None
        names = []

    for line in goose_yaml.splitlines():
        if line.startswith("extensions:"):
            in_ext = True
            continue
        if not in_ext:
            continue
        if line and not line.startswith(" ") and not line.startswith("\t") and not line.startswith("#"):
            break
        key = re.match(r"^  ([A-Za-z0-9_]+):\s*$", line)
        if key:
            flush()
            current = key.group(1)
            continue
        if not current:
            continue
        flag = re.match(r"^    enabled:\s*(true|false)\s*$", line, re.I)
        if flag:
            enabled = flag.group(1).lower() == "true"
            continue
        field = re.match(r"^    (name|display_name):\s*(.+?)\s*$", line)
        if field:
            names.append(field.group(2))
    flush()
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
        print("usage: merge_extension_keywords.py GOOSE.yaml LITELLM.yaml OUT.yaml", file=sys.stderr)
        return 2
    goose = open(argv[1], encoding="utf-8").read()
    litellm = open(argv[2], encoding="utf-8").read()
    extra = disabled_extension_keywords(goose)
    out = merge_keyword_block(litellm, extra)
    with open(argv[3], "w", encoding="utf-8") as fh:
        fh.write(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
