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
        proposed = ca.propose(env, name="Fizz", system_prompt="You are Fizz.", create=True)
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
        env = _env(BUZZ_WORKSPACE=self.ws)
        ca.propose(env, name="Actor", system_prompt="Updated.", pubkey=AGENT)
        env["BUZZ_MESSAGE"] = "confirm"
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
            ca.propose(env, name="Fizz", system_prompt="nope", create=True)
        self.assertIn("owner", str(caught.exception).lower())

    def test_cancel_drops_pending(self):
        env = _env(BUZZ_WORKSPACE=self.ws)
        ca.propose(env, name="Fizz", system_prompt="You are Fizz.", create=True)
        self.assertTrue(ca.cancel(env)["cancelled"])
        self.assertIsNone(ca.load_pending(env))

    def test_cli_propose(self):
        env = _env(BUZZ_WORKSPACE=self.ws)
        code = ca.main(
            ["propose", "--create", "--name", "Fizz", "--instructions", "You are Fizz."],
            env=env,
        )
        self.assertEqual(code, 0)
        self.assertEqual(ca.load_pending(env)["name"], "Fizz")

    def test_cli_propose_update_requires_pubkey(self):
        env = _env(BUZZ_WORKSPACE=self.ws)
        code = ca.main(
            ["propose", "--name", "Fizz", "--instructions", "You are Fizz."],
            env=env,
        )
        self.assertEqual(code, 2)
        self.assertIsNone(ca.load_pending(env))

    def test_cli_propose_update(self):
        env = _env(BUZZ_WORKSPACE=self.ws)
        code = ca.main(
            [
                "propose",
                "--pubkey",
                AGENT,
                "--name",
                "Fizz",
                "--instructions",
                "You are Fizz.",
            ],
            env=env,
        )
        self.assertEqual(code, 0)
        self.assertEqual(ca.load_pending(env)["op"], "update")
        self.assertEqual(ca.load_pending(env)["pubkey"], AGENT)

    def test_http_error(self):
        env = _env(BUZZ_WORKSPACE=self.ws)
        ca.propose(env, name="Fizz", system_prompt="You are Fizz.", create=True)
        env["BUZZ_MESSAGE"] = "confirm"

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

    def test_propose_update_requires_pubkey(self):
        env = _env(BUZZ_WORKSPACE=self.ws)
        with self.assertRaises(SystemExit) as caught:
            ca.propose(env, name="cloud-health", system_prompt="TEST")
        self.assertIn("--pubkey", str(caught.exception))
        self.assertIsNone(ca.load_pending(env))

    def test_propose_create_rejects_pubkey(self):
        env = _env(BUZZ_WORKSPACE=self.ws)
        with self.assertRaises(SystemExit) as caught:
            ca.propose(env, name="Fizz", system_prompt="You are Fizz.", pubkey=AGENT, create=True)
        self.assertIn("not both", str(caught.exception))

    def test_propose_update_stores_pubkey(self):
        env = _env(BUZZ_WORKSPACE=self.ws)
        proposed = ca.propose(
            env,
            name="Cloud Agent Health",
            system_prompt="TEST AGENT INSTRUCTION CHANGE",
            pubkey=AGENT,
        )
        self.assertEqual(proposed["op"], "update")
        self.assertEqual(proposed["pubkey"], AGENT)
        self.assertEqual(ca.load_pending(env)["pubkey"], AGENT)

    def test_propose_on_confirm_applies_stored(self):
        env = _env(BUZZ_WORKSPACE=self.ws)
        ca.propose(env, name="Fizz", system_prompt="You are Fizz.", create=True)
        env["BUZZ_MESSAGE"] = "confirm"
        bodies = []

        def request(req, timeout=0):
            bodies.append(json.loads(req.data.decode()))
            return FakeResp(json.dumps({"ok": True, "agent_id": "fizz", "pubkey": AGENT}))

        result = ca.propose(
            env,
            name="Nope",
            system_prompt="GARBAGE",
            create=True,
            request=request,
            token="jwt",
        )
        self.assertEqual(result["agent_id"], "fizz")
        self.assertEqual(bodies[0]["system_prompt"], "You are Fizz.")
        self.assertIsNone(ca.load_pending(env))

    def test_propose_on_confirm_without_pending(self):
        env = _env(BUZZ_WORKSPACE=self.ws, BUZZ_MESSAGE="confirm")
        with self.assertRaises(SystemExit) as caught:
            ca.propose(env, name="Fizz", system_prompt="GARBAGE", create=True)
        self.assertIn("no pending", str(caught.exception))

    def test_propose_on_cancel_refused(self):
        env = _env(BUZZ_WORKSPACE=self.ws)
        ca.propose(env, name="Fizz", system_prompt="You are Fizz.", create=True)
        env["BUZZ_MESSAGE"] = "cancel"
        with self.assertRaises(SystemExit) as caught:
            ca.propose(env, name="Nope", system_prompt="GARBAGE", create=True)
        self.assertIn("cancel", str(caught.exception).lower())
        self.assertEqual(ca.load_pending(env)["system_prompt"], "You are Fizz.")


if __name__ == "__main__":
    unittest.main()
