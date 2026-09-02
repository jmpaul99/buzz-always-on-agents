"""Unit tests for Google Sheets A1 / hex helpers (no live Google calls)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_STUBS = (
    "mcp",
    "mcp.server",
    "google",
    "google.auth",
    "google.auth.exceptions",
    "google.auth.transport",
    "google.auth.transport.requests",
    "googleapiclient",
    "googleapiclient.discovery",
    "googleapiclient.errors",
    "googleapiclient.http",
)
_injected = [name for name in _STUBS if name not in sys.modules]
for _name in _injected:
    sys.modules[_name] = MagicMock()

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "listener" / "local-mcp"))
try:
    import google_adc_mcp as g  # noqa: E402
finally:
    for _name in _injected:
        sys.modules.pop(_name, None)


class A1Tests(unittest.TestCase):
    def test_col_letters(self):
        self.assertEqual(g._col_letters_to_index("A"), 0)
        self.assertEqual(g._col_letters_to_index("Z"), 25)
        self.assertEqual(g._col_letters_to_index("AA"), 26)
        self.assertEqual(g._col_letters_to_index("AB"), 27)

    def test_single_cell(self):
        self.assertEqual(g._parse_a1_range("A1"), (None, 0, 1, 0, 1))

    def test_sheet_and_range(self):
        self.assertEqual(g._parse_a1_range("Sheet1!A1:B2"), ("Sheet1", 0, 2, 0, 2))

    def test_quoted_sheet_and_wide_columns(self):
        self.assertEqual(
            g._parse_a1_range("'My Sheet'!AA10:AB12"),
            ("My Sheet", 9, 12, 26, 28),
        )

    def test_escaped_quotes_in_sheet_name(self):
        self.assertEqual(g._parse_a1_range("'It''s a sheet'!C3"), ("It's a sheet", 2, 3, 2, 3))

    def test_inverted_range(self):
        with self.assertRaises(ValueError):
            g._parse_a1_range("B2:A1")


class HexAndSheetTests(unittest.TestCase):
    def test_hex_full_and_short(self):
        self.assertEqual(
            g._hex_to_rgb("#FF0000"),
            {"red": 1.0, "green": 0.0, "blue": 0.0},
        )
        self.assertEqual(g._hex_to_rgb("#fff"), {"red": 1.0, "green": 1.0, "blue": 1.0})

    def test_hex_invalid(self):
        with self.assertRaises(ValueError):
            g._hex_to_rgb("#12")

    def test_resolve_sheet_id(self):
        sheets = [
            {"properties": {"sheetId": 0, "title": "Sheet1"}},
            {"properties": {"sheetId": 42, "title": "Budget"}},
        ]
        self.assertEqual(g._resolve_sheet_id(sheets, None), 0)
        self.assertEqual(g._resolve_sheet_id(sheets, "Budget"), 42)
        with self.assertRaises(ValueError):
            g._resolve_sheet_id(sheets, "Missing")

    def test_json_list(self):
        self.assertEqual(g._json_list("[1, 2]", "ranges_json"), [1, 2])
        with self.assertRaises(ValueError):
            g._json_list("{}", "ranges_json")


class GmailDriveCalendarHelperTests(unittest.TestCase):
    def test_split_ids(self):
        self.assertEqual(g._split_ids("INBOX, Label_1;STARRED"), ["INBOX", "Label_1", "STARRED"])

    def test_gmail_plain_prefers_text(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": g.base64.urlsafe_b64encode(b"hello").decode().rstrip("=")},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": g.base64.urlsafe_b64encode(b"<p>nope</p>").decode().rstrip("=")},
                },
            ],
        }
        self.assertEqual(g._gmail_plain(payload).strip(), "hello")

    def test_rfc3339_and_gaps(self):
        start = g._parse_rfc3339("2026-08-26T09:00:00Z")
        end = g._parse_rfc3339("2026-08-26T12:00:00Z")
        busy = [
            (g._parse_rfc3339("2026-08-26T09:00:00Z"), g._parse_rfc3339("2026-08-26T10:00:00Z")),
        ]
        slots = g._suggest_gaps(start, end, g.timedelta(minutes=30), busy, 2)
        self.assertEqual(slots[0]["start"], "2026-08-26T10:00:00+00:00")
        self.assertEqual(g._event_time("2026-08-26", ""), {"date": "2026-08-26"})

    def test_bytes_as_text(self):
        text, binary = g._bytes_as_text(b"ok")
        self.assertEqual(text, "ok")
        self.assertFalse(binary)
        encoded, binary = g._bytes_as_text(b"\xff\xfe")
        self.assertTrue(binary)
        self.assertEqual(encoded, g.base64.b64encode(b"\xff\xfe").decode("ascii"))


class SuiteTests(unittest.TestCase):
    def test_apply_suite_keeps_one_prefix(self):
        server = type("S", (), {})()
        server._tools = {
            "gmail_list_labels": object(),
            "drive_search": object(),
            "calendar_list_calendars": object(),
            "sheets_get_values": object(),
            "google_whoami": object(),
        }
        removed = g.apply_suite("gmail", server)
        self.assertEqual(set(server._tools), {"gmail_list_labels", "google_whoami"})
        self.assertEqual(removed, 3)
        with self.assertRaises(ValueError):
            g.apply_suite("all", server)


if __name__ == "__main__":
    unittest.main()
