from __future__ import annotations

import inspect
import queue
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "listener"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "goose-job"))

from nostrutil import nsec_to_secret, pubkey_hex
from observer import (
    ObserverPublisher,
    ObserverSocket,
    _FlushWait,
    _pool_lock,
    _sockets,
    reset_observer_sockets,
    socket_creds,
    warm_observer,
)
from worker import Handler, _run_goose

SECRET = "11" * 32
OWNER = pubkey_hex(nsec_to_secret("22" * 32))


def _env(**extra: str) -> dict[str, str]:
    data = {
        "BUZZ_PRIVATE_KEY": SECRET,
        "BUZZ_OWNER_PUBKEY": OWNER,
        "BUZZ_RELAY_URL": "wss://example.invalid",
        "BUZZ_CHANNEL_ID": "ch",
        "BUZZ_MESSAGE": "hi",
    }
    data.update(extra)
    return data


class SocketCredsTest(unittest.TestCase):
    def test_https_relay_becomes_wss(self):
        creds = socket_creds(_env(BUZZ_RELAY_URL="https://example.invalid"))
        self.assertIsNotNone(creds)
        assert creds is not None
        self.assertEqual(creds.relay, "wss://example.invalid")
        self.assertEqual(creds.owner, OWNER)
        self.assertTrue(creds.key.startswith(creds.agent_pub))

    def test_invalid_key(self):
        self.assertIsNone(socket_creds(_env(BUZZ_PRIVATE_KEY="not-a-key")))

    def test_missing_owner(self):
        env = _env()
        env.pop("BUZZ_OWNER_PUBKEY")
        self.assertIsNone(socket_creds(env))


class SharedSocketTest(unittest.TestCase):
    def tearDown(self) -> None:
        reset_observer_sockets()

    def test_turns_reuse_one_socket(self):
        creds = socket_creds(_env())
        self.assertIsNotNone(creds)
        assert creds is not None
        sock = ObserverSocket(creds, start=False)
        with _pool_lock:
            _sockets[creds.key] = sock
        first = ObserverPublisher(_env())
        second = ObserverPublisher(_env())
        self.assertTrue(first.enabled)
        self.assertIs(first._sock, sock)
        self.assertIs(second._sock, sock)

    def test_finish_does_not_halt_socket(self):
        creds = socket_creds(_env())
        assert creds is not None
        sock = ObserverSocket(creds, start=False)
        with _pool_lock:
            _sockets[creds.key] = sock
        pub = ObserverPublisher(_env())
        waiter = pub.finish()
        self.assertIsNotNone(waiter)
        self.assertFalse(sock.dead)
        self.assertFalse(sock._halt.is_set())
        items: list = []
        while True:
            try:
                items.append(sock._out.get_nowait())
            except queue.Empty:
                break
        self.assertTrue(any(isinstance(item, dict) and item.get("kind") == "turn_completed" for item in items))
        self.assertTrue(any(isinstance(item, _FlushWait) for item in items))
        self.assertFalse(pub.enabled)

    def test_warm_reuses_live_socket(self):
        creds = socket_creds(_env())
        assert creds is not None
        sock = ObserverSocket(creds, start=False)
        sock._ready.set()
        sock._last_ok = time.monotonic()
        with _pool_lock:
            _sockets[creds.key] = sock
        warm_observer(_env())
        self.assertIs(_sockets[creds.key], sock)

    def test_stale_ready_socket_is_replaced(self):
        creds = socket_creds(_env())
        assert creds is not None
        sock = ObserverSocket(creds, start=False)
        sock._ready.set()
        sock._last_ok = 1.0
        with _pool_lock:
            _sockets[creds.key] = sock
        self.assertTrue(sock.stale())


class WorkerObserverTest(unittest.TestCase):
    def test_goose_waits_after_home_sync(self):
        src = inspect.getsource(_run_goose)
        self.assertIn("wait_ready", src)
        self.assertLess(src.find("ObserverPublisher"), src.find("_agent_home"))
        self.assertLess(src.find("_agent_home"), src.find("wait_ready"))
        self.assertLess(src.find("wait_ready"), src.find("_spawn_goose"))
        self.assertIn("GOOSE_IDLE_TIMEOUT", src)
        self.assertNotIn("REPLY_IDLE_SECS", src)
        self.assertNotIn("reply_stop_decision", src)
        self.assertNotIn("second_send", src)

    def test_run_warms_observer_before_queue(self):
        src = inspect.getsource(Handler.do_POST)
        self.assertIn("warm_observer", src)
        self.assertLess(src.find("warm_observer"), src.find("_submit"))


if __name__ == "__main__":
    unittest.main()
