"""Control API auth: sidecar token vs goose-worker apply."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "listener"))

import agentutil as au  # noqa: E402
import listener  # noqa: E402
from nostrutil import generate_nsec, nsec_to_secret, pubkey_hex  # noqa: E402

OWNER = "b" * 64
STRANGER = "d" * 64
SYNC = "sync-token"
WORKER = "worker-jwt"


class ControlApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.agents = Path(self.tmp.name)
        self._prev_dir = listener.AGENTS_DIR
        self._prev_checker = listener._worker_token_checker
        listener.AGENTS_DIR = self.agents
        listener._worker_token_checker = lambda token: token == WORKER
        listener.ControlHandler.token = SYNC
        nsec, pubkey = generate_nsec()
        au.upsert_agent_files(
            self.agents,
            slug="actor",
            nsec=nsec,
            display="Actor",
            relay=au.DEFAULT_RELAY,
            auth_tag=au.owner_auth_tag(OWNER),
            pubkey=pubkey,
            respond_to="owner-only",
            respond_to_allowlist=[],
            team_id="",
            updated_at="2026-08-25T00:00:00.000Z",
            system_prompt="You are Actor.",
        )
        self.actor_pk = pubkey
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), listener.ControlHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        listener.AGENTS_DIR = self._prev_dir
        listener._worker_token_checker = self._prev_checker
        self.tmp.cleanup()

    def _req(self, method: str, path: str, token: str, body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"raw": raw}
            return exc.code, payload

    def test_worker_token_cannot_get_roster(self):
        code, payload = self._req("GET", "/agents", WORKER)
        self.assertEqual(code, 401)
        self.assertFalse(payload.get("ok"))

    def test_sync_token_can_get_roster(self):
        code, payload = self._req("GET", "/agents", SYNC)
        self.assertEqual(code, 200)
        self.assertTrue(payload.get("ok"))
        self.assertEqual(len(payload.get("agents") or []), 1)

    def test_sync_token_cannot_post_create(self):
        code, payload = self._req(
            "POST",
            "/agents",
            SYNC,
            {
                "author_pubkey": OWNER,
                "actor_slug": "actor",
                "name": "Fizz",
                "system_prompt": "You are Fizz.",
            },
        )
        self.assertEqual(code, 401)
        self.assertFalse(payload.get("ok"))

    def test_worker_create_mints_and_owner_only(self):
        code, payload = self._req(
            "POST",
            "/agents",
            WORKER,
            {
                "author_pubkey": OWNER,
                "actor_slug": "actor",
                "name": "Fizz",
                "system_prompt": "You are Fizz.",
            },
        )
        self.assertEqual(code, 200, payload)
        self.assertTrue(payload.get("ok"))
        pk = payload["pubkey"]
        self.assertEqual(len(pk), 64)
        rec = listener.load_agent_record(pubkey=pk)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["owner"], OWNER)
        self.assertEqual(rec["respond_to"], "owner-only")
        self.assertEqual(au.load_instructions(self.agents, payload["agent_id"]), "You are Fizz.")
        env = au.load_env_file(self.agents / f"{payload['agent_id']}.env")
        derived = pubkey_hex(nsec_to_secret(env["BUZZ_PRIVATE_KEY"]))
        self.assertEqual(derived, pk)

    def test_non_owner_create_forbidden(self):
        code, payload = self._req(
            "POST",
            "/agents",
            WORKER,
            {
                "author_pubkey": STRANGER,
                "actor_slug": "actor",
                "name": "Nope",
                "system_prompt": "nope",
            },
        )
        self.assertEqual(code, 403)
        self.assertIn("owner", str(payload.get("error") or "").lower())

    def test_worker_update_prompt_keeps_nsec(self):
        env_before = au.load_env_file(self.agents / "actor.env")
        nsec_before = env_before["BUZZ_PRIVATE_KEY"]
        code, payload = self._req(
            "PUT",
            f"/agents/{self.actor_pk}",
            WORKER,
            {
                "author_pubkey": OWNER,
                "actor_slug": "actor",
                "name": "Actor",
                "system_prompt": "Updated.",
            },
        )
        self.assertEqual(code, 200, payload)
        self.assertEqual(au.load_instructions(self.agents, "actor"), "Updated.")
        env_after = au.load_env_file(self.agents / "actor.env")
        self.assertEqual(env_after["BUZZ_PRIVATE_KEY"], nsec_before)
        self.assertGreater(env_after.get("BUZZ_UPDATED_AT") or "", "2026-08-25T00:00:00.000Z")

    def test_worker_update_rejects_nsec(self):
        code, payload = self._req(
            "PUT",
            f"/agents/{self.actor_pk}",
            WORKER,
            {
                "author_pubkey": OWNER,
                "actor_slug": "actor",
                "nsec": "nsec1notallowed",
                "system_prompt": "nope",
            },
        )
        self.assertEqual(code, 400)
        self.assertIn("nsec", str(payload.get("error") or "").lower())


class GenerateNsecTest(unittest.TestCase):
    def test_roundtrip(self):
        nsec, pubkey = generate_nsec()
        self.assertTrue(nsec.startswith("nsec1"))
        self.assertEqual(len(pubkey), 64)
        self.assertEqual(pubkey_hex(nsec_to_secret(nsec)), pubkey)


class OwnerAuthTagTest(unittest.TestCase):
    def test_tag(self):
        tag = au.owner_auth_tag(OWNER)
        self.assertEqual(au.owner_from_auth_tags(au.parse_auth_tags(tag)), OWNER)


if __name__ == "__main__":
    unittest.main()
