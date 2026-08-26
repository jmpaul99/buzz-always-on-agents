"""Copy Goose config, hints, and guardrails into an isolated HOME. No secrets logged."""
from __future__ import annotations

import pathlib
import shutil


def _copy_if_newer(src: pathlib.Path, dest: pathlib.Path) -> None:
    if not src.is_file():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.is_file() or src.stat().st_mtime > dest.stat().st_mtime:
        shutil.copy2(src, dest)


def sync_agent_home(base: pathlib.Path, home: pathlib.Path) -> None:
    cfg = home / ".config" / "goose"
    cfg.mkdir(parents=True, exist_ok=True)
    src_cfg = base / ".config" / "goose"
    _copy_if_newer(src_cfg / "config.yaml", cfg / "config.yaml")
    _copy_if_newer(src_cfg / ".goosehints", cfg / ".goosehints")
    _copy_if_newer(src_cfg / "guardrails.md", cfg / "guardrails.md")
    gcloud_src = base / ".config" / "gcloud"
    gcloud_dest = home / ".config" / "gcloud"
    if gcloud_src.is_dir() and not gcloud_dest.exists():
        shutil.copytree(gcloud_src, gcloud_dest, dirs_exist_ok=True)
    (home / ".npm").mkdir(parents=True, exist_ok=True)
