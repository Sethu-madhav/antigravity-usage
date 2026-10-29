"""Tests for protobuf and varint decoding."""

import unittest

from agy_usage.parser import decode_varint, parse_proto_fields, parse_usage_proto


class TestProtobufDecoding(unittest.TestCase):
    def test_varint_decoding(self):
        # 1-byte varint: 0 -> 0, 1 -> 1, 127 -> 127
        val, idx = decode_varint(b"\x00", 0)
        self.assertEqual(val, 0)
        self.assertEqual(idx, 1)

        val, idx = decode_varint(b"\x01", 0)
        self.assertEqual(val, 1)

        # 2-byte varint: 300 = 0xAC 0x02
        val, idx = decode_varint(b"\xac\x02", 0)
        self.assertEqual(val, 300)
        self.assertEqual(idx, 2)

    def test_proto_fields_parsing(self):
        # field 1: varint 42 (tag = (1 << 3) | 0 = 8 = 0x08)
        # field 2: string 'test' (tag = (2 << 3) | 2 = 18 = 0x12, len=4)
        raw = bytes([0x08, 42, 0x12, 4]) + b"test"
        fields = parse_proto_fields(raw)
        self.assertEqual(len(fields), 2)
        self.assertEqual(fields[0], (1, 0, 42))
        self.assertEqual(fields[1], (2, 2, b"test"))

    def test_usage_proto_parsing(self):
        # Construct synthetic UsageMetadata:
        # field 2 (prompt): 1000 -> 0x10, 0xe8, 0x07
        # field 3 (output): 200 -> 0x18, 0xc8, 0x01
        # field 5 (cached): 500 -> 0x28, 0xf4, 0x03
        raw = bytes([0x10, 0xE8, 0x07, 0x18, 0xC8, 0x01, 0x28, 0xF4, 0x03])
        usage = parse_usage_proto(raw)
        self.assertEqual(usage.prompt_tokens, 1000)
        self.assertEqual(usage.output_tokens, 200)
        self.assertEqual(usage.cached_tokens, 500)
        self.assertEqual(usage.total_tokens, 1700)
        self.assertAlmostEqual(usage.cache_hit_rate, 33.33, places=1)


if __name__ == "__main__":
    unittest.main()
