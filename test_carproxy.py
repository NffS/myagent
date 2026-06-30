#!/usr/bin/env python3
"""Tests for carproxy.py's reply-template rendering. Stdlib only:

    python test_carproxy.py
"""

import unittest

from carproxy import render_reply


# A 50-byte AgentMS3 login frame with the LE seq counter (2d 18) at bytes 12-13.
LOGIN = b"\x40\x00\x32\x00\x1c\x46\x00\x00\x00\x00\x00\x00" + b"\x2d\x18" + b"\x00" * 34
SHORT = b"\x40\x00\x32\x00\x1c\x46\x00\x00\x00\x00"  # 10 bytes: too short for seq


class RenderReplyTest(unittest.TestCase):
    def test_hex_template_appends_raw_seq_bytes(self):
        # The strongest ACK candidate: hex "40000300 46" + data[12:14].
        out = render_reply("40000300 46 {seq}", True, LOGIN)
        self.assertEqual(out, b"\x40\x00\x03\x00\x46\x2d\x18")

    def test_seqhex_token_in_hex_template(self):
        # {seqhex} -> "2d18" -> decoded back to the same two bytes in a hex template.
        out = render_reply("4046{seqhex}", True, LOGIN)
        self.assertEqual(out, b"\x40\x46\x2d\x18")

    def test_seq_token_in_text_template_injects_raw_bytes(self):
        out = render_reply("X{seq}Y", False, LOGIN)
        self.assertEqual(out, b"X\x2d\x18Y")

    def test_seqhex_token_in_text_template_injects_ascii_hex(self):
        out = render_reply("SEQ={seqhex}", False, LOGIN)
        self.assertEqual(out, b"SEQ=2d18")

    def test_template_without_tokens_is_static(self):
        self.assertEqual(render_reply("4000", True, b""), b"\x40\x00")

    def test_short_frame_returns_none_when_seq_needed(self):
        self.assertIsNone(render_reply("46{seq}", True, SHORT))
        self.assertIsNone(render_reply("{seqhex}", True, SHORT))

    def test_short_frame_ok_when_no_token(self):
        self.assertEqual(render_reply("4046", True, SHORT), b"\x40\x46")

    def test_invalid_hex_raises(self):
        with self.assertRaises(ValueError):
            render_reply("zz{seq}", True, LOGIN)


if __name__ == "__main__":
    unittest.main()
