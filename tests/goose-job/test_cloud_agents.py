from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from io import BytesIO
from email.message import EmailMessage
from urllib.error import HTTPError

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "goose-job"))

import cloud_agents as ca  # noqa: E402

OWNER = "b" * 64
AGENT = "a" * 64
STRANGER = "d" * 64


def _env(**kwargs) -> dict[str, str]:
    base = {
        "AGENT_NAME": "actor",
        "BUZZ_AUTHOR_PUBKEY": OWNER,
        "BUZZ_OWNER_PUBKEY": OWNER,
        "BUZZ_MESSAGE": "please update fizz",
        "LISTENER_CONTROL_URL": "http://10.0.0.2:8743",
    }
    base.update(kwargs)
    return base


class FakeResp:
    def __init__(self, body: str, status: int = 200):
        self._body = body.encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False


class CloudAgentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = str(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_confirm_ignores_mentions(self):
        self.assertTrue(ca.is_confirm("@actor confirm"))
        self.assertTrue(ca.is_confirm("confirm"))
        self.assertFalse(ca.is_confirm("please confirm this edit"))
        self.assertTrue(ca.is_cancel("@actor cancel"))

    def test_propose_apply_cancel(self):
        env = _env(BUZZ_WORKSPACE=self.ws)
        proposed = ca.propose(env, name="Fizz", system_prompt="You are Fizz.")
        self.assertEqual(proposed["op"], "create")
        pending = ca.load_pending(env)
        self.assertEqual(pending["system_prompt"], "You are Fizz.")

        with self.assertRaises(SystemExit) as caught:
            ca.apply(env, token="jwt")
        self.assertIn("confirm", str(caught.exception))

        env["BUZZ_MESSAGE"] = "confirm"
        calls = []

        def request(req, timeout=0):
            calls.append((req.get_method(), req.full_url, req.data))
            return FakeResp(json.dumps({"ok": True, "agent_id": "fizz", "pubkey": AGENT}))

        result = ca.apply(env, request=request, token="jwt")
        self.assertEqual(result["agent_id"], "fizz")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith("/agents"))
        self.assertIsNone(ca.load_pending(env))

    def test_apply_update_uses_put(self):
        env = _env(BUZZ_WORKSPACE=self.ws, BUZZ_MESSAGE="confirm")
        ca.propose(env, name="Actor", system_prompt="Updated.", pubkey=AGENT)
        calls = []

        def request(req, timeout=0):
            calls.append(req.get_method() + " " + req.full_url)
            return FakeResp(json.dumps({"ok": True, "agent_id": "actor", "pubkey": AGENT}))

        ca.apply(env, request=request, token="jwt")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("PUT "))
        self.assertTrue(calls[0].endswith(f"/agents/{AGENT}"))

    def test_non_owner_refused(self):
        env = _env(BUZZ_WORKSPACE=self.ws, BUZZ_AUTHOR_PUBKEY=STRANGER)
        with self.assertRaises(SystemExit) as caught:
            ca.propose(env, name="Fizz", system_prompt="nope")
        self.assertIn("owner", str(caught.exception).lower())

    def test_cancel_drops_pending(self):
        env = _env(BUZZ_WORKSPACE=self.ws)
        ca.propose(env, name="Fizz", system_prompt="You are Fizz.")
        self.assertTrue(ca.cancel(env)["cancelled"])
        self.assertIsNone(ca.load_pending(env))

    def test_cli_propose(self):
        env = _env(BUZZ_WORKSPACE=self.ws)
        code = ca.main(
            ["propose", "--name", "Fizz", "--instructions", "You are Fizz."],
            env=env,
        )
        self.assertEqual(code, 0)
        self.assertEqual(ca.load_pending(env)["name"], "Fizz")

    def test_http_error(self):
        env = _env(BUZZ_WORKSPACE=self.ws, BUZZ_MESSAGE="confirm")
        ca.propose(env, name="Fizz", system_prompt="You are Fizz.")

        def request(req, timeout=0):
            raise HTTPError(
                req.full_url,
                403,
                "forbidden",
                EmailMessage(),
                BytesIO(b'{"ok":false,"error":"no"}'),
            )

        with self.assertRaises(SystemExit):
            ca.apply(env, request=request, token="jwt")


if __name__ == "__main__":
    unittest.main()
