"""Per-agent mention dedup. Same event id must still wake every mentioned agent."""
from __future__ import annotations

import json
import pathlib


def seen_key(agent_key: str, event_id: str) -> str:
    return f"{(agent_key or '').lower()}:{event_id}"


class SeenStore:
    def __init__(self, path: pathlib.Path, max_ids: int = 4000) -> None:
        self.path = path
        self.max_ids = max(1, int(max_ids))
        self.ids: list[str] = []
        self._set: set[str] = set()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                raw = list(data.get("ids") or [])[-self.max_ids :]
            except json.JSONDecodeError:
                raw = []
            self.ids = [item for item in raw if isinstance(item, str) and ":" in item]
            self._set = set(self.ids)

    def add(self, agent_key: str, event_id: str) -> bool:
        key = seen_key(agent_key, event_id)
        if not event_id or key in self._set:
            return False
        self.ids.append(key)
        self._set.add(key)
        if len(self.ids) > self.max_ids:
            old = self.ids[: -self.max_ids]
            self.ids = self.ids[-self.max_ids :]
            self._set = set(self.ids)
            del old
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ids": self.ids}), encoding="utf-8")
        tmp.replace(self.path)
        return True

    def has(self, agent_key: str, event_id: str) -> bool:
        return seen_key(agent_key, event_id) in self._set
