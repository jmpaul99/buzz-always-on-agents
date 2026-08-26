"""Per-agent seen store: one mention event must still wake every mentioned agent."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "listener"))

from seen import SeenStore, seen_key

FIZZ = "a" * 64
HONEY = "b" * 64
EVENT = "c" * 64


class SeenStoreTests(unittest.TestCase):
    def test_same_event_is_new_for_each_agent(self):
        with tempfile.TemporaryDirectory() as raw:
            store = SeenStore(Path(raw) / "seen.json")
            self.assertFalse(store.has(FIZZ, EVENT))
            self.assertTrue(store.add(FIZZ, EVENT))
            self.assertTrue(store.has(FIZZ, EVENT))
            self.assertFalse(store.has(HONEY, EVENT))
            self.assertTrue(store.add(HONEY, EVENT))
            self.assertTrue(store.has(HONEY, EVENT))
            self.assertFalse(store.add(FIZZ, EVENT))

    def test_own_reply_does_not_block_another_agent(self):
        with tempfile.TemporaryDirectory() as raw:
            store = SeenStore(Path(raw) / "seen.json")
            store.add(FIZZ, EVENT)
            self.assertFalse(store.has(HONEY, EVENT))

    def test_reload_keeps_scoped_keys(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "seen.json"
            store = SeenStore(path)
            store.add(FIZZ, EVENT)
            again = SeenStore(path)
            self.assertTrue(again.has(FIZZ, EVENT))
            self.assertFalse(again.has(HONEY, EVENT))

    def test_ignores_legacy_unscoped_ids(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "seen.json"
            path.write_text(json.dumps({"ids": [EVENT, seen_key(FIZZ, "d" * 64)]}), encoding="utf-8")
            store = SeenStore(path)
            self.assertFalse(store.has(FIZZ, EVENT))
            self.assertFalse(store.has(HONEY, EVENT))
            self.assertTrue(store.has(FIZZ, "d" * 64))

    def test_evicts_oldest(self):
        with tempfile.TemporaryDirectory() as raw:
            store = SeenStore(Path(raw) / "seen.json", max_ids=2)
            store.add(FIZZ, "1")
            store.add(HONEY, "1")
            store.add(FIZZ, "2")
            self.assertFalse(store.has(FIZZ, "1"))
            self.assertTrue(store.has(HONEY, "1"))
            self.assertTrue(store.has(FIZZ, "2"))


if __name__ == "__main__":
    unittest.main()
