"""Tests for varint size limits."""

import unittest

from agy_usage.parser import decode_varint, parse_proto_fields, parse_usage_proto


class TestVarintBounds(unittest.TestCase):
    """Regression tests for BUG-017: a varint is at most 10 bytes / 64 bits;
    longer runs are corruption and must not decode into astronomically large
    ints that flow into the aggregates."""

    MAX_U64 = (1 << 64) - 1

    def test_ten_byte_varint_still_decodes_to_max_u64(self):
        data = b"\xff" * 9 + b"\x01"
        val, idx = decode_varint(data, 0)
        self.assertEqual(val, self.MAX_U64)
        self.assertEqual(idx, 10)

    def test_tenth_byte_overflow_bits_are_masked_to_64_bits(self):
        val, idx = decode_varint(b"\xff" * 9 + b"\x7f", 0)
        self.assertEqual(val, self.MAX_U64)
        self.assertEqual(idx, 10)

    def test_overlong_varint_is_rejected(self):
        # 10th byte still carries a continuation bit: an 11th byte would be needed.
        with self.assertRaises(ValueError):
            decode_varint(b"\xff" * 10 + b"\x01", 0)
        # Far past the cap: previously decoded to a 2**140-scale int.
        with self.assertRaises(ValueError):
            decode_varint(b"\xff" * 20 + b"\x01", 0)

    def test_cap_is_measured_from_the_start_index(self):
        data = b"\x2a" + b"\xff" * 9 + b"\x01"
        val, idx = decode_varint(data, 1)
        self.assertEqual(val, self.MAX_U64)
        self.assertEqual(idx, 11)

    def test_corrupt_varint_value_stops_field_parsing_keeping_prior_fields(self):
        raw = bytes([0x08, 42]) + bytes([0x10]) + b"\xff" * 12 + bytes([0x18, 7])
        self.assertEqual(parse_proto_fields(raw), [(1, 0, 42)])

    def test_corrupt_tag_and_length_stop_field_parsing(self):
        overlong = b"\xff" * 12
        self.assertEqual(parse_proto_fields(overlong + bytes([0x01])), [])
        self.assertEqual(parse_proto_fields(bytes([0x08, 42, 0x12]) + overlong), [(1, 0, 42)])

    def test_corrupt_usage_blob_cannot_inflate_totals(self):
        # output (field 3) is valid; prompt (field 2) is a 20-byte varint.
        raw = bytes([0x18, 0xC8, 0x01]) + bytes([0x10]) + b"\xff" * 19 + b"\x01"
        usage = parse_usage_proto(raw)
        self.assertEqual(usage.output_tokens, 200)
        self.assertEqual(usage.prompt_tokens, 0)
        self.assertEqual(usage.total_tokens, 200)


if __name__ == "__main__":
    unittest.main()
