from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "listener"))

os.environ.setdefault("LITELLM_URL", "http://127.0.0.1:4000")

from litellm_proxy import _disable_stream  # noqa: E402


class DisableStreamTest(unittest.TestCase):
    def test_forces_stream_false(self):
        raw = json.dumps({"model": "cloud", "stream": True, "messages": []}).encode()
        out = json.loads(_disable_stream(raw, "application/json"))
        self.assertFalse(out["stream"])

    def test_leaves_non_stream_payload(self):
        raw = json.dumps({"model": "cloud", "messages": []}).encode()
        self.assertEqual(_disable_stream(raw, "application/json"), raw)


if __name__ == "__main__":
    unittest.main()
