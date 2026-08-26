"""Unit tests for agent records, talk-to permissions, and membership."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "listener"))

import agentutil as au

OWNER = "b" * 64
FRIEND = "c" * 64
STRANGER = "d" * 64
AGENT_PK = "a" * 64


def _agent(**kwargs):
    base = {
        "pubkey": AGENT_PK,
        "owner": OWNER,
        "respond_to": "owner-only",
        "respond_to_allowlist": [],
        "channel_allowlist": [],
    }
    base.update(kwargs)
    return base


def _evt(author: str, *, kind: int = 9, mentioned: bool = True, channel: str = "chan-1", content: str = "hi"):
    tags = [["h", channel]]
    if mentioned:
        tags.append(["p", AGENT_PK])
    return {"kind": kind, "pubkey": author, "content": content, "tags": tags, "id": "e1"}


class ShouldHandleTests(unittest.TestCase):
    def test_ignores_own_messages(self):
        self.assertFalse(au.should_handle(_agent(), _evt(AGENT_PK), {"chan-1": "stream"}))

    def test_owner_only_blocks_stranger(self):
        self.assertFalse(au.should_handle(_agent(), _evt(STRANGER), {"chan-1": "stream"}))

    def test_owner_only_allows_owner_mention(self):
        self.assertTrue(au.should_handle(_agent(), _evt(OWNER), {"chan-1": "stream"}))

    def test_allowlist_allows_listed(self):
        agent = _agent(respond_to="allowlist", respond_to_allowlist=[FRIEND])
        self.assertTrue(au.should_handle(agent, _evt(FRIEND), {"chan-1": "stream"}))
        self.assertFalse(au.should_handle(agent, _evt(OWNER), {"chan-1": "stream"}))
        self.assertFalse(au.should_handle(agent, _evt(STRANGER), {"chan-1": "stream"}))

    def test_stream_requires_mention(self):
        self.assertFalse(au.should_handle(_agent(), _evt(OWNER, mentioned=False), {"chan-1": "stream"}))

    def test_dm_does_not_require_mention(self):
        self.assertTrue(au.should_handle(_agent(), _evt(OWNER, mentioned=False), {"chan-1": "dm"}))

    def test_channel_allowlist(self):
        agent = _agent(channel_allowlist=["chan-1"])
        self.assertTrue(au.should_handle(agent, _evt(OWNER, channel="chan-1"), {"chan-1": "stream"}))
        self.assertFalse(au.should_handle(agent, _evt(OWNER, channel="chan-2"), {"chan-2": "stream"}))

    def test_control_commands_ignored(self):
        self.assertFalse(au.should_handle(_agent(), _evt(OWNER, content="!shutdown"), {"chan-1": "stream"}))


class ReactionIndicatorTests(unittest.TestCase):
    def test_reaction_tags_include_event_author_kind_and_channel(self):
        evt = _evt(OWNER, kind=9)
        evt["id"] = "ab" * 32
        tags = au.reaction_tags(evt, "chan-1")
        self.assertEqual(
            tags,
            [["e", "ab" * 32], ["p", OWNER], ["k", "9"], ["h", "chan-1"]],
        )

    def test_delete_tags_skip_empty(self):
        self.assertEqual(au.delete_tags(["r1", "", "r2"]), [["e", "r1"], ["e", "r2"]])

    def test_deletion_tags_are_one_target(self):
        self.assertEqual(au.deletion_tags("abc"), [["e", "abc"], ["k", "7"]])
        self.assertEqual(
            au.deletion_tags("abc", channel="chan-1"),
            [["e", "abc"], ["k", "7"], ["h", "chan-1"]],
        )
        self.assertEqual(au.deletion_tags(""), [])

    def test_typing_tags_use_parent_or_self(self):
        evt = _evt(OWNER)
        evt["id"] = "self-id"
        self.assertEqual(au.typing_tags_for(evt), [["h", "chan-1"], ["e", "self-id"]])
        evt["tags"].insert(0, ["e", "parent-id"])
        self.assertEqual(au.typing_tags_for(evt), [["h", "chan-1"], ["e", "parent-id"]])


class MembershipTests(unittest.TestCase):
    def test_added_then_removed(self):
        channels: dict[str, str] = {}
        subscribed: set[str] = set()
        added = {"tags": [["h", "room-9"]], "kind": au.MEMBER_ADDED_KIND}
        channels, subscribed, close, sub = au.apply_membership_event(
            au.MEMBER_ADDED_KIND, added, channels, subscribed
        )
        self.assertEqual(sub, [("room-9", "stream")])
        subscribed.add("room-9")
        removed = {"tags": [["h", "room-9"]], "kind": au.MEMBER_REMOVED_KIND}
        channels, subscribed, close, sub = au.apply_membership_event(
            au.MEMBER_REMOVED_KIND, removed, channels, subscribed
        )
        self.assertEqual(close, ["room-9"])
        self.assertEqual(sub, [])
        self.assertNotIn("room-9", channels)
        self.assertNotIn("room-9", subscribed)


class RecordTests(unittest.TestCase):
    def test_upsert_find_delete(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            au.upsert_agent_files(
                root,
                slug="fizz",
                nsec="nsec1notarealkeybutlongenough",
                display="Fizz",
                relay=au.DEFAULT_RELAY,
                auth_tag="",
                pubkey=AGENT_PK,
                respond_to="allowlist",
                respond_to_allowlist=[FRIEND],
                team_id="builtin-team:welcome",
                updated_at="2026-08-25T00:00:00.000Z",
                system_prompt="You are Fizz.",
            )
            path = au.find_env_by_pubkey(root, AGENT_PK)
            self.assertIsNotNone(path)
            env = au.load_env_file(path)
            self.assertEqual(env["BUZZ_ACP_RESPOND_TO"], "allowlist")
            self.assertEqual(env["BUZZ_ACP_RESPOND_TO_ALLOWLIST"], FRIEND)
            self.assertEqual(env["BUZZ_TEAM_ID"], "builtin-team:welcome")
            self.assertIn("You are Fizz", au.load_instructions(root, "fizz"))
            au.delete_agent_files(root, "fizz")
            self.assertIsNone(au.find_env_by_pubkey(root, AGENT_PK))

    def test_team_file_write_clear_delete(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            au.upsert_agent_files(
                root,
                slug="fizz",
                nsec="nsec1notarealkeybutlongenough",
                display="Fizz",
                relay=au.DEFAULT_RELAY,
                auth_tag="",
                pubkey=AGENT_PK,
                respond_to="owner-only",
                respond_to_allowlist=[],
                team_id="team-1",
                updated_at="2026-08-25T00:00:00.000Z",
                system_prompt="You are Fizz.",
                team_instructions="Be kind.",
            )
            self.assertEqual(au.load_team_file(root, "fizz"), "Be kind.")
            au.upsert_agent_files(
                root,
                slug="fizz",
                nsec="nsec1notarealkeybutlongenough",
                display="Fizz",
                relay=au.DEFAULT_RELAY,
                auth_tag="",
                pubkey=AGENT_PK,
                respond_to="owner-only",
                respond_to_allowlist=[],
                team_id="team-1",
                updated_at="2026-08-25T00:00:00.000Z",
                system_prompt="You are Fizz.",
                team_instructions="",
            )
            self.assertEqual(au.load_team_file(root, "fizz"), "")
            self.assertFalse((root / "fizz.team").exists())
            au.upsert_agent_files(
                root,
                slug="fizz",
                nsec="nsec1notarealkeybutlongenough",
                display="Fizz",
                relay=au.DEFAULT_RELAY,
                auth_tag="",
                pubkey=AGENT_PK,
                respond_to="owner-only",
                respond_to_allowlist=[],
                team_id="team-1",
                updated_at="2026-08-25T00:00:00.000Z",
                system_prompt="You are Fizz.",
                team_instructions="Be kind.",
            )
            au.delete_agent_files(root, "fizz")
            self.assertFalse((root / "fizz.team").exists())
            self.assertFalse((root / "fizz.env").exists())

    def test_merge_lww(self):
        row = {
            "system_prompt": "old",
            "respond_to": "owner-only",
            "respond_to_allowlist": [],
            "team_id": "",
            "name": "Fizz",
            "updated_at": "2026-08-25T10:00:00.000Z",
        }
        older = dict(row)
        older["system_prompt"] = "cloud-old"
        older["updated_at"] = "2026-08-25T09:00:00.000Z"
        self.assertFalse(au.merge_cloud_into_row(row, older))
        self.assertEqual(row["system_prompt"], "old")
        newer = {
            "system_prompt": "cloud-new",
            "respond_to": "allowlist",
            "respond_to_allowlist": [FRIEND],
            "team_id": "builtin-team:welcome",
            "name": "Fizz",
            "updated_at": "2026-08-25T11:00:00.000Z",
            "channel_allowlist": ["chan-1"],
        }
        self.assertTrue(au.merge_cloud_into_row(row, newer))
        self.assertEqual(row["system_prompt"], "cloud-new")
        self.assertEqual(row["respond_to"], "allowlist")
        self.assertEqual(row["respond_to_allowlist"], [FRIEND])
        self.assertEqual(row["channel_allowlist"], ["chan-1"])

    def test_slug(self):
        self.assertEqual(au.slug_name("Fizz"), "fizz")
        self.assertEqual(au.slug_name("Honey Bee!"), "honey-bee")

    def test_allocate_slug_does_not_clobber(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            au.upsert_agent_files(
                root,
                slug="fizz",
                nsec="nsec1notarealkeybutlongenough",
                display="Fizz",
                relay=au.DEFAULT_RELAY,
                auth_tag="",
                pubkey=AGENT_PK,
                respond_to="owner-only",
                respond_to_allowlist=[],
                team_id="",
                updated_at="2026-08-25T00:00:00.000Z",
                system_prompt="You are Fizz.",
            )
            other = STRANGER
            self.assertEqual(au.allocate_slug(root, "fizz", AGENT_PK), "fizz")
            self.assertEqual(au.allocate_slug(root, "fizz", other), "fizz-dddddddd")

    def test_compact_desktop_records(self):
        old = {
            "name": "Fizz",
            "pubkey": AGENT_PK,
            "created_at": "2026-08-23T00:00:00Z",
            "updated_at": "2026-08-25T18:00:00Z",
            "team_id": "",
        }
        clone = {
            "name": "Fizz",
            "pubkey": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "created_at": "2026-08-25T19:34:00Z",
            "updated_at": "2026-08-25T19:40:00Z",
            "team_id": "builtin-team:welcome",
            "persona_id": "builtin:fizz",
        }
        stub = {"name": "Fizz", "pubkey": "", "is_builtin": False, "slug": "draft"}
        builtin = {"name": "Fizz", "pubkey": "", "is_builtin": True, "slug": "builtin:fizz"}
        kept, dropped = au.compact_desktop_records([stub, builtin, old, clone])
        self.assertEqual(len(kept), 2)
        self.assertTrue(any(r.get("is_builtin") for r in kept))
        keyed = next(r for r in kept if r.get("pubkey") == AGENT_PK)
        self.assertEqual(keyed.get("team_id"), "builtin-team:welcome")
        self.assertEqual(keyed.get("persona_id"), "builtin:fizz")
        self.assertEqual(len(dropped), 2)

    def test_compact_detaches_orphaned_custom_persona(self):
        pid = "95d7487b-3446-4921-a461-c73e5315fd62"
        stub = {
            "name": "Cloud Agent Health",
            "pubkey": "",
            "is_builtin": False,
            "slug": pid,
        }
        instance = {
            "name": "Cloud Agent Health",
            "pubkey": AGENT_PK,
            "persona_id": pid,
            "persona_source_version": "abc",
            "created_at": "2026-08-25T18:40:48Z",
        }
        kept, dropped = au.compact_desktop_records([stub, instance])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].get("pubkey"), AGENT_PK)
        self.assertIsNone(kept[0].get("persona_id"))
        self.assertIsNone(kept[0].get("persona_source_version"))
        self.assertEqual(dropped, [stub])

    def test_public_record_secrets(self):
        agent = {
            "pubkey": AGENT_PK,
            "display": "Fizz",
            "name": "fizz",
            "nsec": "nsec1notarealkeybutlongenough",
            "auth_tag_raw": "[]",
            "respond_to": "owner-only",
            "respond_to_allowlist": [],
            "team_id": "",
            "relay": "wss://example",
            "channel_allowlist": [],
        }
        public = au.public_record(agent, "You are Fizz.")
        self.assertNotIn("nsec", public)
        self.assertNotIn("auth_tag", public)
        self.assertEqual(public.get("team_instructions"), "")
        agent["team_instructions"] = "Be kind."
        self.assertEqual(au.public_record(agent, "You are Fizz.")["team_instructions"], "Be kind.")
        secret = au.public_record(agent, "You are Fizz.", include_secrets=True)
        self.assertEqual(secret["nsec"], "nsec1notarealkeybutlongenough")
        self.assertEqual(secret["auth_tag"], "[]")
        self.assertEqual(secret["system_prompt"], "You are Fizz.")

    def test_desktop_row_from_cloud(self):
        cloud = {
            "pubkey": AGENT_PK,
            "name": "Fizz",
            "nsec": "nsec1notarealkeybutlongenough",
            "system_prompt": "You are Fizz.",
            "respond_to": "allowlist",
            "respond_to_allowlist": [FRIEND],
            "team_id": "t1",
            "relay_url": "wss://example",
            "channel_allowlist": ["chan-1"],
            "updated_at": "2026-08-25T12:00:00.000Z",
            "slug": "fizz",
        }
        row = au.desktop_row_from_cloud(cloud)
        self.assertEqual(row["pubkey"], AGENT_PK)
        self.assertEqual(row["name"], "Fizz")
        self.assertFalse(row["is_active"])
        self.assertNotIn("nsec", row)
        self.assertIn("id", row)
        self.assertEqual(row["channel_allowlist"], ["chan-1"])
        self.assertEqual(row["backend_agent_id"], "fizz")

    def test_apply_cloud_roster_import_update_delete_keeps_draft(self):
        existing = {
            "name": "Fizz",
            "pubkey": AGENT_PK,
            "system_prompt": "old",
            "updated_at": "2026-08-25T10:00:00.000Z",
            "respond_to": "owner-only",
            "respond_to_allowlist": [],
            "channel_allowlist": [],
        }
        gone = {
            "name": "Gone",
            "pubkey": FRIEND,
            "updated_at": "2026-08-25T09:00:00.000Z",
        }
        draft = {
            "name": "New",
            "pubkey": STRANGER,
            "system_prompt": "draft",
            "updated_at": "2026-08-25T10:00:00.000Z",
        }
        tracked = {
            AGENT_PK: {"fingerprint": "x", "slug": "fizz", "updated_at": "t"},
            FRIEND: {"fingerprint": "y", "slug": "gone", "updated_at": "t"},
        }
        new_pk = "e" * 64
        cloud = [
            {
                "pubkey": AGENT_PK,
                "name": "Fizz",
                "nsec": "nsec1aaa",
                "system_prompt": "new",
                "updated_at": "2026-08-25T11:00:00.000Z",
                "respond_to": "allowlist",
                "respond_to_allowlist": [FRIEND],
                "channel_allowlist": ["c1"],
                "slug": "fizz",
            },
            {
                "pubkey": new_pk,
                "name": "Imported",
                "nsec": "nsec1bbb",
                "system_prompt": "hi",
                "updated_at": "2026-08-25T11:00:00.000Z",
                "slug": "imported",
                "respond_to": "everyone",
            },
        ]
        out, tracked2, imported, removed, updated = au.apply_cloud_roster(
            [existing, gone, draft], cloud, tracked
        )
        self.assertEqual(removed, [FRIEND])
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0]["pubkey"], new_pk)
        self.assertEqual(imported[0]["nsec"], "nsec1bbb")
        self.assertTrue(any(r.get("pubkey") == STRANGER for r in out))
        self.assertFalse(any(r.get("pubkey") == FRIEND for r in out))
        fizz = next(r for r in out if r.get("pubkey") == AGENT_PK)
        self.assertEqual(fizz["system_prompt"], "new")
        self.assertEqual(fizz["channel_allowlist"], ["c1"])
        self.assertIn(new_pk, tracked2)
        self.assertNotIn(FRIEND, tracked2)
        self.assertEqual(updated, [AGENT_PK])
        imported_row = next(r for r in out if r.get("pubkey") == new_pk)
        self.assertFalse(imported_row.get("is_active"))
        self.assertEqual(tracked2[new_pk]["fingerprint"], au.settings_fingerprint(imported_row))

    def test_apply_cloud_roster_skips_import_without_nsec(self):
        records, tracked, imported, removed, updated = au.apply_cloud_roster(
            [],
            [{"pubkey": AGENT_PK, "name": "Fizz", "updated_at": "2026-08-25T11:00:00.000Z"}],
            {},
        )
        self.assertEqual(records, [])
        self.assertEqual(imported, [])
        self.assertEqual(removed, [])
        self.assertEqual(updated, [])
        self.assertEqual(tracked, {})

    def test_user_can_access_agent(self):
        owner_only = {"respond_to": "owner-only", "owner": OWNER, "respond_to_allowlist": []}
        allow = {"respond_to": "allowlist", "respond_to_allowlist": [FRIEND]}
        everyone = {"respond_to": "everyone"}
        self.assertTrue(au.user_can_access_agent(everyone, set()))
        self.assertFalse(au.user_can_access_agent(owner_only, set()))
        self.assertTrue(au.user_can_access_agent(owner_only, {OWNER}))
        self.assertFalse(au.user_can_access_agent(owner_only, {FRIEND}))
        self.assertTrue(au.user_can_access_agent(allow, {FRIEND}))
        self.assertFalse(au.user_can_access_agent(allow, {OWNER}))
        tagged = {"respond_to": "owner-only", "auth_tag": json.dumps(["auth", OWNER, "", "sig"])}
        self.assertTrue(au.user_can_access_agent(tagged, {OWNER}))

    def test_desktop_user_pubkeys_from_auth_tag(self):
        row = {"auth_tag": json.dumps(["auth", OWNER, "", "sig"]), "pubkey": AGENT_PK}
        self.assertEqual(au.desktop_user_pubkeys([row]), {OWNER})

    def test_apply_cloud_roster_skips_inaccessible_import(self):
        other = "f" * 64
        cloud = [
            {
                "pubkey": other,
                "name": "Secret",
                "nsec": "nsec1ccc",
                "respond_to": "owner-only",
                "owner": STRANGER,
                "updated_at": "2026-08-25T11:00:00.000Z",
                "slug": "secret",
            },
            {
                "pubkey": AGENT_PK,
                "name": "Mine",
                "nsec": "nsec1ddd",
                "respond_to": "owner-only",
                "owner": OWNER,
                "updated_at": "2026-08-25T11:00:00.000Z",
                "slug": "mine",
            },
        ]
        out, tracked, imported, removed, updated = au.apply_cloud_roster(
            [], cloud, {}, user_pubkeys={OWNER}
        )
        self.assertEqual(removed, [])
        self.assertEqual([item["pubkey"] for item in imported], [AGENT_PK])
        self.assertTrue(any(r.get("pubkey") == AGENT_PK for r in out))
        self.assertFalse(any(r.get("pubkey") == other for r in out))
        self.assertNotIn(other, tracked)

    def test_apply_cloud_roster_keeps_inaccessible_cloud_from_vanishing(self):
        local = {"name": "Secret", "pubkey": AGENT_PK, "updated_at": "2026-08-25T10:00:00.000Z"}
        tracked = {AGENT_PK: {"fingerprint": "x", "slug": "secret", "updated_at": "t"}}
        cloud = [
            {
                "pubkey": AGENT_PK,
                "name": "Secret",
                "nsec": "nsec1eee",
                "respond_to": "owner-only",
                "owner": STRANGER,
                "updated_at": "2026-08-25T09:00:00.000Z",
            }
        ]
        out, tracked2, imported, removed, updated = au.apply_cloud_roster(
            [local], cloud, tracked, user_pubkeys={OWNER}
        )
        self.assertEqual(removed, [])
        self.assertEqual(imported, [])
        self.assertTrue(any(r.get("pubkey") == AGENT_PK for r in out))
        self.assertIn(AGENT_PK, tracked2)


class GoosePromptTests(unittest.TestCase):
    def setUp(self):
        self.prompt = au.build_goose_prompt(
            identity="You are Fizz.",
            channel="chan-1",
            author=OWNER,
            event_id="e1",
            content="Write a poem and list my repos.",
            send_cmd="buzz messages send --channel chan-1 --content '...'",
        )

    def test_drops_extension_budget_essay(self):
        low = self.prompt.lower()
        self.assertNotIn("5 enabled extensions", low)
        self.assertNotIn("50 tools", low)
        self.assertNotIn("github__search_repositories", low)
        self.assertNotIn("list_repositories", low)

    def test_allows_reaction_then_stop(self):
        low = self.prompt.lower()
        self.assertIn("buzz reactions", low)
        self.assertIn("event id", low)
        self.assertIn("every part of a multi-ask", low)
        self.assertIn("text-only answer is not delivered", low)
        self.assertNotIn("after a successful send, stop", low)


class TeamInstructionsTest(unittest.TestCase):
    def test_lookup_and_fingerprint(self):
        teams = [{"id": "team-1", "instructions": "Be kind.", "updated_at": "2026-08-25T10:00:00.000Z"}]
        self.assertEqual(au.team_instructions_from_records(teams, "team-1"), "Be kind.")
        self.assertEqual(au.team_instructions_from_records(teams, "missing"), "")
        row = {"name": "Fizz", "system_prompt": "hi", "team_id": "team-1", "team_instructions": "Be kind."}
        other = dict(row)
        other["team_instructions"] = "Be nicer."
        self.assertNotEqual(au.settings_fingerprint(row), au.settings_fingerprint(other))

    def test_merge_fills_empty_and_respects_newer_local(self):
        teams = [
            {"id": "team-1", "instructions": "", "updated_at": "2026-08-25T12:00:00.000Z"},
            {"id": "team-2", "instructions": "Local edit.", "updated_at": "2026-08-25T12:00:00.000Z"},
        ]
        cloud = [
            {
                "team_id": "team-1",
                "team_instructions": "From cloud.",
                "updated_at": "2026-08-25T11:00:00.000Z",
            },
            {
                "team_id": "team-2",
                "team_instructions": "Cloud older.",
                "updated_at": "2026-08-25T10:00:00.000Z",
            },
            {
                "team_id": "nope",
                "team_instructions": "Do not create me.",
                "updated_at": "2026-08-25T13:00:00.000Z",
            },
        ]
        self.assertTrue(au.apply_cloud_team_instructions(teams, cloud))
        self.assertEqual(teams[0]["instructions"], "From cloud.")
        self.assertEqual(teams[1]["instructions"], "Local edit.")
        self.assertEqual(len(teams), 2)


class CloudRuntimeTest(unittest.TestCase):
    def test_overwrites_harness_and_keeps_stopped(self):
        row = {
            "agent_command": "goose.exe",
            "agent_command_override": "x",
            "agent_args": ["--foo"],
            "acp_command": "buzz-acp",
            "mcp_command": "mcp",
            "model": "gpt-4",
            "provider": "openai",
            "is_active": True,
        }
        au.apply_cloud_runtime(row, "fizz", {"type": "provider", "id": "cloud", "config": {}})
        self.assertEqual(row["backend"]["type"], "provider")
        self.assertEqual(row["backend_agent_id"], "fizz")
        self.assertEqual(row["agent_command"], "")
        self.assertEqual(row["acp_command"], "")
        self.assertEqual(row["mcp_command"], "")
        self.assertEqual(row["agent_args"], [])
        self.assertEqual(row["model"], "goose")
        self.assertEqual(row["provider"], "litellm")
        self.assertFalse(row["is_active"])


if __name__ == "__main__":
    unittest.main()
